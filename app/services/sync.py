"""The fetch -> score -> store pipeline and digest queries, shared by the API, CLI and scheduler."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.models.common import utcnow
from app.models.cv import ACTIVE_PROFILE_ID, CandidateProfile, CandidateProfileRecord
from app.models.job import DailyRun, Job, RawJob, SyncResult
from app.services.job_fetcher import JobSource, SearchParams, build_sources
from app.services.matcher import Matcher, bytes_to_vector, full_text, metadata_text, vector_to_bytes

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
INSERT_CHUNK = 100
_LEVEL_PREFIX_RE = re.compile(r"^(?:senior|sr\.?|lead|staff|principal|junior|jr\.?|mid-level|intermediate)\s+", re.I)
_sync_lock = asyncio.Lock()


class NoProfileError(RuntimeError):
    pass


class SyncInProgressError(RuntimeError):
    pass


# --------------------------------------------------------------------------- profile


async def get_active_profile(session: AsyncSession) -> CandidateProfileRecord | None:
    return await session.get(CandidateProfileRecord, ACTIVE_PROFILE_ID)


async def save_profile(
    session: AsyncSession, matcher: Matcher, profile: CandidateProfile, source_filename: str | None
) -> CandidateProfileRecord:
    """Embed the profile once and make it the active one."""
    vector = await asyncio.to_thread(matcher.set_profile, profile.summary_blob)
    record = await get_active_profile(session) or CandidateProfileRecord(id=ACTIVE_PROFILE_ID)
    record.skills = profile.skills
    record.roles = profile.roles
    record.seniority = profile.seniority
    record.years_experience = profile.years_experience
    record.summary_blob = profile.summary_blob
    record.embedding = vector_to_bytes(vector)
    record.embedding_model = matcher.model_name
    record.source_filename = source_filename
    record.updated_at = utcnow()
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def _load_profile_vector(session: AsyncSession, matcher: Matcher, record: CandidateProfileRecord) -> None:
    if record.embedding and record.embedding_model == matcher.model_name:
        matcher.set_profile_vector(bytes_to_vector(record.embedding))
        return
    # The embedding model changed since upload: re-encode the stored summary and persist it.
    vector = await asyncio.to_thread(matcher.set_profile, record.summary_blob)
    record.embedding = vector_to_bytes(vector)
    record.embedding_model = matcher.model_name
    session.add(record)
    await session.commit()


def build_queries(roles: Sequence[str]) -> list[str]:
    """Search by role without the level prefix ("Senior Backend Engineer" -> "Backend Engineer")."""
    queries: list[str] = []
    for role in roles:
        query = _LEVEL_PREFIX_RE.sub("", role).strip()
        if query and query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
        if len(queries) == MAX_QUERIES:
            break
    return queries or ["software engineer"]


# --------------------------------------------------------------------------- pipeline


def sync_in_progress() -> bool:
    return _sync_lock.locked()


async def run_sync(
    session_factory: async_sessionmaker[AsyncSession],
    matcher: Matcher,
    settings: Settings,
    sources: list[JobSource] | None = None,
) -> SyncResult:
    if _sync_lock.locked():
        raise SyncInProgressError("A sync is already running")
    async with _sync_lock:
        owns_sources = sources is None
        sources = build_sources(settings) if sources is None else sources
        try:
            return await _run_sync(session_factory, matcher, settings, sources)
        finally:
            if owns_sources:
                await asyncio.gather(*(source.aclose() for source in sources), return_exceptions=True)


async def _run_sync(
    session_factory: async_sessionmaker[AsyncSession],
    matcher: Matcher,
    settings: Settings,
    sources: list[JobSource],
) -> SyncResult:
    started = time.monotonic()
    async with session_factory() as session:
        record = await get_active_profile(session)
        if record is None:
            raise NoProfileError("No CV uploaded yet: POST /api/v1/cv/upload first")
        await _load_profile_vector(session, matcher, record)
        run = DailyRun()
        session.add(run)
        await session.commit()
        run_id = run.id
        queries = build_queries(record.roles)
        seniority = record.seniority if settings.filter_by_seniority else None
    assert run_id is not None

    try:
        batch, source_status, any_success = await _fetch_all(sources, queries, seniority, settings)

        async with session_factory() as session:
            existing = await _existing_keys(session, list(batch))
        new_jobs = [job for key, job in batch.items() if key not in existing]

        scores, final_jobs, enriched = await _score_two_stage(new_jobs, sources, matcher, settings)

        async with session_factory() as session:
            inserted = await _insert_jobs(session, final_jobs, scores)
            matched = sum(1 for key in inserted if scores[key] >= settings.similarity_threshold)
            status = "success" if all(s.startswith("ok") for s in source_status.values()) else (
                "partial" if any_success else "failed"
            )
            run = await session.get(DailyRun, run_id)
            assert run is not None
            run.status = status
            run.finished_at = utcnow()
            run.jobs_fetched = len(batch)
            run.jobs_new = len(inserted)
            run.jobs_matched_count = matched
            if status != "success":
                run.error = "; ".join(f"{name}: {s}" for name, s in source_status.items() if not s.startswith("ok"))
            session.add(run)
            await session.commit()
    except Exception as exc:
        async with session_factory() as session:
            run = await session.get(DailyRun, run_id)
            if run is not None:
                run.status, run.finished_at, run.error = "failed", utcnow(), repr(exc)
                session.add(run)
                await session.commit()
        raise

    result = SyncResult(
        run_id=run_id,
        status=status,
        fetched=len(batch),
        new=len(inserted),
        matched=matched,
        enriched=enriched,
        sources=source_status,
        duration_seconds=round(time.monotonic() - started, 2),
    )
    logger.info("Sync finished: %s", result.model_dump())
    return result


async def _fetch_all(
    sources: list[JobSource], queries: list[str], seniority: str | None, settings: Settings
) -> tuple[dict[str, RawJob], dict[str, str], bool]:
    per_query_limit = max(1, settings.max_jobs_per_source // len(queries))

    async def search(source: JobSource, query: str) -> tuple[JobSource, list[RawJob], Exception | None]:
        params = SearchParams(query=query, remote=settings.remote_only, seniority=seniority,
                              lookback_days=settings.fetch_lookback_days, limit=per_query_limit)
        try:
            return source, await source.search(params), None
        except Exception as exc:  # one failing source/query must not abort the others
            logger.warning("Search failed for %s query=%r: %s", source.name, query, exc)
            return source, [], exc

    results = await asyncio.gather(*(search(source, query) for source in sources for query in queries))

    batch: dict[str, RawJob] = {}
    counts = {source.name: 0 for source in sources}
    errors: dict[str, list[str]] = {source.name: [] for source in sources}
    for source, jobs, error in results:
        if error is not None:
            errors[source.name].append(str(error))
        for job in jobs:
            if job.storage_key not in batch:
                batch[job.storage_key] = job
                counts[source.name] += 1

    status: dict[str, str] = {}
    any_success = False
    for source in sources:
        failures = errors[source.name]
        if not failures:
            status[source.name] = f"ok ({counts[source.name]} jobs)"
            any_success = True
        elif len(failures) < len(queries):
            status[source.name] = f"partial ({counts[source.name]} jobs): {failures[0]}"
            any_success = True
        else:
            status[source.name] = f"error: {failures[0]}"
    return batch, status, any_success


async def _existing_keys(session: AsyncSession, keys: list[str]) -> set[str]:
    existing: set[str] = set()
    for start in range(0, len(keys), 500):
        rows = await session.exec(select(Job.external_id).where(col(Job.external_id).in_(keys[start:start + 500])))
        existing.update(rows.all())
    return existing


async def _score_two_stage(
    jobs: list[RawJob], sources: list[JobSource], matcher: Matcher, settings: Settings
) -> tuple[dict[str, float], list[RawJob], int]:
    """Stage 1 scores search metadata for every job; stage 2 fetches descriptions for the best N and re-scores."""
    if not jobs:
        return {}, [], 0
    stage1 = await asyncio.to_thread(matcher.score_jobs, jobs, metadata_text)
    scores = {job.storage_key: score for job, score in zip(jobs, stage1)}

    sources_by_name = {source.name: source for source in sources}
    ranked = sorted(jobs, key=lambda job: scores[job.storage_key], reverse=True)
    to_enrich = [job for job in ranked[: settings.detail_enrich_top_n]
                 if not job.description and job.source in sources_by_name]

    async def enrich(job: RawJob) -> RawJob:
        try:
            return await sources_by_name[job.source].fetch_details(job)
        except Exception as exc:  # keep the stage-1 score if details can't be fetched
            logger.warning("Detail fetch failed for %s: %s", job.storage_key, exc)
            return job

    by_key = {job.storage_key: job for job in jobs}
    enriched_jobs = await asyncio.gather(*(enrich(job) for job in to_enrich))
    for job in enriched_jobs:
        by_key[job.storage_key] = job

    described = [job for job in by_key.values() if job.description]
    if described:
        stage2 = await asyncio.to_thread(matcher.score_jobs, described, full_text)
        scores.update({job.storage_key: score for job, score in zip(described, stage2)})
    enriched = sum(1 for job in enriched_jobs if job.description)
    return scores, list(by_key.values()), enriched


async def _insert_jobs(session: AsyncSession, jobs: list[RawJob], scores: dict[str, float]) -> list[str]:
    """Insert new jobs, ignoring external_id conflicts (e.g. a concurrent cron run). Returns inserted keys."""
    now = utcnow()
    rows = [
        {
            "id": uuid.uuid4(),
            "external_id": job.storage_key,
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "is_remote": job.is_remote,
            "url": job.url,
            "description": job.description,
            "published_at": job.published_at,
            "relevance_score": scores[job.storage_key],
            "has_description": bool(job.description),
            "first_seen_at": now,
            "is_dismissed": False,
            "is_bookmarked": False,
        }
        for job in jobs
    ]
    inserted: list[str] = []
    for start in range(0, len(rows), INSERT_CHUNK):
        statement = (
            sqlite_insert(Job)
            .values(rows[start:start + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["external_id"])
            .returning(Job.external_id)
        )
        result = await session.exec(statement)
        inserted.extend(result.scalars().all())
    await session.commit()
    return inserted


# --------------------------------------------------------------------------- queries


async def get_daily_jobs(session: AsyncSession, settings: Settings, now: datetime | None = None) -> list[Job]:
    """Top-N non-dismissed matches first seen in the last 24h cycle.

    The window reaches back to the latest completed run when it started more than 24h ago,
    so a late scheduler run never empties the digest.
    """
    now = now or utcnow()
    cutoff = now - timedelta(hours=24)
    latest_start = (await session.exec(
        select(DailyRun.started_at)
        .where(DailyRun.status != "failed")
        .order_by(col(DailyRun.started_at).desc())
        .limit(1)
    )).first()
    if latest_start is not None and latest_start < cutoff:
        cutoff = latest_start
    statement = (
        select(Job)
        .where(
            col(Job.is_dismissed).is_(False),
            Job.relevance_score >= settings.similarity_threshold,
            Job.first_seen_at >= cutoff,
        )
        .order_by(col(Job.relevance_score).desc(), col(Job.first_seen_at).desc())
        .limit(settings.daily_target_count)
    )
    return list((await session.exec(statement)).all())
