from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import sessionmaker

from .collector import SPXCollector
from .config import Settings

LOGGER = logging.getLogger(__name__)


def run_once(settings: Settings, session_factory: sessionmaker) -> None:
    collector = SPXCollector(settings)
    with session_factory() as db_session:
        inserted = asyncio.run(collector.run_snapshot(db_session))
    LOGGER.info(
        "Inserted %s snapshot rows at %s.",
        inserted,
        datetime.now(tz=UTC).isoformat(),
    )


def start_scheduler(settings: Settings, session_factory: sessionmaker) -> None:
    scheduler = BlockingScheduler(timezone="America/New_York")
    trigger = CronTrigger(minute="*/15")
    scheduler.add_job(run_once, trigger=trigger, kwargs={"settings": settings, "session_factory": session_factory})

    LOGGER.info("Scheduler started: collecting every 15 minutes.")
    scheduler.start()
