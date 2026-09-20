from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = "sqlite+aiosqlite:///./jobs.db"

    # Job sources (an empty key disables the source; no keys at all -> mock data)
    jobdatalake_api_key: str | None = None
    jobdatalake_base_url: str = "https://api.jobdatalake.com/v1"
    worklittle_api_key: str | None = None
    worklittle_base_url: str = "https://api.worklittle.com"

    # Matching
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    daily_target_count: int = Field(default=15, ge=1)

    # Fetching
    remote_only: bool = True
    # Off by default: a wrong CV seniority guess would silently drop results at the API.
    filter_by_seniority: bool = False
    fetch_lookback_days: int = Field(default=2, ge=0)
    max_jobs_per_source: int = Field(default=1500, ge=1)
    detail_enrich_top_n: int = Field(default=200, ge=0)
    http_max_retries: int = Field(default=4, ge=0)
    http_timeout_seconds: float = Field(default=30.0, gt=0)

    # Worklittle-only limits: its API is slow (search ~30s, detail ~90s) and the free tier allows 1,000 jobs/month.
    worklittle_max_jobs: int = Field(default=300, ge=1)
    worklittle_detail_top_n: int = Field(default=5, ge=0)
    worklittle_timeout_seconds: float = Field(default=120.0, gt=0)
    worklittle_max_retries: int = Field(default=1, ge=0)

    # CV upload
    max_cv_bytes: int = 10 * 1024 * 1024

    # Scheduling
    scheduler_enabled: bool = True
    sync_interval_hours: float = Field(default=24.0, gt=0)
    sync_on_startup: bool = False

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
