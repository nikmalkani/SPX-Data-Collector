from datetime import UTC, datetime
import unittest

from spx_collector.scheduler import is_collection_window_open


class SchedulerWindowTests(unittest.TestCase):
    def test_weekday_inside_window_open(self):
        # Monday 2026-03-02 06:00:00 PST == 14:00:00 UTC
        now_utc = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
        self.assertTrue(is_collection_window_open(now_utc))

    def test_weekday_before_window_closed(self):
        # Monday 05:59 PST
        now_utc = datetime(2026, 3, 2, 13, 59, tzinfo=UTC)
        self.assertFalse(is_collection_window_open(now_utc))

    def test_weekday_window_end_closed(self):
        # Monday 14:00 PST
        now_utc = datetime(2026, 3, 2, 22, 0, tzinfo=UTC)
        self.assertFalse(is_collection_window_open(now_utc))

    def test_weekend_inside_hours_closed(self):
        # Saturday 10:00 PST
        now_utc = datetime(2026, 3, 7, 18, 0, tzinfo=UTC)
        self.assertFalse(is_collection_window_open(now_utc))


if __name__ == "__main__":
    unittest.main()
