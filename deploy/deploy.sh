#!/usr/bin/env bash
# Deploys image tag $1 on the server; run from CI as `bash deploy.sh <tag>` in the deploy dir.
# Steps: pull -> stop -> back up the DB -> start -> wait for polling; on failure roll back.
set -euo pipefail

TAG="${1:?usage: deploy.sh <image tag>}"
IMAGE=ghcr.io/two-lifes/finance_tracker
KEEP_BACKUPS=10
STARTUP_TIMEOUT=90

cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env in $PWD" >&2; exit 1; }
mkdir -p data backups
PREV_TAG=$(cat .deployed_tag 2>/dev/null || true)

# Shared with the subscription bot; whichever deploys first creates it
docker network inspect bots_internal >/dev/null 2>&1 || docker network create bots_internal

# Pull first, so the bot is down only for the restart itself
IMAGE_TAG="$TAG" docker compose pull

# The container runs as uid 1000 and must be able to write the DB
if ! docker run --rm --user 1000 -v "$PWD/data:/data" --entrypoint sh "$IMAGE:$TAG" \
    -c 'test -w /data'; then
    echo "data/ is not writable by uid 1000, run: sudo chown 1000:1000 $PWD/data" >&2
    exit 1
fi

# Migrations run on start, so take a consistent copy while the bot is stopped
docker compose stop
BACKUP=""
if [ -f data/finance.db ]; then
    BACKUP="backups/finance-$(date -u +%Y%m%d-%H%M%S)-${PREV_TAG:-none}.db"
    cp data/finance.db "$BACKUP"
    # Names start with a UTC timestamp, so a reverse sort puts the newest first
    printf '%s\n' backups/finance-*.db | sort -r | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm --
    echo "DB backup: $BACKUP"
fi

wait_ready() {
    local since=$1
    for _ in $(seq "$STARTUP_TIMEOUT"); do
        # aiogram logs this after getMe succeeded: token, proxy and DB are all fine
        if docker compose logs --since "$since" bot 2>/dev/null | grep -q "Run polling"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
IMAGE_TAG="$TAG" docker compose up -d
if wait_ready "$STARTED"; then
    echo "$TAG" > .deployed_tag
    echo "Deployed $TAG"
    # Keep the current and the previous image for a quick manual rollback
    # grep exits 1 when there is nothing to remove (e.g. the first deploy)
    docker images "$IMAGE" --format '{{.Tag}}' \
        | { grep -vxF -e "$TAG" -e "${PREV_TAG:-$TAG}" || true; } \
        | xargs -r -I{} docker rmi "$IMAGE:{}"
    exit 0
fi

echo "Bot did not start within ${STARTUP_TIMEOUT}s, last logs:" >&2
docker compose logs --since "$STARTED" --tail 50 bot >&2
docker compose stop
if [ -n "$PREV_TAG" ]; then
    # The new version may have migrated the DB; the old one needs the old schema
    if [ -n "$BACKUP" ]; then
        cp "$BACKUP" data/finance.db
    fi
    IMAGE_TAG="$PREV_TAG" FAILED_VERSION="$TAG" docker compose up -d
    echo "Rolled back to $PREV_TAG" >&2
fi
exit 1
