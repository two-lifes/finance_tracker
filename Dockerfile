FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/finance.db

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY bot ./bot
COPY migrations ./migrations
RUN pip install .

# `docker build --target test .` runs the test suite and linter on the image's Python
FROM base AS test
RUN pip install ".[dev]"
COPY tests ./tests
RUN python -m pytest -q && ruff check bot tests

FROM base AS runtime
RUN useradd --create-home --uid 1000 bot && mkdir -p /data && chown bot /data
USER bot
VOLUME ["/data"]
# Set by CI; last, so a new commit doesn't invalidate the cached layers above
ARG APP_VERSION=
ARG APP_TITLE=
ENV APP_VERSION=$APP_VERSION \
    APP_TITLE=$APP_TITLE
CMD ["python", "-m", "bot"]
