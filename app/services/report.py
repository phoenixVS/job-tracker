"""Markdown rendering of the daily digest."""

from collections.abc import Sequence
from datetime import datetime, tzinfo

from app.models.cv import CandidateProfileRecord
from app.models.job import DailyRun, Job

SNIPPET_CHARS = 400
MAX_PROFILE_SKILLS = 15


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|") or "—"


def _link_text(text: str) -> str:
    return _one_line(text).replace("[", "\\[").replace("]", "\\]")


def _snippet(description: str) -> str:
    text = _one_line(description)
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _format_time(value: datetime | None, tz: tzinfo | None, with_time: bool = False) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z" if with_time else "%Y-%m-%d")


def _location(job: Job) -> str:
    location = _one_line(job.location)
    if job.is_remote and "remote" not in location.lower():
        return f"{location} (remote)" if location else "Remote"
    return location or "—"


def render_daily_report(
    jobs: Sequence[Job],
    profile: CandidateProfileRecord | None,
    latest_run: DailyRun | None,
    threshold: float,
    generated_at: datetime,
    tz: tzinfo | None = None,
) -> str:
    """The daily digest as Markdown. Times are shown in `tz` (default: the machine's local timezone)."""
    count = f"{len(jobs)} job{'' if len(jobs) == 1 else 's'}"
    lines = [
        f"# Job matches — {_format_time(generated_at, tz)}",
        "",
        f"Generated {_format_time(generated_at, tz, with_time=True)} · {count} with relevance ≥ {threshold:g} "
        "from the latest 24h cycle.",
        "",
    ]

    if profile is not None:
        experience = f" ({profile.years_experience:g} years)" if profile.years_experience else ""
        lines += [
            "## Profile",
            "",
            f"- **Roles:** {', '.join(profile.roles) or '—'}",
            f"- **Seniority:** {profile.seniority or 'unknown'}{experience}",
            f"- **Top skills:** {', '.join(profile.skills[:MAX_PROFILE_SKILLS]) or '—'}",
            "",
        ]

    if latest_run is not None:
        lines += [
            "## Last sync",
            "",
            f"- Run #{latest_run.id} · {latest_run.status} · {_format_time(latest_run.started_at, tz, with_time=True)} "
            f"· {latest_run.jobs_new} new, {latest_run.jobs_matched_count} matched",
        ]
        if latest_run.error:
            lines.append(f"- Issues: {_one_line(latest_run.error)}")
        lines.append("")

    lines += ["## Top matches", ""]
    if not jobs:
        lines.append("No matches in the current cycle. Run `job-matcher sync`, or lower `SIMILARITY_THRESHOLD` if "
                     "syncs find jobs but none pass it.")
        return "\n".join(lines) + "\n"

    lines += ["| # | Score | Role | Company | Location | Posted |", "|---:|---:|---|---|---|---|"]
    for rank, job in enumerate(jobs, 1):
        title = f"[{_link_text(job.title)}](<{job.url}>)" if job.url else _link_text(job.title)
        if job.is_bookmarked:
            title += " ★"
        lines.append(f"| {rank} | {job.relevance_score:.2f} | {_cell(title)} | {_cell(job.company)} | "
                     f"{_cell(_location(job))} | {_format_time(job.published_at, tz)} |")

    lines += ["", "## Details", ""]
    for rank, job in enumerate(jobs, 1):
        heading = f"### {rank}. {_one_line(job.title)}"
        if job.company:
            heading += f" — {_one_line(job.company)}"
        if job.is_bookmarked:
            heading += " ★"
        meta = " · ".join([
            f"**Score:** {job.relevance_score:.2f}",
            f"**Location:** {_location(job)}",
            f"**Posted:** {_format_time(job.published_at, tz)}",
            f"**Source:** {job.source}",
        ])
        lines += [heading, "", meta, ""]
        if job.url:
            lines += [f"[View posting](<{job.url}>)", ""]
        if job.description:
            lines += [f"> {_snippet(job.description)}", ""]
        lines += [f"Job ID: `{job.id}`", ""]
    return "\n".join(lines).rstrip() + "\n"
