"""Async clients for JobDataLake and Worklittle, normalized to RawJob, with a mock fallback.

Neither vendor fully documents its job object, so normalizers accept common field-name variants.
"""

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.models.job import RawJob

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 120.0
MOCK_DATA_PATH = Path(__file__).with_name("mock_jobs.json")


class SourceError(RuntimeError):
    def __init__(self, source: str, message: str, status_code: int | None = None) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.status_code = status_code


@dataclass
class SearchParams:
    query: str
    tags: list[str] = field(default_factory=list)
    remote: bool = True
    seniority: str | None = None  # junior | mid | senior | staff
    lookback_days: int = 2
    limit: int = 100

    @property
    def text(self) -> str:
        """Free-text query: the search phrase plus tags."""
        return " ".join([self.query, *self.tags]).strip()


class JobSource(Protocol):
    name: str
    max_jobs: int | None  # per-sync job cap; None -> MAX_JOBS_PER_SOURCE
    max_details: int | None  # per-sync detail-call cap; None -> only DETAIL_ENRICH_TOP_N applies

    async def search(self, params: SearchParams) -> list[RawJob]: ...

    async def fetch_details(self, job: RawJob) -> RawJob: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- payload helpers


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value not in (None, "", [])}


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        timestamp = float(value)
        if timestamp > 1e11:  # milliseconds
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _html_to_text(value: str) -> str:
    if "<" in value and ">" in value:
        value = re.sub(r"<\s*(br|/p|/li|/h\d|/div)[^>]*>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).split())


def _description(data: dict[str, Any]) -> str:
    raw = _first(data, "description_text", "description", "job_description", "description_html", "content", "body")
    return _html_to_text(str(raw)) if raw else ""


def _company_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(_first(value, "name", "display_name", "slug") or "")
    return str(value or "")


def _location(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_location(item) for item in value)))
    if isinstance(value, dict):
        return str(_first(value, "name", "display", "city", "country") or "")
    return str(value or "")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return f"HTTP {response.status_code} {error.get('code', '')}: {error.get('message', '')}".strip()
    detail = payload.get("detail") or payload.get("message") if isinstance(payload, dict) else None
    return f"HTTP {response.status_code}: {detail or str(payload)[:200]}"


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    return error.get("code") if isinstance(error, dict) else None


def _retry_after_seconds(header: str | None) -> float | None:
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _backoff_delay(attempt: int) -> float:
    return min(MAX_BACKOFF_SECONDS, 2.0**attempt) + random.uniform(0, 0.5)


# --------------------------------------------------------------------------- HTTP base


class _Throttle:
    """Caps concurrency and spaces request starts to stay under a source's documented rate limit."""

    def __init__(self, max_concurrency: int, min_interval: float) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()
        async with self._lock:
            now = time.monotonic()
            if self._next_start > now:
                await asyncio.sleep(self._next_start - now)
            self._next_start = max(now, self._next_start) + self._min_interval

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()


class _HttpSource:
    name = "http"

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        throttle: _Throttle,
        *,
        max_retries: int = 4,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
        max_jobs: int | None = None,
        max_details: int | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self.max_jobs = max_jobs
        self.max_details = max_details
        self._throttle = throttle
        self._max_retries = max_retries
        self._sleep = sleep  # injectable so tests don't actually wait

    async def aclose(self) -> None:
        await self._client.aclose()

    def _is_retryable(self, response: httpx.Response) -> bool:
        return response.status_code == 429 or response.status_code >= 500

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        attempt = 0
        while True:
            try:
                async with self._throttle:
                    response = await self._client.get(path, params=_clean_params(params or {}))
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise SourceError(self.name, f"network error: {exc!r}") from exc
                delay = _backoff_delay(attempt)
            else:
                if response.status_code < 400:
                    return response.json()
                if attempt >= self._max_retries or not self._is_retryable(response):
                    raise SourceError(self.name, _error_message(response), response.status_code)
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else _backoff_delay(attempt)
            attempt += 1
            logger.warning("%s: retrying GET %s in %.1fs (retry %d/%d)", self.name, path, delay, attempt,
                           self._max_retries)
            await self._sleep(delay)


# --------------------------------------------------------------------------- JobDataLake

_JDL_SENIORITY = {"junior": "Entry", "mid": "Mid Level", "senior": "Senior", "staff": "Staff"}


class JobDataLakeClient(_HttpSource):
    """GET /v1/jobs (page/per_page, max 100) and GET /v1/jobs/{handle}. Auth: X-API-Key. 10 req/s."""

    name = "jobdatalake"
    MAX_PER_PAGE = 100

    def __init__(self, api_key: str, base_url: str = "https://api.jobdatalake.com/v1", **kwargs: Any) -> None:
        headers = {"X-API-Key": api_key, "Accept": "application/json"}
        super().__init__(base_url, headers, _Throttle(max_concurrency=4, min_interval=0.125), **kwargs)

    async def search(self, params: SearchParams) -> list[RawJob]:
        query = params.text or "*"
        posted_after = None
        if params.lookback_days:
            posted_after = int((datetime.now(timezone.utc) - timedelta(days=params.lookback_days)).timestamp() * 1000)
        per_page = min(self.MAX_PER_PAGE, params.limit)
        jobs: list[RawJob] = []
        page = 1
        while len(jobs) < params.limit:
            data = await self._get_json("/jobs", {
                "q": query,
                "remote_type": "fully_remote" if params.remote else None,
                "seniority": _JDL_SENIORITY.get(params.seniority or ""),
                "posted_after": posted_after,
                "sort_by": "posted_at:desc",
                "page": page,
                "per_page": per_page,
            })
            items = data.get("jobs") or []
            jobs.extend(job for job in map(self.normalize, items) if job)
            found = data.get("found")
            if len(items) < per_page or (isinstance(found, int) and page * per_page >= found):
                break
            page += 1
        return jobs[: params.limit]

    async def fetch_details(self, job: RawJob) -> RawJob:
        data = await self._get_json(f"/jobs/{quote(job.external_id, safe='')}")
        payload = (data.get("job") or data.get("data") or data) if isinstance(data, dict) else {}
        return job.model_copy(update={
            "description": _description(payload) or job.description,
            "url": job.url or str(_first(payload, "url", "apply_url") or ""),
            "skills": job.skills or _as_list(payload.get("required_skills")),
        })

    @staticmethod
    def normalize(item: dict[str, Any]) -> RawJob | None:
        native_id, title = _first(item, "job_handle", "handle", "id"), _first(item, "title")
        if not native_id or not title:
            return None
        remote_type = str(item.get("remote_type") or "").lower()
        return RawJob(
            external_id=str(native_id),
            source="jobdatalake",
            title=str(title).strip(),
            company=_company_name(_first(item, "company_name", "company")),
            location=_location(_first(item, "locations", "location")),
            is_remote=remote_type in {"fully_remote", "remote"},
            description=_description(item),
            url=str(_first(item, "url", "apply_url") or ""),
            published_at=_parse_datetime(_first(item, "posted_at", "published_at", "created_at")),
            skills=_as_list(item.get("required_skills")),
            seniority=", ".join(_as_list(item.get("seniority"))) or None,
        )


# --------------------------------------------------------------------------- Worklittle

_WORKLITTLE_SENIORITY = {"junior": "entry", "mid": "mid", "senior": "senior", "staff": "staff"}


def _worklittle_location(item: dict[str, Any]) -> str:
    """Prefer the `locations` list; some postings leave it empty and only fill job_city/job_state/job_country."""
    listed = _location(_first(item, "location", "locations", "location_name"))
    if listed:
        return listed
    parts: list[str] = []
    for key in ("job_city", "job_state", "job_country"):
        value = item.get(key)
        if value and not any(str(value) in part for part in parts):
            parts.append(str(value))
    return ", ".join(parts)


class WorklittleClient(_HttpSource):
    """GET /jobs (cursor, max 50) and GET /jobs/{id}. Auth: Bearer. 60 req/min; QUOTA_EXCEEDED is final."""

    name = "worklittle"
    MAX_LIMIT = 50

    def __init__(self, api_key: str, base_url: str = "https://api.worklittle.com", **kwargs: Any) -> None:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        # Starts stay >=1s apart (60 req/min), but requests may overlap: a search takes ~30s and a detail call ~90s.
        super().__init__(base_url, headers, _Throttle(max_concurrency=5, min_interval=1.0), **kwargs)

    def _is_retryable(self, response: httpx.Response) -> bool:
        if response.status_code == 429 and _error_code(response) == "QUOTA_EXCEEDED":
            return False
        return super()._is_retryable(response)

    async def search(self, params: SearchParams) -> list[RawJob]:
        query = params.text
        jobs: list[RawJob] = []
        cursor: str | None = None
        while len(jobs) < params.limit:
            data = await self._get_json("/jobs", {
                "q": query,
                "workplace_type": "remote" if params.remote else None,
                "seniority_level": _WORKLITTLE_SENIORITY.get(params.seniority or ""),
                "posted_within_days": params.lookback_days or None,
                "limit": min(self.MAX_LIMIT, params.limit - len(jobs)),
                "cursor": cursor,
            })
            items = data.get("data") or []
            jobs.extend(job for job in map(self.normalize, items) if job)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not items or not cursor:
                break
        return jobs[: params.limit]

    async def fetch_details(self, job: RawJob) -> RawJob:
        data = await self._get_json(f"/jobs/{quote(job.external_id, safe='')}")
        payload = (data.get("data") or data) if isinstance(data, dict) else {}
        return job.model_copy(update={
            "description": _description(payload) or job.description,
            "location": job.location or _location(_first(payload, "location", "locations")),
            "url": job.url or str(_first(payload, "apply_url", "url") or ""),
        })

    @staticmethod
    def normalize(item: dict[str, Any]) -> RawJob | None:
        native_id, title = _first(item, "id"), _first(item, "title")
        if not native_id or not title or item.get("closed_at"):
            return None
        workplace = str(_first(item, "workplace_type", "remote_type") or "").lower()
        return RawJob(
            external_id=str(native_id),
            source="worklittle",
            title=str(title).strip(),
            company=_company_name(_first(item, "company", "company_name", "organization")),
            location=_worklittle_location(item),
            is_remote="remote" in workplace or item.get("remote") is True,
            description=_description(item),
            url=str(_first(item, "apply_url", "url", "job_url") or ""),
            published_at=_parse_datetime(_first(item, "posted_at", "published_at", "created_at")),
            skills=_as_list(_first(item, "skills", "required_skills")),
            seniority=str(_first(item, "seniority_level", "seniority") or "") or None,
        )


# --------------------------------------------------------------------------- mock


class MockJobSource:
    """Bundled postings for keyless/offline use. Search omits descriptions, like the real feeds."""

    name = "mock"
    max_jobs: int | None = None
    max_details: int | None = None

    def __init__(self, path: Path = MOCK_DATA_PATH) -> None:
        self._items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        self._by_id = {item["id"]: item for item in self._items}

    async def search(self, params: SearchParams) -> list[RawJob]:
        now = datetime.now(timezone.utc)
        jobs = [
            RawJob(
                external_id=item["id"],
                source="mock",
                title=item["title"],
                company=item.get("company", ""),
                location=item.get("location", ""),
                is_remote=item.get("is_remote", True),
                url=item.get("url", ""),
                published_at=now - timedelta(hours=item.get("hours_ago", 1)),
                skills=item.get("skills", []),
                seniority=item.get("seniority"),
            )
            for item in self._items
            if item.get("is_remote", True) or not params.remote
        ]
        return jobs[: params.limit]

    async def fetch_details(self, job: RawJob) -> RawJob:
        item = self._by_id.get(job.external_id, {})
        return job.model_copy(update={"description": item.get("description", "")})

    async def aclose(self) -> None:
        return None


def build_sources(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> list[JobSource]:
    common = {"max_retries": settings.http_max_retries, "timeout": settings.http_timeout_seconds,
              "transport": transport}
    sources: list[JobSource] = []
    if settings.jobdatalake_api_key:
        sources.append(JobDataLakeClient(settings.jobdatalake_api_key, settings.jobdatalake_base_url, **common))
    if settings.worklittle_api_key:
        sources.append(WorklittleClient(
            settings.worklittle_api_key,
            settings.worklittle_base_url,
            max_retries=settings.worklittle_max_retries,
            timeout=settings.worklittle_timeout_seconds,
            transport=transport,
            max_jobs=settings.worklittle_max_jobs,
            max_details=settings.worklittle_detail_top_n,
        ))
    if not sources:
        logger.warning("No JOBDATALAKE_API_KEY / WORKLITTLE_API_KEY configured; using bundled mock job data")
        sources.append(MockJobSource())
    return sources
