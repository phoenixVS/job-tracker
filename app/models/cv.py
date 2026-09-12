from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import JSON, LargeBinary, Text
from sqlmodel import Field, SQLModel

from app.models.common import UTCDateTime, utcnow

ACTIVE_PROFILE_ID = 1


class CandidateProfile(BaseModel):
    skills: list[str] = []
    roles: list[str] = []
    seniority: str | None = None  # junior | mid | senior | staff
    years_experience: float | None = None
    summary_blob: str


class ProfileRead(CandidateProfile):
    source_filename: str | None = None
    updated_at: datetime


class CandidateProfileRecord(SQLModel, table=True):
    """The single active profile (row id=1), so a CV only has to be uploaded once."""

    __tablename__ = "candidate_profile"

    id: int = Field(default=ACTIVE_PROFILE_ID, primary_key=True)
    skills: list[str] = Field(default_factory=list, sa_type=JSON)
    roles: list[str] = Field(default_factory=list, sa_type=JSON)
    seniority: str | None = None
    years_experience: float | None = None
    summary_blob: str = Field(default="", sa_type=Text)
    embedding: bytes | None = Field(default=None, sa_type=LargeBinary)  # float32 vector
    embedding_model: str | None = None
    source_filename: str | None = None
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)

    def to_profile(self) -> CandidateProfile:
        return CandidateProfile(
            skills=list(self.skills or []),
            roles=list(self.roles or []),
            seniority=self.seniority,
            years_experience=self.years_experience,
            summary_blob=self.summary_blob,
        )

    def to_read(self) -> ProfileRead:
        return ProfileRead(
            **self.to_profile().model_dump(),
            source_filename=self.source_filename,
            updated_at=self.updated_at,
        )
