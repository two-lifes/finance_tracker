"""History of bot versions on the server; the scheduler tells the owner about new entries.

The image carries its version (git sha) and commit subject in env (see Dockerfile). When a new
version fails to start, deploy.sh rolls back and starts the previous one with FAILED_VERSION.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Deployment

STATUS_OK = "ok"
STATUS_FAILED = "failed"
TITLE_MAX = 200


async def record_startup(
    session: AsyncSession,
    version: str | None,
    title: str | None,
    failed_version: str | None,
    now: datetime,
) -> None:
    """Records this start. Restarts of the same version (reboots, crashes) add nothing."""
    if failed_version:
        known = await session.scalar(
            select(Deployment.id).where(
                Deployment.version == failed_version, Deployment.status == STATUS_FAILED
            )
        )
        if known is None:
            session.add(
                Deployment(version=failed_version, status=STATUS_FAILED, created_at=now)
            )
    if version:
        # A local run has no version; a rollback restarts the latest ok version
        current = await session.scalar(
            select(Deployment.version)
            .where(Deployment.status == STATUS_OK)
            .order_by(Deployment.id.desc())
            .limit(1)
        )
        if current != version:
            session.add(
                Deployment(
                    version=version,
                    title=(title or None) and title[:TITLE_MAX],
                    status=STATUS_OK,
                    created_at=now,
                )
            )
    await session.flush()


async def pending_deployments(session: AsyncSession) -> list[Deployment]:
    rows = await session.scalars(
        select(Deployment).where(Deployment.notified_at.is_(None)).order_by(Deployment.id)
    )
    return list(rows)


async def list_deployments(session: AsyncSession, limit: int = 10) -> list[Deployment]:
    rows = await session.scalars(select(Deployment).order_by(Deployment.id.desc()).limit(limit))
    return list(rows)
