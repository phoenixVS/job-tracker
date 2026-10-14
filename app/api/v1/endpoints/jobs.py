import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import JobFilterDep, MatcherDep, SessionDep, SettingsDep
from app.core.database import get_sessionmaker
from app.models.job import Job, JobListItem, JobPageRead, JobRead, JobUpdate, RunRead, StatusRead, SyncResult
from app.services.job_queries import get_daily_jobs, get_latest_run, list_sources, query_jobs
from app.services.sync import NoProfileError, SyncInProgressError, get_active_profile, run_sync, sync_in_progress

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/sync", response_model=SyncResult)
async def trigger_sync(matcher: MatcherDep, settings: SettingsDep) -> SyncResult:
    """Run fetch -> score -> store now."""
    try:
        return await run_sync(get_sessionmaker(), matcher, settings)
    except (NoProfileError, SyncInProgressError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("", response_model=JobPageRead)
async def list_jobs(session: SessionDep, settings: SettingsDep, job_filter: JobFilterDep) -> JobPageRead:
    """Ranked matches: the daily digest window (default) or every stored match, with filters and paging."""
    page = await query_jobs(session, settings, job_filter)
    return JobPageRead(
        items=[JobListItem.from_job(job) for job in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        window=job_filter.window,
    )


@router.get("/status", response_model=StatusRead)
async def status(session: SessionDep, settings: SettingsDep) -> StatusRead:
    """Profile, last sync run and settings the web UI shows around the list."""
    profile = await get_active_profile(session)
    run = await get_latest_run(session)
    return StatusRead(
        profile=profile.to_read() if profile else None,
        last_run=RunRead.model_validate(run, from_attributes=True) if run else None,
        sync_in_progress=sync_in_progress(),
        similarity_threshold=settings.similarity_threshold,
        daily_target_count=settings.daily_target_count,
        sources=await list_sources(session),
    )


@router.get("/daily", response_model=list[JobRead])
async def daily_jobs(session: SessionDep, settings: SettingsDep) -> list[Job]:
    """Today's top ranked, non-dismissed positions."""
    return await get_daily_jobs(session, settings)


@router.get("/{job_id}", response_model=JobRead)
async def get_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.patch("/{job_id}", response_model=JobRead)
async def update_job(job_id: uuid.UUID, update: JobUpdate, session: SessionDep) -> Job:
    """Bookmark or dismiss a listing."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    for field, value in update.model_dump(exclude_none=True).items():
        setattr(job, field, value)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job
