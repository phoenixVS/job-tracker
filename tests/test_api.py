"""End-to-end API flow on a temp SQLite DB, with the fake encoder and bundled mock jobs (no network)."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.database import configure_engine, dispose_engine, get_sessionmaker, init_db
from app.main import create_app
from app.models.common import utcnow
from app.models.job import DailyRun, Job
from app.services.job_queries import get_daily_jobs
from app.services.matcher import Matcher, set_matcher

CV_LINES = [
    "Alex Rivera",
    "Senior Backend Engineer | alex@example.com",
    "Summary",
    "Backend engineer building Python APIs with FastAPI, Django and PostgreSQL on AWS.",
    "Skills",
    "Python, FastAPI, Django, PostgreSQL, Redis, Docker, AWS, REST APIs",
    "Experience",
    "Senior Backend Engineer, Acme GmbH    Jan 2021 - Present",
    "Software Engineer, Beta Ltd    Jun 2017 - Dec 2020",
]
MOCK_REMOTE_JOBS = 24  # remote postings in app/services/mock_jobs.json


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        jobdatalake_api_key=None,
        worklittle_api_key=None,
        scheduler_enabled=False,
        similarity_threshold=0.15,
        detail_enrich_top_n=10,
    )


@pytest.fixture
def client(settings, fake_encoder):
    set_matcher(Matcher(fake_encoder, model_name="fake-bow"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client
    set_matcher(None)


def upload_cv(client: TestClient, pdf: bytes):
    return client.post("/api/v1/cv/upload", files={"file": ("cv.pdf", pdf, "application/pdf")})


def test_upload_sync_daily_and_triage_flow(client, make_pdf, settings):
    assert client.get("/api/v1/cv/profile").status_code == 404
    no_profile = client.post("/api/v1/jobs/sync")
    assert no_profile.status_code == 409
    assert "upload" in no_profile.json()["detail"]

    uploaded = upload_cv(client, make_pdf(CV_LINES))
    assert uploaded.status_code == 200, uploaded.text
    profile = uploaded.json()
    assert profile["roles"][0] == "Senior Backend Engineer"
    assert {"Python", "FastAPI", "PostgreSQL"} <= set(profile["skills"])
    # "Present" makes the total grow over time; the exact years->level mapping is covered in test_cv_parser.
    assert profile["seniority"] in {"senior", "staff"}
    assert profile["source_filename"] == "cv.pdf"
    assert client.get("/api/v1/cv/profile").json()["roles"] == profile["roles"]

    first = client.post("/api/v1/jobs/sync").json()
    assert first["status"] == "success"
    assert first["fetched"] == first["new"] == MOCK_REMOTE_JOBS  # duplicates across role queries collapsed
    assert first["enriched"] == settings.detail_enrich_top_n
    assert 0 < first["matched"] <= first["enriched"] <= first["new"]  # only described jobs can match
    assert first["sources"] == {"mock": f"ok ({MOCK_REMOTE_JOBS} jobs)"}

    daily = client.get("/api/v1/jobs/daily").json()
    scores = [job["relevance_score"] for job in daily]
    assert 0 < len(daily) <= settings.daily_target_count
    assert scores == sorted(scores, reverse=True)
    assert all(score >= settings.similarity_threshold for score in scores)
    top_titles = [job["title"] for job in daily[:5]]
    assert any("Python" in title or "Backend" in title for title in top_titles)
    assert "Customer Support Specialist" not in top_titles

    second = client.post("/api/v1/jobs/sync").json()
    assert (second["fetched"], second["new"], second["matched"]) == (MOCK_REMOTE_JOBS, 0, 0)

    dismissed_id, bookmarked_id = daily[0]["id"], daily[1]["id"]
    dismissed = client.patch(f"/api/v1/jobs/{dismissed_id}", json={"is_dismissed": True})
    assert dismissed.status_code == 200 and dismissed.json()["is_dismissed"] is True
    bookmarked = client.patch(f"/api/v1/jobs/{bookmarked_id}", json={"is_bookmarked": True})
    assert bookmarked.json()["is_bookmarked"] is True

    after = {job["id"]: job for job in client.get("/api/v1/jobs/daily").json()}
    assert dismissed_id not in after
    assert after[bookmarked_id]["is_bookmarked"] is True


def test_patch_validation(client):
    assert client.patch(f"/api/v1/jobs/{uuid.uuid4()}", json={"is_dismissed": True}).status_code == 404
    assert client.patch(f"/api/v1/jobs/{uuid.uuid4()}", json={}).status_code == 422
    assert client.patch("/api/v1/jobs/not-a-uuid", json={"is_dismissed": True}).status_code == 422


def test_upload_rejects_non_pdf(client):
    response = upload_cv(client, b"plain text, not a pdf")

    assert response.status_code == 422
    assert "not a PDF" in response.json()["detail"]


def test_daily_is_empty_before_any_sync(client):
    assert client.get("/api/v1/jobs/daily").json() == []


async def test_daily_window_reaches_back_to_latest_run(settings):
    configure_engine(settings.database_url)
    await init_db()
    now = utcnow()
    async with get_sessionmaker()() as session:
        session.add(DailyRun(started_at=now - timedelta(hours=30), status="success"))
        session.add_all([
            Job(external_id="mock:in-window", source="mock", title="A", relevance_score=0.9,
                first_seen_at=now - timedelta(hours=29), has_description=True),
            Job(external_id="mock:too-old", source="mock", title="B", relevance_score=0.8,
                first_seen_at=now - timedelta(hours=40), has_description=True),
            Job(external_id="mock:low-score", source="mock", title="C", relevance_score=0.01, first_seen_at=now,
                has_description=True),
            Job(external_id="mock:dismissed", source="mock", title="D", relevance_score=0.95, first_seen_at=now,
                is_dismissed=True, has_description=True),
            Job(external_id="mock:no-description", source="mock", title="E", relevance_score=0.99, first_seen_at=now),
        ])
        await session.commit()
        titles = [job.title for job in await get_daily_jobs(session, settings, now=now)]
    await dispose_engine()

    assert titles == ["A"]


def _synced(client, make_pdf):
    upload_cv(client, make_pdf(CV_LINES))
    assert client.post("/api/v1/jobs/sync").status_code == 200


def test_status_before_and_after_sync(client, make_pdf, settings):
    before = client.get("/api/v1/jobs/status").json()
    assert (before["profile"], before["last_run"], before["sync_in_progress"]) == (None, None, False)
    assert before["sources"] == []

    _synced(client, make_pdf)
    after = client.get("/api/v1/jobs/status").json()
    assert after["profile"]["roles"][0] == "Senior Backend Engineer"
    assert after["last_run"]["status"] == "success"
    assert after["sources"] == ["mock"]
    assert after["similarity_threshold"] == settings.similarity_threshold


def test_list_jobs_daily_and_all_with_paging(client, make_pdf, settings):
    _synced(client, make_pdf)

    daily = client.get("/api/v1/jobs").json()
    assert daily["window"] == "daily"
    assert 0 < len(daily["items"]) <= daily["total"] <= settings.daily_target_count
    assert "description" not in daily["items"][0] and "snippet" in daily["items"][0]

    everything = client.get("/api/v1/jobs", params={"window": "all", "limit": 100}).json()
    scores = [job["relevance_score"] for job in everything["items"]]
    assert scores == sorted(scores, reverse=True)
    assert everything["total"] == len(everything["items"]) >= daily["total"]

    first = client.get("/api/v1/jobs", params={"window": "all", "limit": 2}).json()
    second = client.get("/api/v1/jobs", params={"window": "all", "limit": 2, "offset": 2}).json()
    ids = [job["id"] for job in first["items"] + second["items"]]
    assert ids == [job["id"] for job in everything["items"][:4]]
    assert first["total"] == everything["total"]


def test_list_jobs_filters(client, make_pdf):
    _synced(client, make_pdf)
    url = "/api/v1/jobs"
    base = {"window": "all", "limit": 100}

    assert client.get(url, params={**base, "source": "nope"}).json()["items"] == []
    assert client.get(url, params={**base, "source": "mock"}).json()["total"] > 0
    assert client.get(url, params={**base, "remote": False}).json()["items"] == []  # mock jobs are all remote
    assert client.get(url, params={**base, "min_score": 1}).json()["items"] == []
    for sort in ("newest", "published"):
        assert client.get(url, params={**base, "sort": sort}).status_code == 200

    target = client.get(url, params=base).json()["items"][0]["id"]
    client.patch(f"{url}/{target}", json={"is_bookmarked": True})
    saved = client.get(url, params={**base, "bookmarked": True}).json()["items"]
    assert [job["id"] for job in saved] == [target]

    total = client.get(url, params=base).json()["total"]
    client.patch(f"{url}/{target}", json={"is_dismissed": True})
    assert client.get(url, params=base).json()["total"] == total - 1
    assert client.get(url, params={**base, "include_dismissed": True}).json()["total"] == total


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"min_score": 2}, {"sort": "bogus"}, {"window": "bogus"},
])
def test_list_jobs_validation(client, params):
    assert client.get("/api/v1/jobs", params=params).status_code == 422


def test_get_job_by_id(client, make_pdf):
    _synced(client, make_pdf)
    item = client.get("/api/v1/jobs", params={"window": "all", "limit": 1}).json()["items"][0]

    job = client.get(f"/api/v1/jobs/{item['id']}")
    assert job.status_code == 200 and job.json()["description"]
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404


def test_web_ui_is_served(client):
    index = client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert "<title>Job Matches</title>" in index.text
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
