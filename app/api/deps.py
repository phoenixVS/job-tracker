from typing import Annotated

from fastapi import Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.services.matcher import Matcher, get_matcher

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MatcherDep = Annotated[Matcher, Depends(get_matcher)]
