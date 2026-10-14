from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.database import configure_engine, dispose_engine, get_sessionmaker, init_db
from app.models.common import utcnow
from app.models.job import DailyRun, Job
from app.services.job_queries import JobFilter, get_daily_jobs, query_jobs

NOW = utcnow()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=False,
        similarity_threshold=0.3,
        daily_target_count=3,
    )


@pytest.fixture
async def session(settings):
    configure_engine(settings.database_url)
    await init_db()
    async with get_sessionmaker()() as session:
        yield session
    await dispose_engine()


def make_job(name: str, score: float = 0.5, age_hours: float = 1, **overrides) -> Job:
    fields = dict(external_id=f"mock:{name}", source="mock", title=name, relevance_score=score,
                  first_seen_at=NOW - timedelta(hours=age_hours), has_description=True)
    return Job(**{**fields, **overrides})


async def titles(session, settings, **filters) -> list[str]:
    page = await query_jobs(session, settings, JobFilter(**filters), now=NOW)
    return [job.title for job in page.items]


async def test_all_window_ignores_cutoff_but_keeps_base_rules(session, settings):
    session.add_all([
        make_job("recent", 0.6),
        make_job("old", 0.7, age_hours=100),
        make_job("low", 0.1),
        make_job("no-desc", 0.9, has_description=False),
        make_job("dismissed", 0.8, is_dismissed=True),
    ])
    await session.commit()

    assert await titles(session, settings, window="daily") == ["recent"]
    assert await titles(session, settings, window="all") == ["old", "recent"]
    assert await titles(session, settings, window="all", include_dismissed=True) == ["dismissed", "old", "recent"]


async def test_filters_and_min_score_override(session, settings):
    session.add_all([
        make_job("a", 0.9, source="worklittle", is_remote=True),
        make_job("b", 0.5, source="mock", is_bookmarked=True),
        make_job("c", 0.2, source="mock", is_remote=True),
    ])
    await session.commit()

    assert await titles(session, settings, window="all") == ["a", "b"]
    assert await titles(session, settings, window="all", min_score=0.1) == ["a", "b", "c"]
    assert await titles(session, settings, window="all", sources=("mock",)) == ["b"]
    assert await titles(session, settings, window="all", remote=True) == ["a"]
    assert await titles(session, settings, window="all", remote=False) == ["b"]
    assert await titles(session, settings, window="all", bookmarked=True) == ["b"]


async def test_sorting(session, settings):
    session.add_all([
        make_job("high-old", 0.9, age_hours=10, published_at=NOW - timedelta(days=5)),
        make_job("low-new", 0.4, age_hours=1, published_at=NOW - timedelta(days=1)),
        make_job("mid-unpublished", 0.6, age_hours=5),
    ])
    await session.commit()

    assert await titles(session, settings, sort="score") == ["high-old", "mid-unpublished", "low-new"]
    assert await titles(session, settings, sort="newest") == ["low-new", "mid-unpublished", "high-old"]
    assert await titles(session, settings, sort="published") == ["low-new", "high-old", "mid-unpublished"]


async def test_pagination_and_totals(session, settings):
    session.add_all([make_job(f"j{i}", 0.9 - i * 0.05) for i in range(5)])
    await session.commit()

    first = await query_jobs(session, settings, JobFilter(window="all", limit=2), now=NOW)
    last = await query_jobs(session, settings, JobFilter(window="all", limit=2, offset=4), now=NOW)
    beyond = await query_jobs(session, settings, JobFilter(window="all", limit=2, offset=10), now=NOW)

    assert (first.total, [j.title for j in first.items]) == (5, ["j0", "j1"])
    assert [j.title for j in last.items] == ["j4"]
    assert (beyond.total, beyond.items) == (5, [])


async def test_daily_is_capped_at_target_count_across_pages(session, settings):
    session.add_all([make_job(f"j{i}", 0.9 - i * 0.05) for i in range(5)])
    await session.commit()

    page1 = await query_jobs(session, settings, JobFilter(limit=2), now=NOW)
    page2 = await query_jobs(session, settings, JobFilter(limit=2, offset=2), now=NOW)
    page3 = await query_jobs(session, settings, JobFilter(limit=2, offset=4), now=NOW)

    assert page1.total == settings.daily_target_count == 3
    assert [j.title for j in page1.items + page2.items] == ["j0", "j1", "j2"]
    assert page3.items == []


async def test_daily_window_reaches_back_to_latest_run(session, settings):
    session.add(DailyRun(started_at=NOW - timedelta(hours=30), status="success"))
    session.add_all([make_job("in-window", 0.9, age_hours=29), make_job("too-old", 0.8, age_hours=40)])
    await session.commit()

    assert [job.title for job in await get_daily_jobs(session, settings, now=NOW)] == ["in-window"]
