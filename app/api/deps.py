from typing import Annotated

from fastapi import Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.services.job_queries import JobFilter, SortKey, Window
from app.services.matcher import Matcher, get_matcher

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MatcherDep = Annotated[Matcher, Depends(get_matcher)]


def job_filter_params(
    window: Window = "daily",
    source: Annotated[list[str] | None, Query()] = None,
    remote: bool | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=1)] = None,
    bookmarked: bool | None = None,
    include_dismissed: bool = False,
    sort: SortKey = "score",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobFilter:
    return JobFilter(
        window=window,
        sources=tuple(source or ()),
        remote=remote,
        min_score=min_score,
        bookmarked=bookmarked,
        include_dismissed=include_dismissed,
        sort=sort,
        limit=limit,
        offset=offset,
    )


JobFilterDep = Annotated[JobFilter, Depends(job_filter_params)]
