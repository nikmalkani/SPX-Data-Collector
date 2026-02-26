from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session
from tastytrade import Session as TastytradeSession
from tastytrade.market_data import MarketData, get_market_data
from tastytrade.metrics import MarketMetricInfo, get_market_metrics
from tastytrade.order import InstrumentType

from .config import Settings
from .models import SPXMarketSnapshot

LOGGER = logging.getLogger(__name__)


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
        market_data = await self._get_market_data(tt_session, snapshot_id=snapshot_id)
        return self._resolve_spot_price(market_data, snapshot_id=snapshot_id)

    # Main collection path: fetch SPX market-data + metrics, then persist one row.
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
        market_data = await self._get_market_data(tt_session, snapshot_id=snapshot_id)
        spot = self._resolve_spot_price(market_data, snapshot_id=snapshot_id)
        metric = await self._get_market_metric(tt_session, snapshot_id=snapshot_id)

        row = SPXMarketSnapshot(
            snapshot_ts=snapshot_ts,
            symbol=symbol,
            spot_price=spot,
            bid_price=_to_float(getattr(market_data, "bid", None)),
            ask_price=_to_float(getattr(market_data, "ask", None)),
            last_price=_to_float(getattr(market_data, "last", None)),
            mark_price=_to_float(getattr(market_data, "mark", None)),
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

        try:
            db_session.add(row)
            db_session.commit()
        except Exception as exc:
            context = {
                "snapshot_id": snapshot_id,
                "symbol": symbol,
                "spot_price": spot,
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
            "snapshot_id=%s stage=db_commit_success symbol=%s spot=%.6f",
            snapshot_id,
            symbol,
            spot,
        )
        return 1

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
        snapshot_id: str,
    ) -> MarketData:
        symbol = self.settings.underlying_symbol
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
        snapshot_id: str,
    ) -> MarketMetricInfo | None:
        symbol = self.settings.underlying_symbol
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
    def _resolve_spot_price(self, market_data: MarketData, *, snapshot_id: str) -> float:
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
            "symbol": self.settings.underlying_symbol,
            "market_data_updated_at": getattr(market_data, "updated_at", None),
        }
        LOGGER.error(
            "snapshot_id=%s stage=spot_resolution_failed symbol=%s",
            snapshot_id,
            self.settings.underlying_symbol,
        )
        raise SpotPriceResolutionError(
            "Unable to determine SPX spot from market-data payload.", context=context
        )


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


# Correlation id across logs for one collector run.
def _snapshot_id(snapshot_ts: datetime) -> str:
    return f"{snapshot_ts.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
