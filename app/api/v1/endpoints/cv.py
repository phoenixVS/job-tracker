from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.deps import MatcherDep, SessionDep, SettingsDep
from app.models.cv import ProfileRead
from app.services.cv_parser import CVParseError, build_profile
from app.services.sync import get_active_profile, save_profile

router = APIRouter(prefix="/cv", tags=["cv"])


@router.post("/upload", response_model=ProfileRead)
async def upload_cv(file: UploadFile, session: SessionDep, matcher: MatcherDep, settings: SettingsDep) -> ProfileRead:
    """Parse a CV PDF, embed its profile and make it the active candidate profile."""
    data = await file.read(settings.max_cv_bytes + 1)
    if len(data) > settings.max_cv_bytes:
        raise HTTPException(413, f"CV larger than {settings.max_cv_bytes // (1024 * 1024)} MB")
    try:
        profile = await run_in_threadpool(build_profile, data)
    except CVParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    record = await save_profile(session, matcher, profile, file.filename)
    return record.to_read()


@router.get("/profile", response_model=ProfileRead)
async def get_profile(session: SessionDep) -> ProfileRead:
    record = await get_active_profile(session)
    if record is None:
        raise HTTPException(404, "No CV uploaded yet")
    return record.to_read()
