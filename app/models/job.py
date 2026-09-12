import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, model_validator
from sqlalchemy import Text
from sqlmodel import Field, SQLModel

from app.models.common import UTCDateTime, utcnow

SourceName = Literal["jobdatalake", "worklittle", "mock"]


class RawJob(BaseModel):
    """A posting normalized from any external feed, before scoring/storage."""

    external_id: str  # the source's native id
    source: SourceName
    title: str
    company: str = ""
    location: str = ""
    is_remote: bool = False
    description: str = ""
    url: str = ""
    published_at: datetime | None = None
    # Extra metadata some feeds provide; used for stage-1 scoring.
    skills: list[str] = []
    seniority: str | None = None

    @property
    def storage_key(self) -> str:
        """Globally unique key: native ids from different sources may collide."""
        return f"{self.source}:{self.external_id}"


class Job(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    external_id: str = Field(index=True, unique=True, max_length=512)
    source: str = Field(index=True)
    title: str
    company: str = ""
    location: str = ""
    is_remote: bool = False
    url: str = ""
    description: str = Field(default="", sa_type=Text)
    published_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    relevance_score: float = Field(default=0.0, index=True)
    has_description: bool = False
    first_seen_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)
    is_dismissed: bool = Field(default=False, index=True)
    is_bookmarked: bool = False


class DailyRun(SQLModel, table=True):
    __tablename__ = "daily_run"

    id: int | None = Field(default=None, primary_key=True)
    run_date: date = Field(default_factory=lambda: utcnow().date(), index=True)
    started_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    status: str = "running"  # running | success | partial | failed
    jobs_fetched: int = 0
    jobs_new: int = 0
    jobs_matched_count: int = 0
    error: str | None = Field(default=None, sa_type=Text)


class JobRead(SQLModel):
    id: uuid.UUID
    external_id: str
    source: str
    title: str
    company: str
    location: str
    is_remote: bool
    url: str
    description: str
    published_at: datetime | None
    relevance_score: float
    has_description: bool
    first_seen_at: datetime
    is_dismissed: bool
    is_bookmarked: bool


class JobUpdate(SQLModel):
    is_dismissed: bool | None = None
    is_bookmarked: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "JobUpdate":
        if self.is_dismissed is None and self.is_bookmarked is None:
            raise ValueError("Provide is_dismissed and/or is_bookmarked")
        return self


class SyncResult(BaseModel):
    run_id: int
    status: str
    fetched: int
    new: int
    matched: int
    enriched: int
    sources: dict[str, str]
    duration_seconds: float
