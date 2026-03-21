#!/usr/bin/env python3
from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.spx_collector import backtest_staging as bs


DB_PATH = REPO_ROOT / "spx_options.db"


@dataclass
class LegConfig:
    side: str
    option_type: str
    dte: int
    target_delta: float
    entry_time: str
    quantity: int = 1


def load_trade_points() -> tuple[list[tuple[datetime, float]], float]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        symbol = "SPX"
        trade_date = date.fromisoformat("2026-03-05")
        legs = [
            LegConfig(side="BUY", option_type="PUT", dte=1, target_delta=10.0, entry_time="10:30"),
            LegConfig(side="SELL", option_type="PUT", dte=1, target_delta=25.0, entry_time="10:30"),
        ]
        resolved: list[dict[str, object]] = []
        for leg in legs:
            payload = bs._run_resolve_leg_payload(
                conn,
                symbol=symbol,
                option_type=leg.option_type,
                dte=leg.dte,
                target_delta=leg.target_delta,
                entry_time=leg.entry_time,
                entry_date=trade_date,
                target_side=leg.side,
                window_minutes=5,
                best_only=True,
                strict_dte=True,
            )
            resolved.append(
                {
                    "side": leg.side,
                    "sign": -1 if leg.side == "SELL" else 1,
                    "streamer_symbol": payload["streamer_symbol"],
                    "entry_snapshot_ts": payload["snapshot_ts"],
                }
            )

        streamers = [str(item["streamer_symbol"]) for item in resolved]
        series_rows = [
            dict(row)
            for row in bs._run_series_payload(
                conn,
                symbol=symbol,
                streamers=streamers,
                start_dt=bs._parse_datetime("2026-03-04T00:00:00", "from"),
                end_dt=bs._parse_datetime("2026-03-06T23:59:59", "to"),
                field="mid_price",
            )["rows"]
        ]

        rows_by_streamer: dict[str, list[dict[str, object]]] = {streamer: [] for streamer in streamers}
        for row in series_rows:
            rows_by_streamer[str(row["streamer_symbol"])].append(row)
        for streamer in streamers:
            rows_by_streamer[streamer].sort(key=lambda row: str(row["snapshot_ts"]))

        row_by_streamer_ts = {
            streamer: {str(row["snapshot_ts"]): row for row in rows}
            for streamer, rows in rows_by_streamer.items()
        }

        for leg in resolved:
            streamer = str(leg["streamer_symbol"])
            entry_ts = str(leg["entry_snapshot_ts"])
            entry_row = next(row for row in rows_by_streamer[streamer] if str(row["snapshot_ts"]) == entry_ts)
            leg["entry_value"] = float(entry_row["value"])
            leg["entry_ts"] = datetime.fromisoformat(entry_ts)

        trade_start = max(leg["entry_ts"] for leg in resolved)
        entry_strategy_cost = sum(int(leg["sign"]) * float(leg["entry_value"]) for leg in resolved)
        all_timestamps = sorted({str(row["snapshot_ts"]) for row in series_rows})
        points: list[tuple[datetime, float]] = []
        for ts in all_timestamps:
            current = datetime.fromisoformat(ts)
            if current < trade_start:
                continue
            strategy_value = 0.0
            complete = True
            for leg in resolved:
                row = row_by_streamer_ts[str(leg["streamer_symbol"])].get(ts)
                if row is None or row["value"] is None:
                    complete = False
                    break
                strategy_value += int(leg["sign"]) * float(row["value"])
            if not complete or entry_strategy_cost == 0:
                continue
            points.append((current, (strategy_value / entry_strategy_cost) * 100.0))
        return points, entry_strategy_cost
    finally:
        conn.close()


def chart_terminal_point(points: list[tuple[datetime, float]]) -> tuple[datetime, float]:
    step = timedelta(minutes=15)
    entry_dt = points[0][0]
    normalized = [(ts - entry_dt, indexed) for ts, indexed in points]
    max_elapsed = normalized[-1][0]
    max_step = math.ceil(max_elapsed / step)
    terminal_step = entry_dt + (max_step * step)
    terminal_value = normalized[-1][1]
    return terminal_step, terminal_value


def hover_percent(indexed: float, strategy_cost: float) -> float:
    raw_delta = indexed - 100.0
    return -raw_delta if strategy_cost < 0 else raw_delta


def main() -> int:
    points, strategy_cost = load_trade_points()
    actual_final_ts, actual_final_indexed = points[-1]
    chart_final_ts, chart_final_indexed = chart_terminal_point(points)
    actual_hover = hover_percent(actual_final_indexed, strategy_cost)
    chart_hover = hover_percent(chart_final_indexed, strategy_cost)

    print(f"actual_final_ts={actual_final_ts.isoformat(sep=' ')}")
    print(f"actual_final_indexed={actual_final_indexed:.6f}")
    print(f"actual_hover_pct={actual_hover:.6f}")
    print(f"chart_final_ts={chart_final_ts.isoformat(sep=' ')}")
    print(f"chart_final_indexed={chart_final_indexed:.6f}")
    print(f"chart_hover_pct={chart_hover:.6f}")

    if not math.isclose(chart_final_indexed, actual_final_indexed, rel_tol=0.0, abs_tol=1e-9):
        raise SystemExit("chart terminal point does not match actual final point")
    if not math.isclose(chart_hover, actual_hover, rel_tol=0.0, abs_tol=1e-9):
        raise SystemExit("chart hover percent does not match actual final return")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
