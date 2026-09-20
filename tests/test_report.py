import uuid
from datetime import datetime, timezone

from app.cli import main
from app.core.config import get_settings
from app.models.cv import CandidateProfileRecord
from app.models.job import DailyRun, Job
from app.services.report import SNIPPET_CHARS, render_daily_report

NOW = datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc)


def make_job(**overrides) -> Job:
    fields = {
        "id": uuid.UUID(int=1), "external_id": "jobdatalake:1", "source": "jobdatalake",
        "title": "Senior Backend Engineer", "company": "Ledgerly", "location": "Remote - Europe", "is_remote": True,
        "url": "https://example.com/jobs/1", "description": "Build APIs. " * 100,
        "published_at": datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc), "relevance_score": 0.6234,
        "first_seen_at": NOW,
    }
    return Job(**(fields | overrides))


def test_report_has_profile_run_table_and_details():
    jobs = [
        make_job(is_bookmarked=True),
        make_job(id=uuid.UUID(int=2), external_id="worklittle:2", source="worklittle", title="Dev | Ops [Platform]",
                 company="", location="", is_remote=False, url="", description="", published_at=None,
                 relevance_score=0.5),
    ]
    profile = CandidateProfileRecord(roles=["Senior Full-Stack Engineer"], skills=["Python", "React"],
                                     seniority="senior", years_experience=8.8, summary_blob="unused")
    run = DailyRun(id=3, status="partial", started_at=NOW, jobs_new=42, jobs_matched_count=12,
                   error="worklittle: error: timeout")

    report = render_daily_report(jobs, profile, run, threshold=0.45, generated_at=NOW, tz=timezone.utc)

    assert report.startswith("# Job matches — 2026-09-14\n")
    assert "Generated 2026-09-14 07:30 UTC · 2 jobs with relevance ≥ 0.45" in report
    assert "- **Roles:** Senior Full-Stack Engineer" in report
    assert "- **Seniority:** senior (8.8 years)" in report
    assert "- Run #3 · partial · 2026-09-14 07:30 UTC · 42 new, 12 matched" in report
    assert "- Issues: worklittle: error: timeout" in report
    assert ("| 1 | 0.62 | [Senior Backend Engineer](<https://example.com/jobs/1>) ★ | Ledgerly | Remote - Europe "
            "| 2026-09-13 |") in report
    assert "| 2 | 0.50 | Dev \\| Ops \\[Platform\\] | — | — | — |" in report

    assert "### 1. Senior Backend Engineer — Ledgerly ★" in report
    assert "**Location:** Remote - Europe · **Posted:** 2026-09-13 · **Source:** jobdatalake" in report
    assert "[View posting](<https://example.com/jobs/1>)" in report
    snippet = next(line for line in report.splitlines() if line.startswith("> "))
    assert snippet.startswith("> Build APIs.") and snippet.endswith("…")
    assert len(snippet) <= SNIPPET_CHARS + 3
    assert f"Job ID: `{uuid.UUID(int=1)}`" in report
    assert "### 2. Dev | Ops [Platform]\n" in report


def test_report_without_matches_explains_next_step():
    report = render_daily_report([], None, None, threshold=0.45, generated_at=NOW, tz=timezone.utc)

    assert "0 jobs with relevance ≥ 0.45" in report
    assert "No matches in the current cycle" in report
    assert "## Profile" not in report and "| # |" not in report


def test_cli_daily_writes_report_to_file_or_dated_file_in_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the project's .env (and real API keys) out of this test
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    get_settings.cache_clear()
    try:
        assert main(["daily", "--output", "out/today.md"]) == 0
        report = (tmp_path / "out" / "today.md").read_text(encoding="utf-8")
        assert report.startswith("# Job matches — ")
        assert "No matches in the current cycle" in report

        assert main(["daily", "-o", "reports/"]) == 0
        assert len(list((tmp_path / "reports").glob("jobs-????-??-??.md"))) == 1
    finally:
        get_settings.cache_clear()
