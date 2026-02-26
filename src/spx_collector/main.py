from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .collector import SPXCollector
from .config import Settings
from .db import build_session_factory
from .scheduler import run_once, start_scheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spx-collector",
        description="Collect SPX spot + market metrics snapshots from tastytrade.",
    )
    parser.add_argument(
        "mode",
        choices=["run-once", "daemon", "diagnose-spot"],
        help=(
            "run-once: collect one snapshot now; "
            "daemon: schedule every 15 minutes; "
            "diagnose-spot: authenticate and resolve underlying spot with detailed logs."
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings()
    log_level_name = settings.collector_log_level.upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO
        log_level_name = "INFO"
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger(__name__).info("Using log level: %s", log_level_name)

    try:
        if args.mode == "run-once":
            session_factory = build_session_factory(settings.db_url)
            run_once(settings, session_factory)
        elif args.mode == "daemon":
            session_factory = build_session_factory(settings.db_url)
            start_scheduler(settings, session_factory)
        else:
            collector = SPXCollector(settings)
            spot = asyncio.run(collector.diagnose_spot())
            logging.getLogger(__name__).info("diagnose_spot_success spot=%.6f", spot)
    except Exception:
        logging.exception("Collector failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
