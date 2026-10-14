"""Read-side queries over stored jobs, shared by the API, web UI and CLI."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.models.common import utcnow
from app.models.job import DailyRun, Job

Window = Literal["daily", "all"]
SortKey = Literal["score", "newest", "published"]


@dataclass(frozen=True)
class JobFilter:
    window: Window = "daily"
    sources: tuple[str, ...] = ()
    remote: bool | None = None  # None = any
    min_score: float | None = None  # None = settings.similarity_threshold
    bookmarked: bool | None = None  # True = only bookmarked
    include_dismissed: bool = False
    sort: SortKey = "score"
    limit: int = 25
    offset: int = 0


@dataclass(frozen=True)
class JobPage:
    items: list[Job]
    total: int  # matching rows before limit/offset; in daily mode capped at daily_target_count
    limit: int
    offset: int


async def daily_cutoff(session: AsyncSession, now: datetime) -> datetime:
    """Start of the daily window: 24h back, or the latest completed run's start when that is older,
    so a late scheduler run never empties the digest."""
    cutoff = now - timedelta(hours=24)
    latest_start = (await session.exec(
        select(DailyRun.started_at)
        .where(DailyRun.status != "failed")
        .order_by(col(DailyRun.started_at).desc())
        .limit(1)
    )).first()
    if latest_start is not None and latest_start < cutoff:
        return latest_start
    return cutoff


def _where_clauses(f: JobFilter, settings: Settings, cutoff: datetime | None) -> list:
    # Only jobs scored on a fetched description are ranked: stage-1 metadata scores run higher and aren't comparable.
    floor = settings.similarity_threshold if f.min_score is None else f.min_score
    clauses = [col(Job.has_description).is_(True), col(Job.relevance_score) >= floor]
    if not f.include_dismissed:
        clauses.append(col(Job.is_dismissed).is_(False))
    if f.sources:
        clauses.append(col(Job.source).in_(f.sources))
    if f.remote is not None:
        clauses.append(col(Job.is_remote).is_(f.remote))
    if f.bookmarked:
        clauses.append(col(Job.is_bookmarked).is_(True))
    if cutoff is not None:
        clauses.append(col(Job.first_seen_at) >= cutoff)
    return clauses


def _order_by(sort: SortKey) -> list:
    score, seen = col(Job.relevance_score).desc(), col(Job.first_seen_at).desc()
    if sort == "newest":
        return [seen, score, col(Job.id)]
    if sort == "published":
        return [col(Job.published_at).is_(None), col(Job.published_at).desc(), score, seen, col(Job.id)]
    return [score, seen, col(Job.id)]  # id keeps pages stable when scores tie


async def query_jobs(session: AsyncSession, settings: Settings, f: JobFilter, now: datetime | None = None) -> JobPage:
    cutoff = await daily_cutoff(session, now or utcnow()) if f.window == "daily" else None
    clauses = _where_clauses(f, settings, cutoff)

    total = (await session.exec(select(func.count()).select_from(Job).where(*clauses))).one()
    limit = f.limit
    if f.window == "daily":  # keep "top N" true however the pager walks
        total = min(total, settings.daily_target_count)
        limit = max(0, min(limit, settings.daily_target_count - f.offset))
    if limit == 0:
        return JobPage([], total, f.limit, f.offset)

    statement = select(Job).where(*clauses).order_by(*_order_by(f.sort)).limit(limit).offset(f.offset)
    return JobPage(list((await session.exec(statement)).all()), total, f.limit, f.offset)


async def get_daily_jobs(session: AsyncSession, settings: Settings, now: datetime | None = None) -> list[Job]:
    """Top-N non-dismissed matches first seen in the last 24h cycle."""
    page = await query_jobs(session, settings, JobFilter(limit=settings.daily_target_count), now)
    return page.items


async def list_sources(session: AsyncSession) -> list[str]:
    return sorted((await session.exec(select(Job.source).distinct())).all())


async def get_latest_run(session: AsyncSession) -> DailyRun | None:
    return (await session.exec(select(DailyRun).order_by(col(DailyRun.started_at).desc()).limit(1))).first()
