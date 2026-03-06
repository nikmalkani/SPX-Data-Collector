from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from tastytrade.dxfeed import Greeks, Quote
from tastytrade.instruments import Option, OptionType, get_option_chain
from tastytrade import Session as TastytradeSession
from tastytrade.market_data import MarketData, get_market_data
from tastytrade.metrics import MarketMetricInfo, get_market_metrics
from tastytrade.order import InstrumentType
from tastytrade.streamer import DXLinkStreamer

from .config import Settings
from .models import SPXMarketSnapshot, SPXOptionSnapshot

LOGGER = logging.getLogger(__name__)
_EASTERN_TZ = ZoneInfo("America/New_York")


# Base structured error for stage-specific failures.
class CollectorStageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.context = context or {}
        details = " ".join(f"{k}={v}" for k, v in sorted(self.context.items()))
        full = f"[{stage}] {message}"
        if details:
            full = f"{full}; {details}"
        super().__init__(full)


class SpotPriceResolutionError(CollectorStageError):
    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message, stage="spot_resolution", context=context)


class SnapshotPersistenceError(CollectorStageError):
    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message, stage="snapshot_persistence", context=context)


class OptionChainSelectionError(CollectorStageError):
    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message, stage="option_chain_selection", context=context)


class SPXCollector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # Spot-only check path used by `spx-collector diagnose-spot`.
    async def diagnose_spot(self) -> float:
        snapshot_id = _snapshot_id(datetime.now(tz=UTC))
        LOGGER.info(
            "snapshot_id=%s stage=snapshot_start mode=diagnose_spot symbol=%s",
            snapshot_id,
            self.settings.underlying_symbol,
        )
        tt_session = self._build_tastytrade_session(snapshot_id=snapshot_id)
        symbol = self.settings.underlying_symbol
        market_data = await self._get_market_data(
            tt_session, symbol=symbol, snapshot_id=snapshot_id
        )
        return self._resolve_spot_price(
            market_data, symbol=symbol, snapshot_id=snapshot_id
        )

    # Main collection path: fetch SPX market-data + metrics + options, then persist rows.
    async def run_snapshot(self, db_session: Session) -> int:
        snapshot_ts = datetime.now(tz=UTC)
        snapshot_id = _snapshot_id(snapshot_ts)
        symbol = self.settings.underlying_symbol
        LOGGER.info(
            "snapshot_id=%s stage=snapshot_start symbol=%s",
            snapshot_id,
            symbol,
        )

        tt_session = self._build_tastytrade_session(snapshot_id=snapshot_id)
        market_rows: list[SPXMarketSnapshot] = []
        underlying_spot: float | None = None
        for market_symbol in self._market_symbols():
            market_data = await self._get_market_data(
                tt_session, symbol=market_symbol, snapshot_id=snapshot_id
            )
            spot = self._resolve_spot_price(
                market_data, symbol=market_symbol, snapshot_id=snapshot_id
            )
            metric = await self._get_market_metric(
                tt_session, symbol=market_symbol, snapshot_id=snapshot_id
            )
            market_rows.append(
                SPXMarketSnapshot(
                    snapshot_ts=snapshot_ts,
                    symbol=market_symbol,
                    spot_price=spot,
                    bid_price=_to_float(getattr(market_data, "bid", None)),
                    ask_price=_to_float(getattr(market_data, "ask", None)),
                    last_price=_to_float(getattr(market_data, "last", None)),
                    market_data_updated_at=getattr(market_data, "updated_at", None),
                    metrics_updated_at=getattr(metric, "updated_at", None),
                    implied_volatility_index=_to_float(
                        getattr(metric, "implied_volatility_index", None)
                    ),
                    implied_volatility_30_day=_to_float(
                        getattr(metric, "implied_volatility_30_day", None)
                    ),
                    historical_volatility_30_day=_to_float(
                        getattr(metric, "historical_volatility_30_day", None)
                    ),
                )
            )
            if market_symbol == symbol:
                underlying_spot = spot

        if underlying_spot is None:
            raise SpotPriceResolutionError(
                "Unable to determine underlying spot for options selection.",
                context={"snapshot_id": snapshot_id, "symbol": symbol},
            )
        option_rows = await self._collect_option_rows(
            tt_session=tt_session,
            snapshot_id=snapshot_id,
            snapshot_ts=snapshot_ts,
            symbol=symbol,
        )

        try:
            db_session.add_all(market_rows)
            if option_rows:
                db_session.add_all(option_rows)
            db_session.commit()
        except Exception as exc:
            context = {
                "snapshot_id": snapshot_id,
                "symbol": symbol,
                "spot_price": underlying_spot,
                "market_rows": len(market_rows),
                "option_rows": len(option_rows),
                "error": exc.__class__.__name__,
            }
            LOGGER.exception(
                "snapshot_id=%s stage=snapshot_persistence_error symbol=%s",
                snapshot_id,
                symbol,
            )
            raise SnapshotPersistenceError(
                "Failed to persist SPX market snapshot.", context=context
            ) from exc

        LOGGER.info(
            "snapshot_id=%s stage=db_commit_success symbol=%s spot=%.6f market_rows=%s option_rows=%s",
            snapshot_id,
            symbol,
            underlying_spot,
            len(market_rows),
            len(option_rows),
        )
        return len(market_rows) + len(option_rows)

    # Options-only path for rate-limit diagnostics without market-data requests.
    async def run_options_only(self, db_session: Session) -> int:
        snapshot_ts = datetime.now(tz=UTC)
        snapshot_id = _snapshot_id(snapshot_ts)
        symbol = self.settings.underlying_symbol
        LOGGER.info(
            "snapshot_id=%s stage=options_only_start symbol=%s",
            snapshot_id,
            symbol,
        )

        tt_session = self._build_tastytrade_session(snapshot_id=snapshot_id)
        option_rows = await self._collect_option_rows(
            tt_session=tt_session,
            snapshot_id=snapshot_id,
            snapshot_ts=snapshot_ts,
            symbol=symbol,
        )

        try:
            if option_rows:
                db_session.add_all(option_rows)
            db_session.commit()
        except Exception as exc:
            context = {
                "snapshot_id": snapshot_id,
                "symbol": symbol,
                "option_rows": len(option_rows),
                "error": exc.__class__.__name__,
            }
            LOGGER.exception(
                "snapshot_id=%s stage=options_only_persistence_error symbol=%s",
                snapshot_id,
                symbol,
            )
            raise SnapshotPersistenceError(
                "Failed to persist SPX option snapshots.", context=context
            ) from exc

        LOGGER.info(
            "snapshot_id=%s stage=options_only_commit_success symbol=%s option_rows=%s",
            snapshot_id,
            symbol,
            len(option_rows),
        )
        return len(option_rows)

    # Shared options pipeline used by both run_snapshot and run_options_only.
    async def _collect_option_rows(
        self,
        *,
        tt_session: TastytradeSession,
        snapshot_id: str,
        snapshot_ts: datetime,
        symbol: str,
    ) -> list[SPXOptionSnapshot]:
        selected_options = await self._select_options_without_spot(
            tt_session, snapshot_id=snapshot_id
        )
        option_events = await self._stream_option_events(
            tt_session, selected_options=selected_options, snapshot_id=snapshot_id
        )
        return self._build_option_rows(
            snapshot_ts=snapshot_ts,
            symbol=symbol,
            selected_options=selected_options,
            option_events=option_events,
        )

    # OAuth-only auth path.
    def _build_tastytrade_session(self, snapshot_id: str) -> TastytradeSession:
        secret = self.settings.tastytrade_client_secret
        refresh = self.settings.tastytrade_refresh_token
        if secret and refresh:
            LOGGER.info(
                "snapshot_id=%s stage=auth_ready auth_mode=oauth",
                snapshot_id,
            )
            return TastytradeSession(secret, refresh)

        raise ValueError(
            "Missing credentials: set both "
            "TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN."
        )

    # Pull one SPX market-data payload from tastytrade REST API.
    async def _get_market_data(
        self,
        tt_session: TastytradeSession,
        *,
        symbol: str,
        snapshot_id: str,
    ) -> MarketData:
        LOGGER.info(
            "snapshot_id=%s stage=market_data_request symbol=%s",
            snapshot_id,
            symbol,
        )
        market_data = await get_market_data(tt_session, symbol, InstrumentType.INDEX)
        LOGGER.info(
            "snapshot_id=%s stage=market_data_loaded symbol=%s updated_at=%s",
            snapshot_id,
            symbol,
            getattr(market_data, "updated_at", None),
        )
        return market_data

    # Pull one SPX market-metrics payload from tastytrade REST API.
    async def _get_market_metric(
        self,
        tt_session: TastytradeSession,
        *,
        symbol: str,
        snapshot_id: str,
    ) -> MarketMetricInfo | None:
        LOGGER.info(
            "snapshot_id=%s stage=market_metrics_request symbol=%s",
            snapshot_id,
            symbol,
        )
        metrics = await get_market_metrics(tt_session, [symbol])
        metric = metrics[0] if metrics else None
        if metric is None:
            LOGGER.warning(
                "snapshot_id=%s stage=market_metrics_missing symbol=%s",
                snapshot_id,
                symbol,
            )
        else:
            LOGGER.info(
                "snapshot_id=%s stage=market_metrics_loaded symbol=%s updated_at=%s",
                snapshot_id,
                symbol,
                getattr(metric, "updated_at", None),
            )
        return metric

    # Determine spot from most useful available price fields.
    def _resolve_spot_price(
        self, market_data: MarketData, *, symbol: str, snapshot_id: str
    ) -> float:
        for field in ("last", "mark", "mid", "bid", "ask"):
            value = _to_float(getattr(market_data, field, None))
            if value is not None and value > 0:
                LOGGER.info(
                    "snapshot_id=%s stage=spot_resolved field=%s spot=%.6f",
                    snapshot_id,
                    field,
                    value,
                )
                return value

        context = {
            "snapshot_id": snapshot_id,
            "symbol": symbol,
            "market_data_updated_at": getattr(market_data, "updated_at", None),
        }
        LOGGER.error(
            "snapshot_id=%s stage=spot_resolution_failed symbol=%s",
            snapshot_id,
            symbol,
        )
        raise SpotPriceResolutionError(
            "Unable to determine SPX spot from market-data payload.", context=context
        )

    async def _select_options(
        self,
        tt_session: TastytradeSession,
        *,
        spot: float,
        snapshot_id: str,
    ) -> list[Option]:
        symbol = self.settings.underlying_symbol
        LOGGER.info(
            "snapshot_id=%s stage=option_chain_request symbol=%s",
            snapshot_id,
            symbol,
        )
        chain = await get_option_chain(tt_session, symbol)
        if not chain:
            raise OptionChainSelectionError(
                "Option chain returned no expirations.",
                context={"snapshot_id": snapshot_id, "symbol": symbol},
            )

        expiries = sorted(chain.keys())[: self.settings.option_expiries_per_run]
        selected: list[Option] = []
        for expiry in expiries:
            contracts = chain.get(expiry, [])
            calls = [
                c
                for c in contracts
                if getattr(c, "option_type", None) == OptionType.CALL
                and _to_float(getattr(c, "strike_price", None)) is not None
                and float(c.strike_price) >= spot
            ]
            puts = [
                c
                for c in contracts
                if getattr(c, "option_type", None) == OptionType.PUT
                and _to_float(getattr(c, "strike_price", None)) is not None
                and float(c.strike_price) <= spot
            ]

            calls.sort(key=lambda c: abs(float(c.strike_price) - spot))
            puts.sort(key=lambda c: abs(float(c.strike_price) - spot))
            selected.extend(calls[: self.settings.option_strikes_count])
            selected.extend(puts[: self.settings.option_strikes_count])

        LOGGER.info(
            "snapshot_id=%s stage=options_selected symbol=%s expiries_considered=%s options_selected=%s",
            snapshot_id,
            symbol,
            len(expiries),
            len(selected),
        )
        return selected

    async def _select_options_without_spot(
        self,
        tt_session: TastytradeSession,
        *,
        snapshot_id: str,
    ) -> list[Option]:
        symbol = self.settings.underlying_symbol
        LOGGER.info(
            "snapshot_id=%s stage=option_chain_request_no_spot symbol=%s",
            snapshot_id,
            symbol,
        )
        chain = await get_option_chain(tt_session, symbol)
        if not chain:
            raise OptionChainSelectionError(
                "Option chain returned no expirations.",
                context={"snapshot_id": snapshot_id, "symbol": symbol},
            )

        expiries = sorted(chain.keys())[: self.settings.option_expiries_per_run]
        selected: list[Option] = []
        for expiry in expiries:
            contracts = chain.get(expiry, [])
            puts = [
                c
                for c in contracts
                if getattr(c, "option_type", None) == OptionType.PUT
                and _to_float(getattr(c, "strike_price", None)) is not None
            ]
            puts.sort(key=lambda c: float(c.strike_price))

            count = self.settings.option_strikes_count
            if len(puts) <= count:
                selected_puts = puts
            else:
                # Keep a centered strike window to avoid pulling the full chain.
                start = (len(puts) - count) // 2
                selected_puts = puts[start : start + count]

            selected.extend(selected_puts)

        LOGGER.info(
            "snapshot_id=%s stage=options_selected_no_spot symbol=%s expiries_considered=%s options_selected=%s",
            snapshot_id,
            symbol,
            len(expiries),
            len(selected),
        )
        return selected

    async def _stream_option_events(
        self,
        tt_session: TastytradeSession,
        *,
        selected_options: list[Option],
        snapshot_id: str,
    ) -> dict[str, dict[str, Any]]:
        symbols = [
            s
            for s in [getattr(option, "streamer_symbol", None) for option in selected_options]
            if s
        ]
        if not symbols:
            LOGGER.warning(
                "snapshot_id=%s stage=options_stream_skip reason=no_streamer_symbols",
                snapshot_id,
            )
            return {}

        LOGGER.info(
            "snapshot_id=%s stage=options_stream_subscribed symbols=%s timeout_seconds=%s",
            snapshot_id,
            len(symbols),
            self.settings.options_stream_timeout_seconds,
        )
        quotes: dict[str, Quote] = {}
        greeks: dict[str, Greeks] = {}
        deadline = asyncio.get_running_loop().time() + float(
            self.settings.options_stream_timeout_seconds
        )

        async with DXLinkStreamer(tt_session) as streamer:
            await streamer.subscribe(Quote, symbols)
            await streamer.subscribe(Greeks, symbols)

            while asyncio.get_running_loop().time() < deadline:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                if remaining <= 0:
                    break

                quote = await self._read_event(Quote, streamer, min(0.3, remaining))
                if quote and quote.event_symbol in symbols:
                    quotes[quote.event_symbol] = quote

                greek = await self._read_event(Greeks, streamer, min(0.3, remaining))
                if greek and greek.event_symbol in symbols:
                    greeks[greek.event_symbol] = greek

                if len(quotes) == len(symbols) and len(greeks) == len(symbols):
                    break

        LOGGER.info(
            "snapshot_id=%s stage=options_stream_collected symbols=%s quotes=%s greeks=%s",
            snapshot_id,
            len(symbols),
            len(quotes),
            len(greeks),
        )
        return {
            symbol: {"quote": quotes.get(symbol), "greeks": greeks.get(symbol)}
            for symbol in symbols
        }

    async def _read_event(
        self,
        event_class: type[Quote] | type[Greeks],
        streamer: DXLinkStreamer,
        timeout_seconds: float,
    ) -> Quote | Greeks | None:
        try:
            return await asyncio.wait_for(streamer.get_event(event_class), timeout_seconds)
        except TimeoutError:
            return None

    def _build_option_rows(
        self,
        *,
        snapshot_ts: datetime,
        symbol: str,
        selected_options: list[Option],
        option_events: dict[str, dict[str, Any]],
    ) -> list[SPXOptionSnapshot]:
        rows: list[SPXOptionSnapshot] = []
        for option in selected_options:
            streamer_symbol = getattr(option, "streamer_symbol", None)
            if not streamer_symbol:
                continue

            quote = option_events.get(streamer_symbol, {}).get("quote")
            greek = option_events.get(streamer_symbol, {}).get("greeks")
            bid = _to_float(getattr(quote, "bid_price", None))
            ask = _to_float(getattr(quote, "ask_price", None))
            mid = _mid_price(bid, ask)
            rows.append(
                SPXOptionSnapshot(
                    snapshot_ts=snapshot_ts,
                    symbol=symbol,
                    streamer_symbol=streamer_symbol,
                    expiration_date=option.expiration_date,
                    dte=max(0, (option.expiration_date - snapshot_ts.date()).days),
                    time_in_day_est=snapshot_ts.astimezone(_EASTERN_TZ).strftime("%H:%M"),
                    strike_price=float(option.strike_price),
                    option_type=getattr(option.option_type, "name", str(option.option_type)),
                    bid_price=bid,
                    ask_price=ask,
                    mid_price=mid,
                    volatility=_to_float(getattr(greek, "volatility", None)),
                    delta=_to_float(getattr(greek, "delta", None)),
                    gamma=_to_float(getattr(greek, "gamma", None)),
                    theta=_to_float(getattr(greek, "theta", None)),
                    vega=_to_float(getattr(greek, "vega", None)),
                )
            )

        return rows

    def _market_symbols(self) -> list[str]:
        symbols = [self.settings.underlying_symbol]
        deduped: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            if symbol not in seen:
                deduped.append(symbol)
                seen.add(symbol)
        return deduped


# Safe numeric conversion helper for API values.
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


# Correlation id across logs for one collector run.
def _snapshot_id(snapshot_ts: datetime) -> str:
    return f"{snapshot_ts.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
