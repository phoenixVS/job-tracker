"""Fetcher tests against httpx.MockTransport, with payloads shaped like the vendors' documented examples."""

from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import Settings
from app.services.job_fetcher import (
    JobDataLakeClient,
    MockJobSource,
    SearchParams,
    SourceError,
    WorklittleClient,
    _parse_datetime,
    _Throttle,
    build_sources,
)

POSTED_AT = 1788000000  # unix seconds


def jdl_job(n: int) -> dict:
    return {
        "job_handle": f"jdl-{n}", "title": f"Backend Engineer {n}", "company_name": "Acme", "domain_name": "acme.com",
        "posted_at": POSTED_AT, "locations": ["Berlin, DE", "Remote"], "countries": ["DE"],
        "remote_type": "fully_remote", "job_function": "eng", "seniority": ["Senior"], "salary_min_usd": 90,
        "salary_max_usd": 130, "required_skills": ["Python", "PostgreSQL"], "employment_type": "full_time",
        "url": f"https://acme.com/jobs/{n}",
    }


def worklittle_job(job_id: str, closed_at: str | None = None) -> dict:
    return {
        "id": job_id, "title": "Site Reliability Engineer", "company": {"name": "Streamforge", "slug": "streamforge"},
        "apply_url": f"https://worklittle.com/apply/{job_id}", "can_apply": True, "closed_at": closed_at,
        "workplace_type": "remote", "location": "Remote - EU", "posted_at": "2026-09-10T08:30:00Z",
    }


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def unthrottled(client):
    client._throttle = _Throttle(max_concurrency=10, min_interval=0)
    return client


async def test_jobdatalake_search_normalizes_and_paginates(monkeypatch):
    monkeypatch.setattr(JobDataLakeClient, "MAX_PER_PAGE", 2)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        jobs = [jdl_job(n) for n in range((page - 1) * 2, min(page * 2, 3))]
        return httpx.Response(200, json={"found": 3, "page": page, "per_page": 2, "jobs": jobs, "stats": {}})

    client = unthrottled(JobDataLakeClient("jdl-key", transport=httpx.MockTransport(handler)))
    jobs = await client.search(SearchParams(query="backend engineer", tags=["python"], limit=10))
    await client.aclose()

    assert [r.url.params["page"] for r in requests] == ["1", "2"]
    first = requests[0]
    assert first.url.path == "/v1/jobs"
    assert first.headers["X-API-Key"] == "jdl-key"
    assert first.url.params["q"] == "backend engineer python"
    assert first.url.params["remote_type"] == "fully_remote"
    assert int(first.url.params["posted_after"]) > POSTED_AT * 1000  # milliseconds
    assert "seniority" not in first.url.params

    assert [job.external_id for job in jobs] == ["jdl-0", "jdl-1", "jdl-2"]
    job = jobs[0]
    assert job.storage_key == "jobdatalake:jdl-0"
    assert (job.source, job.title, job.company, job.location) == ("jobdatalake", "Backend Engineer 0", "Acme",
                                                                  "Berlin, DE, Remote")
    assert job.is_remote is True
    assert job.skills == ["Python", "PostgreSQL"]
    assert job.seniority == "Senior"
    assert job.published_at == datetime.fromtimestamp(POSTED_AT, tz=timezone.utc)
    assert job.description == ""


async def test_jobdatalake_details_turn_html_into_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/jdl-0"
        html = "<p>Build <b>APIs</b> &amp; services</p><ul><li>Python</li></ul>"
        return httpx.Response(200, json={"job": {"job_handle": "jdl-0", "description_html": html}})

    client = unthrottled(JobDataLakeClient("k", transport=httpx.MockTransport(handler)))
    base = JobDataLakeClient.normalize(jdl_job(0))
    detailed = await client.fetch_details(base)
    await client.aclose()

    assert detailed.description == "Build APIs & services Python"
    assert detailed.title == base.title


async def test_worklittle_cursor_pagination_auth_and_closed_jobs():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "cursor" not in request.url.params:
            data = [worklittle_job("wl-1"), worklittle_job("wl-2", closed_at="2026-09-01T00:00:00Z")]
            return httpx.Response(200, json={"data": data, "meta": {"next_cursor": "c2"}})
        return httpx.Response(200, json={"data": [worklittle_job("wl-3")], "meta": {"next_cursor": None}})

    client = unthrottled(WorklittleClient("sk-wl-api01-test", transport=httpx.MockTransport(handler)))
    jobs = await client.search(SearchParams(query="SRE", lookback_days=3, limit=10))
    await client.aclose()

    assert requests[0].headers["Authorization"] == "Bearer sk-wl-api01-test"
    assert requests[0].url.path == "/jobs"
    assert requests[0].url.params["workplace_type"] == "remote"
    assert requests[0].url.params["posted_within_days"] == "3"
    assert requests[1].url.params["cursor"] == "c2"
    assert [job.external_id for job in jobs] == ["wl-1", "wl-3"]  # closed posting dropped
    assert jobs[0].company == "Streamforge"
    assert jobs[0].url == "https://worklittle.com/apply/wl-1"
    assert jobs[0].is_remote is True
    assert jobs[0].published_at == datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)


def test_worklittle_location_falls_back_to_job_address_fields():
    item = worklittle_job("wl-7") | {"location": None, "locations": [], "job_city": "Hanover, MD", "job_state": "MD",
                                     "job_country": "US"}

    assert WorklittleClient.normalize(item).location == "Hanover, MD, US"


async def test_worklittle_details_use_description_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "wl-1", "description_text": "Keep  the\nplatform up."}})

    client = unthrottled(WorklittleClient("k", transport=httpx.MockTransport(handler)))
    detailed = await client.fetch_details(WorklittleClient.normalize(worklittle_job("wl-1")))
    await client.aclose()

    assert detailed.description == "Keep the platform up."


async def test_429_honors_retry_after_then_backs_off_then_succeeds():
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "RATE_LIMITED", "message": "slow"}}),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"found": 1, "jobs": [jdl_job(1)]}),
    ])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    sleeps = Sleeps()
    client = unthrottled(JobDataLakeClient("k", transport=httpx.MockTransport(handler), sleep=sleeps))
    jobs = await client.search(SearchParams(query="python", limit=5))
    await client.aclose()

    assert len(calls) == 3
    assert sleeps.delays[0] == 7.0
    assert 2.0 <= sleeps.delays[1] <= 2.5  # exponential backoff (2**1) + jitter
    assert [job.external_id for job in jobs] == ["jdl-1"]


async def test_worklittle_quota_exceeded_is_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"},
                              json={"error": {"code": "QUOTA_EXCEEDED", "message": "Monthly quota reached"}})

    sleeps = Sleeps()
    client = unthrottled(WorklittleClient("k", transport=httpx.MockTransport(handler), sleep=sleeps))
    with pytest.raises(SourceError, match="QUOTA_EXCEEDED") as excinfo:
        await client.search(SearchParams(query="python"))
    await client.aclose()

    assert excinfo.value.status_code == 429
    assert len(calls) == 1
    assert sleeps.delays == []


async def test_unauthorized_is_not_retried():
    client = unthrottled(JobDataLakeClient(
        "bad", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "Invalid API key"})),
        sleep=Sleeps(),
    ))
    with pytest.raises(SourceError, match="Invalid API key") as excinfo:
        await client.search(SearchParams(query="python"))
    await client.aclose()

    assert excinfo.value.status_code == 401


async def test_gives_up_after_max_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    sleeps = Sleeps()
    client = unthrottled(WorklittleClient("k", transport=httpx.MockTransport(handler), max_retries=2, sleep=sleeps))
    with pytest.raises(SourceError):
        await client.search(SearchParams(query="python"))
    await client.aclose()

    assert len(calls) == 3
    assert len(sleeps.delays) == 2


async def test_transport_errors_are_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"data": [worklittle_job("wl-9")], "meta": {"next_cursor": None}})

    client = unthrottled(WorklittleClient("k", transport=httpx.MockTransport(handler), sleep=Sleeps()))
    jobs = await client.search(SearchParams(query="sre"))
    await client.aclose()

    assert [job.external_id for job in jobs] == ["wl-9"]


@pytest.mark.parametrize(("value", "expected"), [
    (POSTED_AT, datetime.fromtimestamp(POSTED_AT, tz=timezone.utc)),
    (POSTED_AT * 1000, datetime.fromtimestamp(POSTED_AT, tz=timezone.utc)),
    (str(POSTED_AT), datetime.fromtimestamp(POSTED_AT, tz=timezone.utc)),
    ("2026-09-10T08:30:00Z", datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)),
    ("2026-09-10T08:30:00", datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)),
    ("not a date", None),
    (None, None),
])
def test_parse_datetime_variants(value, expected):
    assert _parse_datetime(value) == expected


async def test_build_sources_uses_mock_only_without_keys():
    no_keys = Settings(_env_file=None, jobdatalake_api_key=None, worklittle_api_key="")
    sources = build_sources(no_keys)
    assert [type(source) for source in sources] == [MockJobSource]

    with_key = Settings(_env_file=None, jobdatalake_api_key="jdl", worklittle_api_key=None)
    sources = build_sources(with_key)
    assert [type(source) for source in sources] == [JobDataLakeClient]
    await sources[0].aclose()


async def test_build_sources_gives_worklittle_its_own_limits():
    settings = Settings(_env_file=None, jobdatalake_api_key="jdl", worklittle_api_key="wl",
                        http_timeout_seconds=30, http_max_retries=4)
    jobdatalake, worklittle = build_sources(settings)
    try:
        assert (jobdatalake.max_jobs, jobdatalake.max_details, jobdatalake._max_retries) == (None, None, 4)
        assert jobdatalake._client.timeout.read == 30
        assert (worklittle.max_jobs, worklittle.max_details, worklittle._max_retries) == (300, 5, 1)
        assert worklittle._client.timeout.read == 120
    finally:
        await jobdatalake.aclose()
        await worklittle.aclose()


async def test_mock_source_behaves_like_real_feeds():
    source = MockJobSource()

    remote_jobs = await source.search(SearchParams(query="anything", remote=True, limit=100))
    all_jobs = await source.search(SearchParams(query="anything", remote=False, limit=100))

    assert remote_jobs and all(job.is_remote for job in remote_jobs)
    assert len(all_jobs) > len(remote_jobs)
    assert all(job.description == "" for job in remote_jobs)
    assert (await source.fetch_details(remote_jobs[0])).description
