import asyncio
import unittest
from datetime import UTC, datetime

from spx_collector.collector import SPXCollector, SpotPriceResolutionError, _to_float
from spx_collector.config import Settings
from spx_collector.db import build_session_factory
from spx_collector.models import SPXMarketSnapshot, SPXOptionSnapshot


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self, mode="python"):
        return dict(self.__dict__)


class _OptType:
    def __init__(self, name):
        self.name = name


class _StubCollector(SPXCollector):
    def _build_tastytrade_session(self, snapshot_id: str):  # type: ignore[override]
        return object()

    async def _get_market_data(self, tt_session, *, symbol: str, snapshot_id: str):  # type: ignore[override]
        last = 6900.5 if symbol == "SPX" else 18.5
        return _Obj(
            symbol=symbol,
            updated_at=datetime.now(tz=UTC),
            bid=last - 0.2,
            ask=last + 0.2,
            last=last,
            mark=last + 0.1,
            mid=last,
        )

    async def _get_market_metric(self, tt_session, *, symbol: str, snapshot_id: str):  # type: ignore[override]
        return _Obj(
            symbol=symbol,
            updated_at=datetime.now(tz=UTC),
            implied_volatility_index=18.2,
            implied_volatility_30_day=17.8,
            historical_volatility_30_day=15.1,
        )

    async def _select_options_without_spot(self, tt_session, *, snapshot_id: str):  # type: ignore[override]
        return [
            _Obj(
                streamer_symbol=".SPXW260301P06900000",
                expiration_date=datetime(2026, 3, 1, tzinfo=UTC).date(),
                strike_price=6900.0,
                option_type=_OptType("PUT"),
            )
        ]

    async def _stream_option_events(self, tt_session, *, selected_options, snapshot_id: str):  # type: ignore[override]
        return {
            ".SPXW260301P06900000": {
                "quote": _Obj(bid_price=10.0, ask_price=12.0),
                "greeks": _Obj(
                    volatility=0.2,
                    delta=0.55,
                    gamma=0.02,
                    theta=-0.1,
                    vega=0.15,
                ),
            }
        }


class _NoSpotCollector(_StubCollector):
    async def _get_market_data(self, tt_session, *, symbol: str, snapshot_id: str):  # type: ignore[override]
        return _Obj(symbol=symbol, updated_at=datetime.now(tz=UTC), bid=None, ask=None, last=None, mark=None, mid=None)


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
            self.assertEqual(inserted, 2)
            self.assertEqual(db_session.query(SPXMarketSnapshot).count(), 1)
            row = db_session.query(SPXMarketSnapshot).filter_by(symbol="SPX").one()
            opt = db_session.query(SPXOptionSnapshot).one()
            self.assertEqual(row.symbol, "SPX")
            self.assertEqual(row.spot_price, 6900.5)
            self.assertEqual(row.bid_price, 6900.3)
            self.assertEqual(row.ask_price, 6900.7)
            self.assertEqual(row.implied_volatility_index, 18.2)
            self.assertEqual(opt.streamer_symbol, ".SPXW260301P06900000")
            self.assertEqual(opt.option_type, "PUT")
            self.assertEqual(opt.mid_price, 11.0)
            self.assertEqual(opt.delta, 0.55)

    def test_resolve_spot_raises_with_empty_payload(self):
        settings = Settings(_env_file=None)
        collector = _NoSpotCollector(settings)
        with self.assertRaises(SpotPriceResolutionError):
            asyncio.run(collector.diagnose_spot())


if __name__ == "__main__":
    unittest.main()
