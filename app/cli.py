"""Standalone CLI, e.g. for cron: `python -m app.cli sync`."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from app.core.config import Settings, get_settings
from app.core.database import configure_engine, dispose_engine, get_sessionmaker, init_db
from app.services.cv_parser import CVParseError, build_profile
from app.services.matcher import get_matcher
from app.services.sync import NoProfileError, SyncInProgressError, get_daily_jobs, run_sync, save_profile

console = Console()


async def cmd_sync(settings: Settings) -> int:
    matcher = await asyncio.to_thread(get_matcher)
    try:
        result = await run_sync(get_sessionmaker(), matcher, settings)
    except (NoProfileError, SyncInProgressError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    table = Table(title=f"Sync run #{result.run_id}: {result.status}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Fetched (unique)", str(result.fetched))
    table.add_row("New positions", str(result.new))
    table.add_row(f"Matched (score >= {settings.similarity_threshold})", str(result.matched))
    table.add_row("Descriptions fetched", str(result.enriched))
    table.add_row("Duration", f"{result.duration_seconds}s")
    console.print(table)
    for name, status in result.sources.items():
        color = "green" if status.startswith("ok") else "yellow"
        console.print(f"  [{color}]{name}[/{color}]: {status}")
    return 2 if result.status == "failed" else 0


async def cmd_daily(settings: Settings) -> int:
    async with get_sessionmaker()() as session:
        jobs = await get_daily_jobs(session, settings)
    if not jobs:
        console.print("No matches in the current 24h cycle. Run [bold]python -m app.cli sync[/bold] first.")
        return 0
    table = Table(title=f"Top {len(jobs)} jobs today", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Job ID / URL", overflow="fold")
    for rank, job in enumerate(jobs, 1):
        marker = " ★" if job.is_bookmarked else ""
        table.add_row(str(rank), f"{job.relevance_score:.2f}", job.title + marker, job.company, job.location,
                      f"{job.id}\n{job.url}")
    console.print(table)
    return 0


async def cmd_upload_cv(settings: Settings, path: Path) -> int:
    try:
        data = path.read_bytes()
        profile = await asyncio.to_thread(build_profile, data)
    except (OSError, CVParseError) as exc:
        console.print(f"[red]Could not read CV: {exc}[/red]")
        return 1
    matcher = await asyncio.to_thread(get_matcher)
    async with get_sessionmaker()() as session:
        await save_profile(session, matcher, profile, path.name)
    console.print(f"[green]Active profile updated from {path.name}[/green]")
    console.print(f"  Roles:      {', '.join(profile.roles)}")
    console.print(f"  Seniority:  {profile.seniority or 'unknown'} ({profile.years_experience or '?'} years)")
    console.print(f"  Skills:     {', '.join(profile.skills) or 'none detected'}")
    return 0


async def _run(handler: Callable[[Settings], Awaitable[int]]) -> int:
    settings = get_settings()
    configure_engine(settings.database_url)
    await init_db()
    try:
        return await handler(settings)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Local job matcher")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="fetch -> score -> store new jobs (cron-friendly exit codes)")
    commands.add_parser("daily", help="show today's top matches")
    upload = commands.add_parser("upload-cv", help="parse a CV PDF and make it the active profile")
    upload.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    handlers: dict[str, Callable[[Settings], Awaitable[int]]] = {
        "sync": cmd_sync,
        "daily": cmd_daily,
        "upload-cv": lambda settings: cmd_upload_cv(settings, args.path),
    }
    try:
        return asyncio.run(_run(handlers[args.command]))
    except Exception:
        console.print_exception()
        return 1


if __name__ == "__main__":
    sys.exit(main())
