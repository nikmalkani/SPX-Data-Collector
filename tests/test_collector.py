import asyncio
import unittest
from datetime import UTC, datetime

from spx_collector.collector import SPXCollector, SpotPriceResolutionError, _to_float
from spx_collector.config import Settings
from spx_collector.db import build_session_factory
from spx_collector.models import SPXMarketSnapshot


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self, mode="python"):
        return dict(self.__dict__)


class _StubCollector(SPXCollector):
    def _build_tastytrade_session(self, snapshot_id: str):  # type: ignore[override]
        return object()

    async def _get_market_data(self, tt_session, *, snapshot_id: str):  # type: ignore[override]
        return _Obj(
            symbol="SPX",
            updated_at=datetime.now(tz=UTC),
            bid=6900.0,
            ask=6901.0,
            last=6900.5,
            mark=6900.6,
            mid=6900.55,
        )

    async def _get_market_metric(self, tt_session, *, snapshot_id: str):  # type: ignore[override]
        return _Obj(
            symbol="SPX",
            updated_at=datetime.now(tz=UTC),
            implied_volatility_index=18.2,
            implied_volatility_30_day=17.8,
            historical_volatility_30_day=15.1,
        )


class _NoSpotCollector(_StubCollector):
    async def _get_market_data(self, tt_session, *, snapshot_id: str):  # type: ignore[override]
        return _Obj(symbol="SPX", updated_at=datetime.now(tz=UTC), bid=None, ask=None, last=None, mark=None, mid=None)


class CollectorTests(unittest.TestCase):
    def test_to_float(self):
        self.assertEqual(_to_float("12.5"), 12.5)
        self.assertEqual(_to_float(8), 8.0)
        self.assertIsNone(_to_float(None))

    def test_run_snapshot_inserts_one_row(self):
        settings = Settings(_env_file=None)
        collector = _StubCollector(settings)
        session_factory = build_session_factory("sqlite:///:memory:")

        with session_factory() as db_session:
            inserted = asyncio.run(collector.run_snapshot(db_session))
            self.assertEqual(inserted, 1)
            row = db_session.query(SPXMarketSnapshot).one()
            self.assertEqual(row.symbol, "SPX")
            self.assertEqual(row.spot_price, 6900.5)
            self.assertEqual(row.bid_price, 6900.0)
            self.assertEqual(row.ask_price, 6901.0)
            self.assertEqual(row.implied_volatility_index, 18.2)

    def test_resolve_spot_raises_with_empty_payload(self):
        settings = Settings(_env_file=None)
        collector = _NoSpotCollector(settings)
        with self.assertRaises(SpotPriceResolutionError):
            asyncio.run(collector.diagnose_spot())


if __name__ == "__main__":
    unittest.main()
