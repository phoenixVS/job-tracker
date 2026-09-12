import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import MatcherDep, SessionDep, SettingsDep
from app.core.database import get_sessionmaker
from app.models.job import Job, JobRead, JobUpdate, SyncResult
from app.services.sync import NoProfileError, SyncInProgressError, get_daily_jobs, run_sync

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/sync", response_model=SyncResult)
async def trigger_sync(matcher: MatcherDep, settings: SettingsDep) -> SyncResult:
    """Run fetch -> score -> store now."""
    try:
        return await run_sync(get_sessionmaker(), matcher, settings)
    except (NoProfileError, SyncInProgressError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/daily", response_model=list[JobRead])
async def daily_jobs(session: SessionDep, settings: SettingsDep) -> list[Job]:
    """Today's top ranked, non-dismissed positions."""
    return await get_daily_jobs(session, settings)


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
