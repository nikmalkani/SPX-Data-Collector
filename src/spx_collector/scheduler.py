from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import sessionmaker

from .collector import SPXCollector
from .config import Settings

LOGGER = logging.getLogger(__name__)
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
WINDOW_START = time(hour=6, minute=0)
WINDOW_END = time(hour=14, minute=0)


def is_collection_window_open(now_utc: datetime) -> bool:
    now_pacific = now_utc.astimezone(PACIFIC_TZ)
    # Monday=0 ... Sunday=6
    is_weekday = now_pacific.weekday() < 5
    in_window = WINDOW_START <= now_pacific.time().replace(tzinfo=None) < WINDOW_END
    return is_weekday and in_window


def run_once(settings: Settings, session_factory: sessionmaker) -> None:
    now_utc = datetime.now(tz=UTC)
    if not is_collection_window_open(now_utc):
        LOGGER.info(
            "Outside collection window at %s (Pacific %s), skipping snapshot.",
            now_utc.isoformat(),
            now_utc.astimezone(PACIFIC_TZ).isoformat(),
        )
        return

    collector = SPXCollector(settings)
    with session_factory() as db_session:
        inserted = asyncio.run(collector.run_snapshot(db_session))
    LOGGER.info(
        "Inserted %s snapshot rows at %s.",
        inserted,
        datetime.now(tz=UTC).isoformat(),
    )


def start_scheduler(settings: Settings, session_factory: sessionmaker) -> None:
    scheduler = BlockingScheduler(timezone="America/Los_Angeles")
    trigger = CronTrigger(day_of_week="mon-fri", hour="6-13", minute="*/15")
    scheduler.add_job(
        run_once,
        trigger=trigger,
        kwargs={"settings": settings, "session_factory": session_factory},
    )

    LOGGER.info(
        "Scheduler started: collecting weekdays every 15 minutes, 06:00-14:00 Pacific."
    )
    scheduler.start()
