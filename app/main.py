import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.database import configure_engine, dispose_engine, init_db
from app.services.matcher import get_matcher
from app.services.scheduler import create_scheduler

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is noise
        configure_engine(settings.database_url)
        await init_db()
        await asyncio.to_thread(get_matcher)  # load the embedding model once, off the event loop
        scheduler = None
        if settings.scheduler_enabled:
            scheduler = create_scheduler(settings)
            scheduler.start()
            logger.info("Scheduler started: sync every %gh (next run %s)", settings.sync_interval_hours,
                        scheduler.get_jobs()[0].next_run_time)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            await dispose_engine()

    app = FastAPI(title="Job Matcher", version="0.1.0", lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(api_router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
