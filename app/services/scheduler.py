"""In-process daily sync via APScheduler (3.x)."""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings
from app.core.database import get_sessionmaker
from app.services.matcher import get_matcher
from app.services.sync import NoProfileError, SyncInProgressError, run_sync

logger = logging.getLogger(__name__)

SYNC_JOB_ID = "daily-job-sync"


async def scheduled_sync(settings: Settings) -> None:
    try:
        result = await run_sync(get_sessionmaker(), get_matcher(), settings)
    except NoProfileError:
        logger.warning("Scheduled sync skipped: no CV uploaded yet")
    except SyncInProgressError:
        logger.info("Scheduled sync skipped: another sync is already running")
    except Exception:
        logger.exception("Scheduled sync failed")
    else:
        logger.info("Scheduled sync %s: %d new, %d matched", result.status, result.new, result.matched)


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    extra = {"next_run_time": datetime.now(timezone.utc)} if settings.sync_on_startup else {}
    scheduler.add_job(
        scheduled_sync,
        IntervalTrigger(hours=settings.sync_interval_hours),
        args=[settings],
        id=SYNC_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        **extra,
    )
    return scheduler
