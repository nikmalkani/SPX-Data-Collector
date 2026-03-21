import tempfile
import unittest
from datetime import date
from pathlib import Path

from spx_collector.tracking import (
    build_common_legs_payload,
    build_overview_payload,
    build_recent_runs_payload,
    build_timeseries_payload,
    ensure_tracking_db,
    insert_tracking_event,
    validate_tracking_payload,
)


class TrackingModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "site_tracking.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self.tracking_db = ensure_tracking_db(self.db_url)

    def _insert(self, event_name, occurred_at, **kwargs):
        payload = {
            "event_name": event_name,
            "event_version": 1,
            "anonymous_id": kwargs.pop("anonymous_id", "anon-1"),
            "session_id": kwargs.pop("session_id", "sess-1"),
            "page_path": kwargs.pop("page_path", "/"),
            "occurred_at": occurred_at,
            "data": kwargs.pop("data", {}),
        }
        if "outcome" in kwargs:
            payload["outcome"] = kwargs.pop("outcome")
        if "referrer_host" in kwargs:
            payload["referrer_host"] = kwargs.pop("referrer_host")
        self.assertFalse(kwargs, f"unexpected kwargs: {kwargs}")
        insert_tracking_event(self.tracking_db, payload)

    def test_validate_tracking_payload_rejects_unknown_event(self):
        with self.assertRaises(ValueError):
            validate_tracking_payload(
                {
                    "event_name": "unknown_event",
                    "event_version": 1,
                    "anonymous_id": "anon-1",
                    "session_id": "sess-1",
                    "page_path": "/",
                    "occurred_at": "2026-03-21T10:00:00Z",
                    "data": {},
                }
            )

    def test_build_metrics_payloads(self):
        self._insert("page_view", "2026-03-20T14:00:00Z")
        self._insert(
            "strategy_leg_add_attempt",
            "2026-03-20T14:05:00Z",
            data={
                "symbol": "SPX",
                "option_type": "PUT",
                "target_delta": 35,
                "target_dte": 1,
                "entry_time": "10:30",
            },
        )
        self._insert(
            "strategy_leg_add_result",
            "2026-03-20T14:05:01Z",
            outcome="success",
            data={
                "symbol": "SPX",
                "option_type": "PUT",
                "target_delta": 35,
                "target_dte": 1,
                "entry_time": "10:30",
            },
        )
        self._insert(
            "strategy_run_attempt",
            "2026-03-20T14:06:00Z",
            data={
                "symbol": "SPX",
                "legs": [
                    {
                        "side": "BUY",
                        "option_type": "PUT",
                        "target_delta": 35,
                        "target_dte": 1,
                        "quantity": 1,
                        "entry_time": "10:30",
                    }
                ],
                "snapshot_from_date": "2026-03-10",
                "snapshot_to_date": "2026-03-20",
                "hold_till_expiry": True,
            },
        )
        self._insert(
            "strategy_run_result",
            "2026-03-20T14:06:10Z",
            outcome="success",
            data={
                "symbol": "SPX",
                "legs": [
                    {
                        "side": "BUY",
                        "option_type": "PUT",
                        "target_delta": 35,
                        "target_dte": 1,
                        "quantity": 1,
                        "entry_time": "10:30",
                    }
                ],
                "snapshot_from_date": "2026-03-10",
                "snapshot_to_date": "2026-03-20",
                "hold_till_expiry": True,
                "trade_dates_count": 5,
                "trade_plan_count": 4,
                "completed_trade_count": 4,
                "completed_contract_count": 4,
                "skipped_dates": 1,
            },
        )
        self._insert(
            "page_view",
            "2026-03-21T16:00:00Z",
            anonymous_id="anon-2",
            session_id="sess-2",
        )

        overview = build_overview_payload(
            self.tracking_db,
            from_date=date.fromisoformat("2026-03-20"),
            to_date=date.fromisoformat("2026-03-21"),
        )
        self.assertEqual(overview["pageviews"], 2)
        self.assertEqual(overview["sessions"], 2)
        self.assertEqual(overview["unique_visitors"], 2)
        self.assertEqual(overview["add_leg_attempts"], 1)
        self.assertEqual(overview["add_leg_successes"], 1)
        self.assertEqual(overview["run_attempts"], 1)
        self.assertEqual(overview["run_successes"], 1)

        timeseries = build_timeseries_payload(
            self.tracking_db,
            from_date=date.fromisoformat("2026-03-20"),
            to_date=date.fromisoformat("2026-03-21"),
        )
        self.assertEqual(len(timeseries["rows"]), 2)
        self.assertEqual(timeseries["rows"][0]["add_leg_attempts"], 1)
        self.assertEqual(timeseries["rows"][0]["run_attempts"], 1)
        self.assertEqual(timeseries["rows"][1]["unique_visitors"], 1)

        recent_runs = build_recent_runs_payload(
            self.tracking_db,
            from_date=date.fromisoformat("2026-03-20"),
            to_date=date.fromisoformat("2026-03-21"),
        )
        self.assertEqual(len(recent_runs["rows"]), 1)
        self.assertEqual(recent_runs["rows"][0]["outcome"], "success")
        self.assertIn("BUY PUT", recent_runs["rows"][0]["leg_labels"][0])

        common_legs = build_common_legs_payload(
            self.tracking_db,
            from_date=date.fromisoformat("2026-03-20"),
            to_date=date.fromisoformat("2026-03-21"),
        )
        self.assertEqual(common_legs["rows"][0]["count"], 1)
        self.assertIn("BUY PUT", common_legs["rows"][0]["label"])


if __name__ == "__main__":
    unittest.main()
