from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import Settings


def _resolve_sqlite_path(db_url: str) -> Path:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError(
            f"SQL UI currently supports sqlite only. DB_URL was: {db_url!r}"
        )
    raw_path = db_url[len(prefix) :]
    return Path(raw_path).expanduser().resolve()


def _safe_query(query: str) -> str:
    q = query.strip()
    if not q:
        raise ValueError("Query is empty.")

    lowered = q.lower()
    if ";" in q[:-1]:
        raise ValueError("Only a single SQL statement is allowed.")

    if not (
        lowered.startswith("select")
        or lowered.startswith("pragma")
        or lowered.startswith("with")
    ):
        raise ValueError("Only SELECT/CTE/PRAGMA queries are allowed.")

    blocked = [
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "attach ",
        "detach ",
        "replace ",
        "truncate ",
    ]
    if any(tok in lowered for tok in blocked):
        raise ValueError("Mutating SQL is blocked in this UI.")

    return q


def _json_response(
    handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200
) -> None:
    data = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    data = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _schema_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [str(t[0]) for t in tables]
    schema: dict[str, list[dict[str, Any]]] = {}
    for table in table_names:
        rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
        schema[table] = [
            {
                "cid": r[0],
                "name": r[1],
                "type": r[2],
                "notnull": r[3],
                "default": r[4],
                "pk": r[5],
            }
            for r in rows
        ]
    return {"tables": table_names, "schema": schema}


def _run_query(conn: sqlite3.Connection, query: str) -> dict[str, Any]:
    safe = _safe_query(query)
    cur = conn.execute(safe)
    col_names = [c[0] for c in (cur.description or [])]
    rows = cur.fetchall()
    values = [[row[i] for i in range(len(col_names))] for row in rows]
    return {"columns": col_names, "rows": values, "row_count": len(values)}


def _parse_datetime(value: str | None, label: str) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = f"{s[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}. Expected ISO datetime.") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(timezone.utc)


def _sqlite_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_float(value: str | None, label: str) -> float | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}.") from exc


def _parse_int(value: str | None, label: str, fallback: int) -> int:
    if value is None:
        return fallback
    s = value.strip()
    if not s:
        return fallback
    try:
        return int(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}.") from exc


def _parse_int_required(value: str | None, label: str) -> int:
    if value is None:
        raise ValueError(f"Missing required {label}.")
    s = value.strip()
    if not s:
        raise ValueError(f"Missing required {label}.")
    try:
        return int(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}.") from exc


def _parse_float_required(value: str | None, label: str) -> float:
    if value is None:
        raise ValueError(f"Missing required {label}.")
    s = value.strip()
    if not s:
        raise ValueError(f"Missing required {label}.")
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}.") from exc


def _parse_date(value: str | None, label: str) -> date | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}. Expected YYYY-MM-DD.") from exc


def _parse_est_hhmm(value: str | None, label: str) -> tuple[int, int]:
    if value is None:
        raise ValueError(f"Missing required {label}.")
    s = value.strip()
    if not s:
        raise ValueError(f"Missing required {label}.")
    try:
        hour_min = datetime.strptime(s, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid {label}. Expected HH:MM.") from exc
    return hour_min.hour, hour_min.minute


def _resolve_latest_option_date(conn: sqlite3.Connection, symbol: str) -> date | None:
    row = conn.execute(
        "SELECT date(MAX(snapshot_ts)) FROM spx_option_snapshots WHERE symbol = ?",
        [symbol],
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return date.fromisoformat(str(row[0]))
    except ValueError:
        return None


def _run_snapshot_dates_payload(conn: sqlite3.Connection, *, symbol: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT DISTINCT date(snapshot_ts) AS snapshot_date FROM spx_option_snapshots WHERE symbol = ? ORDER BY snapshot_date ASC",
        [symbol],
    ).fetchall()
    return {"dates": [str(row[0]) for row in rows if row[0]]}


def _run_resolve_leg_payload(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    option_type: str,
    dte: int,
    target_delta: float,
    entry_time: str,
    entry_date: date | None = None,
    target_side: str | None = None,
    snapshot_from: datetime | None = None,
    snapshot_to: datetime | None = None,
    window_minutes: int = 30,
    strict_dte: bool = False,
) -> dict[str, Any]:
    opt_type = option_type.upper()
    if opt_type not in {"PUT", "CALL"}:
        raise ValueError("option_type must be PUT or CALL.")

    normalized_delta = abs(target_delta)
    if normalized_delta > 1:
        normalized_delta /= 100

    effective_side = target_side.upper() if target_side else None
    if effective_side and effective_side not in {"BUY", "SELL"}:
        raise ValueError("target_side must be BUY or SELL.")

    latest_date = _resolve_latest_option_date(conn, symbol)
    if entry_date is None:
        if latest_date is None:
            raise ValueError("No option snapshots found for this symbol.")
        entry_date = latest_date

    hh, mm = _parse_est_hhmm(entry_time, "entry_time")
    entry_local = datetime(
        year=entry_date.year,
        month=entry_date.month,
        day=entry_date.day,
        hour=hh,
        minute=mm,
        second=0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    entry_utc = entry_local.astimezone(timezone.utc)
    entry_epoch = int(entry_utc.timestamp())

    default_from = entry_utc - timedelta(minutes=window_minutes)
    default_to = entry_utc + timedelta(minutes=window_minutes)
    window_from = _sqlite_timestamp(snapshot_from.astimezone(timezone.utc)) if snapshot_from else _sqlite_timestamp(default_from)
    window_to = _sqlite_timestamp(snapshot_to.astimezone(timezone.utc)) if snapshot_to else _sqlite_timestamp(default_to)

    def query_candidates(dte_min: int, dte_max: int) -> list[Any]:
        rows = conn.execute(
            f"""
        SELECT
            streamer_symbol,
            option_type,
            strike_price,
            expiration_date,
            dte,
            delta,
            snapshot_ts,
            mid_price,
            bid_price,
            ask_price,
            ABS(ABS(delta) - ?) AS delta_diff,
            ABS(CAST(strftime('%s', snapshot_ts) AS INTEGER) - ?) AS time_diff,
            ABS(dte - ?) AS dte_diff,
            CASE WHEN CAST(strftime('%s', snapshot_ts) AS INTEGER) <= ? THEN 0 ELSE 1 END AS is_after
        FROM spx_option_snapshots
        WHERE symbol = ?
          AND UPPER(option_type) = ?
          AND dte BETWEEN ? AND ?
          AND delta IS NOT NULL
          AND snapshot_ts BETWEEN ? AND ?
        ORDER BY time_diff ASC, is_after ASC, delta_diff ASC, dte_diff ASC, strike_price ASC
        """,
            [
            normalized_delta,
            entry_epoch,
            dte,
            entry_epoch,
            symbol,
            opt_type,
            dte_min,
            dte_max,
            window_from,
            window_to,
            ],
        ).fetchall()

        by_streamer: dict[str, Any] = {}
        for row in rows:
            streamer = str(row[0])
            if streamer not in by_streamer:
                by_streamer[streamer] = row
        return list(by_streamer.values())

    rows = query_candidates(dte, dte)
    if not rows and not strict_dte:
        rows = query_candidates(max(0, dte - 1), dte + 1)

    if not rows:
        if strict_dte:
            raise ValueError(
                f"No exact DTE={dte} contract found for this leg in the requested entry window."
            )
        raise ValueError(
            "No matching contract for this leg in the requested entry window."
        )

    contracts: list[dict[str, Any]] = []
    for row in rows:
        value = row[7]
        if value is None and row[8] is not None and row[9] is not None:
            value = (row[8] + row[9]) / 2.0

        delta_diff = float(row[10]) if row[10] is not None else None
        time_diff = float(row[11]) if row[11] is not None else None
        score = None
        if delta_diff is not None and time_diff is not None:
            score = round(delta_diff * 100 + (time_diff / 60.0), 4)

        contracts.append(
            {
                "symbol": symbol,
                "streamer_symbol": row[0],
                "option_type": row[1],
                "strike_price": row[2],
                "expiration_date": row[3],
                "dte": row[4],
                "delta": row[5],
                "snapshot_ts": row[6],
                "value": value,
                "target_dte": dte,
                "target_delta": normalized_delta,
                "delta_diff": delta_diff,
                "time_diff_seconds": time_diff,
                "entry_snapshot_ts": row[6],
                "entry_date": str(entry_date),
                "entry_time": entry_time,
                "entry_timezone": "America/New_York",
                "score": score,
                "window_minutes": window_minutes,
                "target_side": effective_side,
                "label": f"{row[1]} {row[2]} {row[3]}",
            }
        )

    # Keep top-level contract keys for backward compatibility while adding full match set.
    best = contracts[0]
    return {
        **best,
        "count": len(contracts),
        "contracts": contracts,
    }


def _run_contracts_payload(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    option_type: str | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    limit: int = 400,
) -> dict[str, Any]:
    clauses = ["symbol = ?"]
    params: list[Any] = [symbol]

    if start_dt is not None:
        clauses.append("snapshot_ts >= ?")
        params.append(_sqlite_timestamp(start_dt))
    if end_dt is not None:
        clauses.append("snapshot_ts <= ?")
        params.append(_sqlite_timestamp(end_dt))
    if option_type:
        clauses.append("UPPER(option_type) = ?")
        params.append(option_type.upper())
    if min_strike is not None:
        clauses.append("strike_price >= ?")
        params.append(min_strike)
    if max_strike is not None:
        clauses.append("strike_price <= ?")
        params.append(max_strike)

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT
            streamer_symbol,
            option_type,
            strike_price,
            expiration_date,
            MIN(snapshot_ts) AS first_ts,
            MAX(snapshot_ts) AS last_ts,
            COUNT(*) AS points
        FROM spx_option_snapshots
        WHERE {where}
        GROUP BY streamer_symbol, option_type, strike_price, expiration_date
        ORDER BY last_ts DESC, option_type, strike_price, expiration_date
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    return {
        "count": len(rows),
        "contracts": [
            {
                "streamer_symbol": str(row[0]),
                "option_type": row[1],
                "strike_price": row[2],
                "expiration_date": row[3],
                "first_ts": row[4],
                "last_ts": row[5],
                "points": row[6],
            }
            for row in rows
        ],
    }


def _run_series_payload(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    streamers: list[str],
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    field: str = "mid_price",
) -> dict[str, Any]:
    if not streamers:
        return {"rows": [], "count": 0}
    if len(streamers) > 120:
        raise ValueError("At most 120 streamers supported per call.")

    allowed_fields = {
        "mid_price": "mid_price",
        "bid_price": "bid_price",
        "ask_price": "ask_price",
    }
    if field not in allowed_fields:
        raise ValueError("Invalid field.")

    value_expr = f"COALESCE({allowed_fields[field]}, (bid_price + ask_price) / 2.0)"
    placeholders = ",".join(["?"] * len(streamers))
    clauses = [
        "symbol = ?",
        f"streamer_symbol IN ({placeholders})",
        "(mid_price IS NOT NULL OR (bid_price IS NOT NULL AND ask_price IS NOT NULL))",
    ]
    params: list[Any] = [symbol, *streamers]

    if start_dt is not None:
        clauses.append("snapshot_ts >= ?")
        params.append(_sqlite_timestamp(start_dt))
    if end_dt is not None:
        clauses.append("snapshot_ts <= ?")
        params.append(_sqlite_timestamp(end_dt))

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT
            snapshot_ts,
            streamer_symbol,
            mid_price,
            bid_price,
            ask_price,
            strike_price,
            option_type,
            expiration_date,
            delta,
            gamma,
            theta,
            vega,
            volatility,
            {value_expr} AS value
        FROM spx_option_snapshots
        WHERE {where}
        ORDER BY streamer_symbol, snapshot_ts
        """,
        params,
    ).fetchall()

    return {"count": len(rows), "rows": [dict(row) for row in rows]}


def _run_summary_payload(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, Any]:
    option_clauses = ["symbol = ?"]
    option_params: list[Any] = [symbol]

    if start_dt is not None:
        option_clauses.append("snapshot_ts >= ?")
        option_params.append(_sqlite_timestamp(start_dt))
    if end_dt is not None:
        option_clauses.append("snapshot_ts <= ?")
        option_params.append(_sqlite_timestamp(end_dt))

    option_where = " AND ".join(option_clauses)
    option_stats = conn.execute(
        f"""
        SELECT
            COUNT(*) AS option_rows,
            COUNT(DISTINCT streamer_symbol) AS contract_count,
            MIN(snapshot_ts) AS first_ts,
            MAX(snapshot_ts) AS last_ts
        FROM spx_option_snapshots
        WHERE {option_where}
        """,
        option_params,
    ).fetchone()

    market_clauses = ["symbol = ?"]
    market_params: list[Any] = [symbol]
    if start_dt is not None:
        market_clauses.append("snapshot_ts >= ?")
        market_params.append(_sqlite_timestamp(start_dt))
    if end_dt is not None:
        market_clauses.append("snapshot_ts <= ?")
        market_params.append(_sqlite_timestamp(end_dt))
    market_where = " AND ".join(market_clauses)

    market_rows = conn.execute(
        f"""
        SELECT snapshot_ts, spot_price, implied_volatility_index
        FROM spx_market_snapshots
        WHERE {market_where}
        ORDER BY snapshot_ts
        """,
        market_params,
    ).fetchall()

    return {
        "option_rows": option_stats[0] if option_stats else 0,
        "contract_count": option_stats[1] if option_stats else 0,
        "first_ts": option_stats[2] if option_stats else None,
        "last_ts": option_stats[3] if option_stats else None,
        "market_series": [dict(row) for row in market_rows],
    }


def _parse_strategy_leg_payload(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"leg[{index}] must be an object.")

    side = str(value.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"leg[{index}] must specify side BUY or SELL.")

    option_type = str(value.get("option_type", "PUT")).upper()
    if option_type not in {"PUT", "CALL"}:
        raise ValueError(f"leg[{index}] option_type must be PUT or CALL.")

    dte = _parse_int(str(value.get("dte")), "leg.dte")
    if dte < 0:
        raise ValueError(f"leg[{index}] dte must be >= 0.")

    target_delta = _parse_float_required(str(value.get("target_delta")), "leg.target_delta")
    entry_time = str(value.get("entry_time", "")).strip()
    if not entry_time:
        raise ValueError(f"leg[{index}] entry_time is required.")
    _parse_est_hhmm(entry_time, f"leg[{index}].entry_time")

    quantity = _parse_int(str(value.get("quantity", "1")), "leg.quantity")
    if quantity <= 0:
        raise ValueError(f"leg[{index}] quantity must be > 0.")

    return {
        "side": side,
        "option_type": option_type,
        "dte": dte,
        "target_delta": target_delta,
        "entry_time": entry_time,
        "quantity": quantity,
    }


def _run_strategy_history_payload(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    legs: list[dict[str, Any]],
    start_date: date | None,
    end_date: date | None,
    window_minutes: int = 30,
) -> dict[str, Any]:
    if not legs:
        raise ValueError("At least one strategy leg is required.")

    if start_date is None or end_date is None:
        latest_date = _resolve_latest_option_date(conn, symbol)
        if latest_date is None:
            raise ValueError("No option snapshots found for this symbol.")
        if end_date is None:
            end_date = latest_date
        if start_date is None:
            start_candidate = latest_date - timedelta(days=30)
            start_date = start_candidate if end_date is None or start_candidate <= end_date else end_date

    if start_date > end_date:
        raise ValueError("from cannot be after to.")

    if window_minutes <= 0:
        raise ValueError("window_minutes must be > 0.")

    trades: list[dict[str, Any]] = []
    total_pnl = 0.0
    total_indexed = []
    completed_count = 0
    win_count = 0

    current = start_date
    while current <= end_date:
        trade_result: dict[str, Any] = {
            "trade_date": str(current),
            "status": "ok",
            "legs": [],
            "strategy_entry": None,
            "strategy_exit": None,
            "strategy_pnl": None,
            "strategy_indexed": None,
            "strategy_contracts": 0,
        }

        leg_rows: list[dict[str, Any]] = []
        valid = True

        for index, leg in enumerate(legs):
            resolved_payload = _run_resolve_leg_payload(
                conn,
                symbol=symbol,
                option_type=leg["option_type"],
                dte=leg["dte"],
                target_delta=leg["target_delta"],
                entry_time=leg["entry_time"],
                entry_date=current,
                target_side=leg["side"],
                window_minutes=window_minutes,
            )
            resolved_contracts = list(resolved_payload.get("contracts") or [])
            if not resolved_contracts:
                valid = False
                trade_result["status"] = "missing_entry"
                break

            next_day_local = datetime(
                year=current.year,
                month=current.month,
                day=current.day,
                hour=23,
                minute=59,
                second=59,
                tzinfo=ZoneInfo("America/New_York"),
            ) + timedelta(seconds=1)
            exit_window_end = _sqlite_timestamp(next_day_local.astimezone(timezone.utc))
            missing_exit_count = 0
            for resolved in resolved_contracts:
                exit_row = conn.execute(
                    """
                    SELECT
                        snapshot_ts,
                        COALESCE(mid_price, (bid_price + ask_price) / 2.0) AS value
                    FROM spx_option_snapshots
                    WHERE symbol = ?
                      AND streamer_symbol = ?
                      AND snapshot_ts >= ?
                      AND snapshot_ts < ?
                      AND (mid_price IS NOT NULL OR (bid_price IS NOT NULL AND ask_price IS NOT NULL))
                    ORDER BY snapshot_ts DESC
                    LIMIT 1;
                    """,
                    [
                        symbol,
                        resolved["streamer_symbol"],
                        resolved["snapshot_ts"],
                        exit_window_end,
                    ],
                ).fetchone()
                if exit_row is None or exit_row[1] is None:
                    missing_exit_count += 1
                    continue

                exit_value = float(exit_row[1])
                entry_value = resolved.get("value")
                if entry_value is None:
                    continue

                sign = 1 if leg["side"] == "BUY" else -1
                qty = leg["quantity"]
                leg_entry_cash = sign * qty * float(entry_value)
                leg_exit_cash = sign * qty * exit_value
                leg_rows.append({
                    "streamer_symbol": resolved["streamer_symbol"],
                    "option_type": resolved["option_type"],
                    "strike_price": resolved["strike_price"],
                    "expiration_date": resolved["expiration_date"],
                    "entry_snapshot_ts": resolved["snapshot_ts"],
                    "exit_snapshot_ts": exit_row[0],
                    "entry_value": entry_value,
                    "exit_value": exit_value,
                    "qty": qty,
                    "side": leg["side"],
                    "target_delta": leg["target_delta"],
                    "target_dte": leg["dte"],
                    "resolved_delta": resolved["delta"],
                    "delta_diff": resolved["delta_diff"],
                    "time_diff_seconds": resolved["time_diff_seconds"],
                    "leg_entry_cash": leg_entry_cash,
                    "leg_exit_cash": leg_exit_cash,
                    "leg_pnl": leg_exit_cash - leg_entry_cash,
                })

            if missing_exit_count and not leg_rows:
                valid = False
                trade_result["status"] = "missing_exit"
                break
            if missing_exit_count:
                trade_result["status"] = "partial_missing_exit"

        if valid and leg_rows:
            strategy_entry = sum(leg["leg_entry_cash"] for leg in leg_rows)
            strategy_exit = sum(leg["leg_exit_cash"] for leg in leg_rows)
            strategy_pnl = strategy_exit - strategy_entry
            strategy_indexed = (strategy_exit / strategy_entry * 100) if strategy_entry else None

            completed_count += 1
            total_pnl += strategy_pnl
            if strategy_indexed is not None:
                total_indexed.append(strategy_indexed)
            if strategy_pnl > 0:
                win_count += 1

            trade_result.update(
                {
                    "status": "ok",
                    "legs": leg_rows,
                    "strategy_contracts": len(leg_rows),
                    "strategy_entry": strategy_entry,
                    "strategy_exit": strategy_exit,
                    "strategy_pnl": strategy_pnl,
                    "strategy_indexed": strategy_indexed,
                }
            )
        else:
            trade_result["legs"] = leg_rows
            trade_result["strategy_contracts"] = len(leg_rows)

        trades.append(trade_result)
        current += timedelta(days=1)

    completed = completed_count
    avg_indexed = sum(total_indexed) / len(total_indexed) if total_indexed else None
    win_rate = (win_count / completed * 100.0) if completed > 0 else None

    return {
        "summary": {
            "trade_count": len(trades),
            "completed_count": completed,
            "overall_pnl": total_pnl,
            "avg_indexed": avg_indexed,
            "win_rate": win_rate,
        },
        "trades": trades,
    }


def _error_response(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    _json_response(handler, {"error": message}, status=status)


def _get_query_params(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(path)
    return parsed.path, {k: v for k, v in parse_qs(parsed.query).items()}


def _get_qs(params: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = params.get(key)
    if not values:
        return default
    return values[0]


class SqlUiHandler(BaseHTTPRequestHandler):
    db_path: Path

    def do_GET(self) -> None:  # noqa: N802
        path, qs = _get_query_params(self.path)

        if path == "/":
            _html_response(self, _HTML)
            return
        if path == "/api/health":
            _json_response(self, {"ok": True, "db_path": str(self.db_path)})
            return
        if path == "/api/schema":
            with sqlite3.connect(self.db_path) as conn:
                payload = _schema_payload(conn)
            _json_response(self, payload)
            return
        if path == "/api/options/contracts":
            try:
                symbol = _get_qs(qs, "symbol", "SPX")
                start_dt = _parse_datetime(_get_qs(qs, "from"), "from")
                end_dt = _parse_datetime(_get_qs(qs, "to"), "to")
                option_type = _get_qs(qs, "type")
                min_strike = _parse_float(_get_qs(qs, "min_strike"), "min_strike")
                max_strike = _parse_float(_get_qs(qs, "max_strike"), "max_strike")
                limit = _parse_int(_get_qs(qs, "limit"), "limit", 400)
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    payload = _run_contracts_payload(
                        conn,
                        symbol=symbol or "SPX",
                        start_dt=start_dt,
                        end_dt=end_dt,
                        option_type=option_type,
                        min_strike=min_strike,
                        max_strike=max_strike,
                        limit=limit,
                    )
                _json_response(self, payload)
            except Exception as exc:
                _error_response(self, str(exc))
            return
        if path == "/api/options/series":
            try:
                symbol = _get_qs(qs, "symbol", "SPX")
                raw_streamers = _get_qs(qs, "streamers", "")
                streamers = [s.strip() for s in (raw_streamers or "").split(",") if s.strip()]
                if not streamers:
                    raise ValueError("streamers parameter is required.")
                start_dt = _parse_datetime(_get_qs(qs, "from"), "from")
                end_dt = _parse_datetime(_get_qs(qs, "to"), "to")
                field = _get_qs(qs, "field", "mid_price")
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    payload = _run_series_payload(
                        conn,
                        symbol=symbol or "SPX",
                        streamers=streamers,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        field=field,
                    )
                _json_response(self, payload)
            except Exception as exc:
                _error_response(self, str(exc))
            return
        if path == "/api/options/summary":
            try:
                symbol = _get_qs(qs, "symbol", "SPX")
                start_dt = _parse_datetime(_get_qs(qs, "from"), "from")
                end_dt = _parse_datetime(_get_qs(qs, "to"), "to")
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    payload = _run_summary_payload(
                        conn, symbol=symbol or "SPX", start_dt=start_dt, end_dt=end_dt
                    )
                _json_response(self, payload)
            except Exception as exc:
                _error_response(self, str(exc))
            return
        if path == "/api/options/snapshot-dates":
            try:
                symbol = _get_qs(qs, "symbol", "SPX")
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    payload = _run_snapshot_dates_payload(
                        conn, symbol=symbol or "SPX"
                    )
                _json_response(self, payload)
            except Exception as exc:
                _error_response(self, str(exc))
            return
        if path == "/api/options/resolve-leg":
            try:
                symbol = _get_qs(qs, "symbol", "SPX")
                option_type = _get_qs(qs, "option_type", "PUT")
                dte = _parse_int_required(_get_qs(qs, "dte"), "dte")
                target_delta = _parse_float_required(
                    _get_qs(qs, "target_delta"), "target_delta"
                )
                entry_time = _get_qs(qs, "entry_time")
                entry_date = _parse_date(_get_qs(qs, "entry_date"), "entry_date")
                target_side = _get_qs(qs, "target_side")
                snapshot_from = _parse_datetime(_get_qs(qs, "snapshot_from"), "snapshot_from")
                snapshot_to = _parse_datetime(_get_qs(qs, "snapshot_to"), "snapshot_to")
                window_minutes = _parse_int(_get_qs(qs, "window_minutes"), "window_minutes", 30)
                strict_dte_raw = (_get_qs(qs, "strict_dte") or "").strip().lower()
                strict_dte = strict_dte_raw in {"1", "true", "yes", "on"}
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    payload = _run_resolve_leg_payload(
                        conn,
                        symbol=symbol or "SPX",
                        option_type=option_type,
                        dte=dte,
                        target_delta=target_delta,
                        entry_time=entry_time or "",
                        entry_date=entry_date,
                        target_side=target_side,
                        snapshot_from=snapshot_from,
                        snapshot_to=snapshot_to,
                        window_minutes=window_minutes,
                        strict_dte=strict_dte,
                    )
                _json_response(self, payload)
            except Exception as exc:
                _error_response(self, str(exc))
            return

        _json_response(self, {"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/query", "/api/options/strategy-history"}:
            _json_response(self, {"error": "not_found"}, status=404)
            return

        try:
            raw_len = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(raw_len).decode("utf-8")
            parsed = json.loads(body)

            if self.path == "/api/query":
                query = str(parsed.get("query", ""))
                with sqlite3.connect(self.db_path) as conn:
                    payload = _run_query(conn, query)
                _json_response(self, payload)
                return

            payload_legs_raw = parsed.get("legs")
            if not isinstance(payload_legs_raw, list):
                raise ValueError("legs must be an array.")
            legs = [_parse_strategy_leg_payload(leg, i) for i, leg in enumerate(payload_legs_raw)]

            start = _parse_date(parsed.get("from"), "from")
            end = _parse_date(parsed.get("to"), "to")
            symbol = str(parsed.get("symbol", "SPX"))
            window_minutes = _parse_int(parsed.get("window_minutes"), "window_minutes", 30)

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                payload = _run_strategy_history_payload(
                    conn,
                    symbol=symbol or "SPX",
                    legs=legs,
                    start_date=start,
                    end_date=end,
                    window_minutes=window_minutes,
                )
            _json_response(self, payload)
        except Exception as exc:
            _json_response(self, {"error": str(exc)}, status=400)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spx-backtest-ui",
        description="Run local SQL UI against collector database.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings()
    db_path = _resolve_sqlite_path(settings.db_url)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found at {db_path}")

    SqlUiHandler.db_path = db_path
    server = ThreadingHTTPServer((args.host, args.port), SqlUiHandler)
    print(f"SQL UI running at http://{args.host}:{args.port} using {db_path}")
    server.serve_forever()


_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SPX Playground</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --ink: #0f172a;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --line: #e2e8f0;
      --accent: #0d9488;
      --accent2: #0ea5e9;
      --muted: #64748b;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Space Grotesk", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 420px at 0% 0%, #d1fae5 0%, transparent 50%),
        radial-gradient(900px 360px at 100% 0%, #e0f2fe 0%, transparent 45%),
        var(--bg);
    }
    .wrap {
      max-width: 1400px;
      margin: 0 auto;
      padding: 22px;
    }
    h1, h2, h3 {
      margin-top: 0;
    }
    h1 { font-size: 2rem; letter-spacing: -0.02em; }
    .sub { color: var(--muted); margin-top: 6px; }
    .grid {
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 14px;
      margin-top: 16px;
    }
    .grid.full {
      grid-template-columns: 1fr;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 6px 20px rgba(15,23,42,0.04);
      overflow: hidden;
    }
    textarea {
      width: 100%;
      min-height: 150px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92rem;
      resize: vertical;
    }
    .row {
      display: flex;
      gap: 10px;
      margin-top: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    button, select {
      border: 0;
      border-radius: 10px;
      padding: 9px 14px;
      font-weight: 700;
      color: #fff;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      cursor: pointer;
      font-family: inherit;
    }
    .secondary {
      background: #334155;
    }
    .input {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      font-size: 0.9rem;
      font-family: inherit;
    }
    label { font-size: 0.9rem; color: #1e293b; }
    .meta {
      color: var(--muted);
      font-size: 0.9rem;
    }
    .result-wrap {
      margin-top: 14px;
      max-height: 520px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding-bottom: 10px;
      scrollbar-gutter: stable both-edges;
    }
    .chart-wrap {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      overflow: hidden;
      position: relative;
      padding: 10px;
    }
    .chart-svg {
      width: 100%;
      height: 320px;
      display: block;
      background: #ffffff;
      cursor: crosshair;
    }
    .chart-tooltip {
      position: absolute;
      min-width: 126px;
      max-width: 187px;
      padding: 8px 10px;
      border-radius: 10px;
      background: rgba(15, 23, 42, 0.94);
      color: #f8fafc;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.18);
      font-size: 0.7rem;
      line-height: 1.35;
      pointer-events: none;
      opacity: 0;
      transform: translate(12px, -12px);
      transition: opacity 120ms ease;
      z-index: 2;
      white-space: nowrap;
    }
    .chart-tooltip.visible {
      opacity: 1;
    }
    .chart-tooltip-label {
      color: #cbd5e1;
      margin-bottom: 2px;
    }
    .chart-tooltip-value {
      font-weight: 700;
      text-align: center;
    }
    .chart-legend {
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
      margin-top: 8px;
      font-size: 0.85rem;
      color: #334155;
    }
    .chart-legend-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .chart-legend-swatch {
      width: 14px;
      height: 8px;
      border-radius: 4px;
      display: inline-block;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      font-size: 0.9rem;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      background: #f8fafc;
      z-index: 1;
    }
    .strategy-summary {
      background: #f1f5f9;
      font-weight: 700;
    }
    .schema-block {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px dashed var(--line);
    }
    .controls-card {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .analyzer-filter-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      grid-column: 1 / -1;
    }
    .controls-card .full { grid-column: 1 / -1; }
    .controls-card select[multiple] {
      height: 260px;
      padding: 6px;
      font-family: inherit;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
    }
    select option { padding: 4px; }
    .small {
      font-size: 0.85rem;
      color: #475569;
    }
    .status {
      font-size: 0.9rem;
      color: var(--muted);
      min-height: 1.25rem;
    }
    .success { color: #0369a1; }
    .danger { color: #b91c1c; }
    .run-analysis-wide {
      width: 100%;
      margin-top: 10px;
      background: #16a34a;
    }
    .remove-leg {
      border: 0;
      background: transparent;
      color: #94a3b8;
      font-weight: 700;
      cursor: pointer;
      padding: 0 4px;
      font-size: 1rem;
      line-height: 1;
    }
    .remove-leg:hover { color: #64748b; }
    .side-group {
      display: inline-flex;
      gap: 6px;
    }
    .side-btn {
      min-width: 58px;
      border: 0;
      border-radius: 8px;
      padding: 6px 10px;
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      opacity: 0.55;
    }
    .side-btn.active { opacity: 1; }
    .buy-btn { background: #166534; }
    .sell-btn { background: #b91c1c; }
    .qty-input {
      width: 100px;
    }
    .tab-nav {
      display: flex;
      gap: 10px;
      margin: 14px 0;
      flex-wrap: wrap;
    }
    .tab-button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      font-weight: 500;
      opacity: 0.8;
    }
    .tab-button.active {
      color: #fff;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      opacity: 1;
    }
    .tab-panel {
      display: none;
      margin-top: 0;
    }
    .tab-panel.active {
      display: block;
      animation: fade-in 180ms ease;
    }
    @keyframes fade-in {
      from { opacity: 0; transform: translateY(3px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 1100px) {
      .grid { grid-template-columns: 1fr; }
      .controls-card { grid-template-columns: 1fr; }
      .controls-card .full { grid-column: auto; }
      .tab-nav { gap: 8px; }
      .tab-button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>SPX Playground</h1>
    <div class="sub">Run read-only SQL against your local collector DB, and analyze option movement by contract.</div>
    <div class="tab-nav">
      <button type="button" class="tab-button active" data-tab="strategy">Strategy</button>
      <button type="button" class="tab-button" data-tab="analyzer">Options Analyzer</button>
      <button type="button" class="tab-button" data-tab="sql">SQL Lab</button>
    </div>

    <section id="tab-strategy" class="tab-panel active" data-tab="strategy">
      <div class="grid full">
        <div class="card">
          <h2 style="margin-bottom: 6px;">Strategy</h2>
          <div class="meta">Build and run strategy legs.</div>
          <div class="controls-card" style="margin-top:10px;">
            <div>
              <label for="strategySymbol">Symbol</label><br/>
              <select id="strategySymbol" class="input">
                <option value="SPX">SPX</option>
              </select>
            </div>
            <div>
              <label for="strategySide">Side</label><br/>
              <select id="strategySide" class="input">
                <option value="BUY">BUY</option>
                <option value="SELL">SELL</option>
              </select>
            </div>
            <div>
              <label for="strategyOptionType">Option Type</label><br/>
              <select id="strategyOptionType" class="input">
                <option value="PUT">PUT</option>
                <option value="CALL">CALL</option>
              </select>
            </div>
            <div>
              <label for="strategyDte">DTE</label><br/>
              <input id="strategyDte" class="input" type="number" min="0" step="1" value="1" />
            </div>
            <div>
              <label for="strategyDelta">Target Delta</label><br/>
              <input id="strategyDelta" class="input" type="number" min="0" step="1" value="35" />
            </div>
            <div>
              <label for="strategyEntryTime">Entry Time (ET)</label><br/>
              <input id="strategyEntryTime" class="input" type="time" value="10:30" />
            </div>
            <div>
              <label for="strategySnapshotFromDate">Snapshot From</label><br/>
              <input id="strategySnapshotFromDate" class="input" type="date" list="strategySnapshotFromDateList" />
              <datalist id="strategySnapshotFromDateList"></datalist>
            </div>
            <div>
              <label for="strategySnapshotToDate">Snapshot To</label><br/>
              <input id="strategySnapshotToDate" class="input" type="date" list="strategySnapshotToDateList" />
              <datalist id="strategySnapshotToDateList"></datalist>
            </div>
            <div>
              <label>&nbsp;</label><br/>
              <button id="strategyResolveBtn" class="run-analysis-wide">Add leg</button>
            </div>
          </div>
          <div id="strategyBuilderMeta" class="meta" style="margin-top:4px;">Resolve a leg to add it to the strategy.</div>
          <div class="result-wrap" style="margin-top:12px;">
            <table id="strategyLegsTable">
              <thead>
                <tr>
                  <th></th><th>Leg</th><th>Side</th><th>Quantity</th><th>Matches</th><th>Resolved At</th>
                </tr>
              </thead>
              <tbody></tbody>
            </table>
          </div>
          <button id="strategyRunBtn" class="run-analysis-wide" style="display:none; margin-top:12px;">Run Analysis</button>
          <div id="strategyAnalysisMeta" class="meta" style="margin-top:4px;">Resolve at least one leg to analyze.</div>
        </div>
      </div>

      <div class="grid full">
        <div class="card">
          <h2 style="margin-bottom: 6px;">Strategy Time Series</h2>
          <div id="strategySeriesMeta" class="meta">Resolved legs only.</div>
          <div class="result-wrap" style="margin-top:12px;">
            <table id="strategySeriesTable">
              <thead>
                <tr>
                  <th>Snapshot</th><th>Trade</th><th>Contract</th><th>Leg</th><th>Side</th><th>Spot</th><th>Price</th><th>Indexed</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th><th>Vol</th><th>Spread</th><th>Leg Contribution</th><th>Strategy</th><th>Strategy Cost</th><th>Strategy P&L</th><th>Strategy Indexed</th>
                </tr>
              </thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <h2 style="margin-bottom: 6px;">Strategy Trade Matrix</h2>
          <div class="meta">One row per trade, aligned at entry (T+0=100). Columns show indexed strategy progression by snapshot step.</div>
          <div class="result-wrap" style="margin-top:12px;">
            <table id="strategyTradeMatrixTable">
              <thead></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <h2 style="margin-bottom: 6px;">Strategy Index Chart</h2>
          <div id="strategyIndexChartMeta" class="meta">Aligned at entry (T+0). 15-minute ET interpolation with blended average.</div>
          <div class="chart-wrap">
            <svg id="strategyIndexChartSvg" class="chart-svg" viewBox="0 0 1200 320" preserveAspectRatio="none"></svg>
            <div id="strategyIndexChartTooltip" class="chart-tooltip" aria-hidden="true"></div>
          </div>
          <div class="chart-legend">
            <span class="chart-legend-item"><span class="chart-legend-swatch" style="background:#cbd5e1;"></span>Trade lines</span>
            <span class="chart-legend-item"><span class="chart-legend-swatch" style="background:#0f172a;"></span>Blended avg</span>
            <span class="chart-legend-item"><span class="chart-legend-swatch" style="background:#94a3b8;"></span>100 baseline</span>
            <span class="chart-legend-item"><span class="chart-legend-swatch" style="background:rgba(22,163,74,0.24);"></span>Profit zone</span>
            <span class="chart-legend-item"><span class="chart-legend-swatch" style="background:rgba(220,38,38,0.22);"></span>Loss zone</span>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-analyzer" class="tab-panel" data-tab="analyzer">
      <div class="grid">
        <div class="card">
          <h2 style="margin-bottom: 6px;">Options Analyzer</h2>
          <div class="meta">Track selected contracts and compare movement.</div>
          <div class="controls-card" style="margin-top:10px;">
            <div class="analyzer-filter-row">
              <div>
                <label for="analyzerSymbol">Symbol</label><br/>
                <select id="analyzerSymbol" class="input">
                  <option value="SPX">SPX</option>
                </select>
              </div>
              <div>
                <label for="analyzerOptionType">Option Type</label><br/>
                <select id="analyzerOptionType" class="input">
                  <option value="">All</option>
                  <option value="CALL">CALL</option>
                  <option value="PUT">PUT</option>
                </select>
              </div>
              <div>
                <label for="analyzerExpiration">Expiration</label><br/>
                <select id="analyzerExpiration" class="input">
                  <option value="">All Expirations</option>
                </select>
              </div>
            </div>
            <div class="full">
              <div class="row" style="margin-top:4px;">
                <span id="analyzerMeta" class="meta">Select 1-4 contracts and run analysis.</span>
              </div>
              <label for="analyzerContracts">Contracts (multi-select)</label>
              <select id="analyzerContracts" multiple></select>
            </div>
            <div class="full">
              <div class="result-wrap" style="margin-top:8px;">
                <table id="analyzerLegsTable">
                  <thead>
                    <tr>
                      <th></th><th>Contract</th><th>Side</th><th>Quantity</th>
                    </tr>
                  </thead>
                  <tbody></tbody>
                </table>
              </div>
            </div>
            <div class="full row">
              <button id="analyzerRunStrategyBtn" class="run-analysis-wide secondary">Run Strategy Analysis</button>
            </div>
          </div>
        </div>
      </div>
      <div class="grid full">
        <div class="card">
          <h2 style="margin-bottom: 6px;">Strategy Time Series</h2>
          <div id="analyzerStrategyMeta" class="meta">Use selected contracts with side/qty to analyze the combined strategy.</div>
          <div class="result-wrap" style="margin-top:12px;">
            <table id="analyzerStrategySeriesTable">
              <thead>
                <tr>
                  <th>Snapshot</th><th>Contract</th><th>Spot</th><th>Price</th><th>Indexed</th><th>Leg Contribution</th><th>Strategy</th><th>Strategy Cost</th><th>Strategy P&L</th><th>Strategy Indexed</th><th>Spread</th>
                </tr>
              </thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-sql" class="tab-panel" data-tab="sql">
      <div class="grid">
        <div class="card">
          <label><strong>SQL Query</strong></label>
          <textarea id="sqlQuery">SELECT snapshot_ts, symbol, spot_price
FROM spx_market_snapshots
ORDER BY snapshot_ts DESC
LIMIT 100;</textarea>
          <div class="row">
            <button id="sqlRunBtn">Run Query</button>
            <button id="sqlCsvBtn" class="secondary" disabled>Export CSV</button>
            <span id="sqlMeta" class="meta"></span>
          </div>
          <div class="result-wrap">
            <table id="sqlResultTable">
              <thead></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <h3 style="margin:0">Schema Guide</h3>
          <div class="meta">Tables and columns from current DB:</div>
          <div id="sqlSchema"></div>
        </div>
      </div>
    </section>
  </div>

  <script>
    const MAX_ANALYZER_SELECTED_CONTRACTS = 4;
    const MAX_STRATEGY_RESOLVED_CONTRACTS = 30;
    const MAX_STRATEGY_ANALYSIS_STREAMERS = 120;
    const MINUTE_DIFF_LABEL = 60;

    const strategyState = {
      symbol: "SPX",
      legs: [],
      nextLegId: 1,
      snapshotDates: [],
      tableRows: [],
      historyRows: [],
      lastMeta: "",
    };

    const tabInitState = {
      strategy: false,
      analyzer: false,
      sql: false,
    };

    const analyzerState = {
      symbol: "SPX",
      loadedContracts: [],
      selectedStreamers: new Set(),
      legs: new Map(),
      contractByStreamer: new Map(),
      tableRows: [],
      lastMeta: "",
    };

    const sqlState = {
      lastColumns: [],
      lastRows: [],
    };

    function escapeHtml(v) {
      const s = v === null || v === undefined ? "" : String(v);
      return s.split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;");
    }

    function parseTimestamp(value) {
      if (!value) return null;
      const raw = String(value).trim();
      if (!raw) return null;
      const hasZone = /Z$|[+-]\d\d:\d\d$/.test(raw);
      const normalized = hasZone ? raw : raw.replace(" ", "T") + "Z";
      const d = new Date(normalized);
      return Number.isNaN(d.getTime()) ? null : d;
    }

    function toCsv(columns, rows) {
      const esc = (v) => {
        const s = v === null || v === undefined ? "" : String(v);
        if (s.includes('"') || s.includes(",") || s.includes("\\n")) {
          return '"' + s.split('"').join('""') + '"';
        }
        return s;
      };
      const lines = [columns.map(esc).join(",")];
      rows.forEach((r) => lines.push(r.map(esc).join(",")));
      return lines.join("\\n");
    }

    function formatTimeDiff(seconds) {
      if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
      const secs = Number(seconds);
      if (!Number.isFinite(secs)) return "";
      const sign = secs < 0 ? "-" : "";
      const abs = Math.abs(secs);
      if (abs >= MINUTE_DIFF_LABEL) {
        const mins = Math.round(abs / 60);
        return `${sign}${mins}m`;
      }
      return `${sign}${Math.round(abs)}s`;
    }

    function formatDeltaTarget(value) {
      if (value === null || value === undefined) return "";
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "";
      const normalized = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
      return String(Math.round(normalized));
    }

    function formatLocalDateTime(value) {
      const d = parseTimestamp(value);
      if (!d) return "";
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: true,
      }).format(d) + " ET";
    }

    function contractLabel(contract) {
      const t = String(contract.option_type || "").toLowerCase();
      const typeLabel = t ? `${t[0].toUpperCase()}${t.slice(1)}` : "";
      return `${typeLabel} ${contract.strike_price} ${contract.expiration_date}`.trim();
    }

    function buildSpotLookup(spotSeries) {
      const normalized = (spotSeries || [])
        .filter((row) => row.snapshot_ts && row.spot_price !== null && row.spot_price !== undefined)
        .map((row) => ({ ts: row.snapshot_ts, value: Number(row.spot_price) }))
        .filter((row) => Number.isFinite(row.value))
        .sort((a, b) => parseTimestamp(a.ts) - parseTimestamp(b.ts));
      const times = normalized.map((row) => row.ts);

      return function nearestSpot(ts) {
        if (!times.length) return null;
        const target = parseTimestamp(ts);
        if (!target) return null;
        let lo = 0;
        let hi = times.length - 1;
        let best = 0;
        while (lo <= hi) {
          const mid = Math.floor((lo + hi) / 2);
          const midTs = parseTimestamp(times[mid]);
          if (midTs <= target) {
            best = mid;
            lo = mid + 1;
          } else {
            hi = mid - 1;
          }
        }
        return normalized[best] ? normalized[best].value : null;
      };
    }

    function renderSimpleTable(selector, columns, rows) {
      const table = document.getElementById(selector);
      const thead = table.querySelector("thead");
      const tbody = table.querySelector("tbody");
      thead.innerHTML = "";
      tbody.innerHTML = "";
      if (!columns.length) return;
      const trHead = document.createElement("tr");
      columns.forEach((c) => {
        const th = document.createElement("th");
        th.textContent = c;
        trHead.appendChild(th);
      });
      thead.appendChild(trHead);
      rows.forEach((r) => {
        const tr = document.createElement("tr");
        r.forEach((v) => {
          const td = document.createElement("td");
          td.textContent = v == null ? "" : v;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    }

    function initTabs() {
      const buttons = document.querySelectorAll(".tab-button");
      const panels = document.querySelectorAll(".tab-panel");

      function showTab(name) {
        buttons.forEach((btn) => {
          btn.classList.toggle("active", btn.getAttribute("data-tab") === name);
        });
        panels.forEach((panel) => {
          panel.classList.toggle("active", panel.getAttribute("data-tab") === name);
        });
      }

      buttons.forEach((btn) => {
        btn.addEventListener("click", () => {
          const target = btn.getAttribute("data-tab");
          if (!target) return;
          showTab(target);
        });
      });

      showTab("strategy");
    }

    function initSqlTab() {
      if (tabInitState.sql) return;
      tabInitState.sql = true;

      function renderSqlTable(columns, rows) {
        renderSimpleTable("sqlResultTable", columns, rows);
      }

      async function loadSchema() {
        const res = await fetch("/api/schema");
        const data = await res.json();
        const holder = document.getElementById("sqlSchema");
        holder.innerHTML = "";
        (data.tables || []).forEach((table) => {
          const block = document.createElement("div");
          block.className = "schema-block";
          const title = document.createElement("div");
          title.innerHTML = `<strong>${escapeHtml(table)}</strong>`;
          block.appendChild(title);
          const rows = (data.schema && data.schema[table]) ? data.schema[table] : [];
          const cols = document.createElement("div");
          cols.innerHTML = rows.map((c) => `<div class="meta">${escapeHtml(c.name)} <code>${escapeHtml(c.type || "")}</code></div>`).join("");
          block.appendChild(cols);
          holder.appendChild(block);
        });
      }

      async function runSqlQuery() {
        const query = document.getElementById("sqlQuery").value || "";
        const meta = document.getElementById("sqlMeta");
        const res = await fetch("/api/query", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
        });
        const data = await res.json();
        if (!res.ok) {
          meta.textContent = "Error: " + (data.error || "unknown");
          meta.className = "meta danger";
          sqlState.lastColumns = [];
          sqlState.lastRows = [];
          renderSqlTable([], []);
          document.getElementById("sqlCsvBtn").disabled = true;
          return;
        }
        sqlState.lastColumns = data.columns || [];
        sqlState.lastRows = data.rows || [];
        renderSqlTable(sqlState.lastColumns, sqlState.lastRows);
        meta.textContent = `${data.row_count == null ? sqlState.lastRows.length : data.row_count} rows returned`;
        meta.className = "meta success";
        document.getElementById("sqlCsvBtn").disabled = !sqlState.lastColumns.length;
      }

      function exportCsv() {
        if (!sqlState.lastColumns.length) return;
        const csv = toCsv(sqlState.lastColumns, sqlState.lastRows);
        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "query_results.csv";
        a.click();
        URL.revokeObjectURL(url);
      }

      document.getElementById("sqlRunBtn").addEventListener("click", runSqlQuery);
      document.getElementById("sqlCsvBtn").addEventListener("click", exportCsv);

      loadSchema();
      runSqlQuery();
    }

    function strategyLegLabel(leg) {
      const type = String(leg.option_type || "").toUpperCase();
      const delta = formatDeltaTarget(leg.target_delta);
      const dte = leg.target_dte == null ? "" : String(leg.target_dte);
      const entry = String(leg.entry_time || "");
      return `${type} Δ${delta} DTE ${dte} @ ${entry}`;
    }

    function refreshStrategyRunButtonVisibility() {
      const runBtn = document.getElementById("strategyRunBtn");
      const analysisMeta = document.getElementById("strategyAnalysisMeta");
      if (!runBtn || !analysisMeta) return;
      const hasLegs = strategyState.legs.length > 0;
      runBtn.style.display = hasLegs ? "inline-block" : "none";
      if (!hasLegs) {
        analysisMeta.textContent = "Resolve at least one leg to analyze.";
      }
    }

    function renderStrategyLegsTable() {
      const body = document.querySelector("#strategyLegsTable tbody");
      body.innerHTML = "";
      strategyState.legs.forEach((leg) => {
        const buyActive = leg.side === "BUY" ? "active" : "";
        const sellActive = leg.side === "SELL" ? "active" : "";
        const resolvedAt = leg.entry_snapshot_ts ? formatLocalDateTime(leg.entry_snapshot_ts) : "";
        const matchCount = Number(leg.matched_count) || 0;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><button type="button" class="remove-leg" data-leg-id="${String(leg.id)}">x</button></td>
          <td>${escapeHtml(strategyLegLabel(leg))}</td>
          <td>
            <div class="side-group">
              <button type="button" class="side-btn buy-btn ${buyActive}" data-side-leg-id="${String(leg.id)}" data-side="BUY">BUY</button>
              <button type="button" class="side-btn sell-btn ${sellActive}" data-side-leg-id="${String(leg.id)}" data-side="SELL">SELL</button>
            </div>
          </td>
          <td><input class="input qty-input" type="number" min="1" step="1" value="${Number(leg.quantity) || 1}" data-qty-leg-id="${String(leg.id)}" /></td>
          <td>${escapeHtml(String(matchCount))}</td>
          <td>${escapeHtml(resolvedAt)}</td>
        `;
        body.appendChild(tr);
      });

      body.querySelectorAll("[data-leg-id]").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          const rawId = event.currentTarget.getAttribute("data-leg-id");
          const id = parseInt(rawId || "", 10);
          if (!Number.isFinite(id)) return;
          strategyState.legs = strategyState.legs.filter((leg) => leg.id !== id);
          renderStrategyLegsTable();
        });
      });

      body.querySelectorAll("[data-side-leg-id]").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          const rawId = event.currentTarget.getAttribute("data-side-leg-id");
          const side = event.currentTarget.getAttribute("data-side");
          const id = parseInt(rawId || "", 10);
          if (!Number.isFinite(id) || (side !== "BUY" && side !== "SELL")) return;
          const leg = strategyState.legs.find((row) => row.id === id);
          if (!leg) return;
          leg.side = side;
          renderStrategyLegsTable();
        });
      });

      body.querySelectorAll("[data-qty-leg-id]").forEach((inputEl) => {
        inputEl.addEventListener("input", (event) => {
          const rawId = event.currentTarget.getAttribute("data-qty-leg-id");
          const id = parseInt(rawId || "", 10);
          if (!Number.isFinite(id)) return;
          const leg = strategyState.legs.find((row) => row.id === id);
          if (!leg) return;
          const parsed = parseInt(event.currentTarget.value || "1", 10);
          leg.quantity = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
        });
      });
      refreshStrategyRunButtonVisibility();
    }

    async function resolveStrategyLeg() {
      const meta = document.getElementById("strategyBuilderMeta");
      const symbol = document.getElementById("strategySymbol").value || "SPX";
      const side = document.getElementById("strategySide").value || "BUY";
      const optionType = document.getElementById("strategyOptionType").value || "PUT";
      const dte = parseInt(document.getElementById("strategyDte").value || "0", 10);
      const targetDelta = parseFloat(document.getElementById("strategyDelta").value || "0");
      const entryTime = document.getElementById("strategyEntryTime").value || "";
      const snapshotFromDate = document.getElementById("strategySnapshotFromDate").value || "";
      const snapshotToDate = document.getElementById("strategySnapshotToDate").value || "";
      const snapshotDates = Array.isArray(strategyState.snapshotDates) ? strategyState.snapshotDates : [];
      const hasSnapshotDate = (value) => !value || snapshotDates.includes(value);

      if (!entryTime) {
        meta.textContent = "Entry Time is required.";
        meta.className = "meta danger";
        return;
      }
      if (!Number.isFinite(dte) || dte < 0) {
        meta.textContent = "DTE must be a non-negative integer.";
        meta.className = "meta danger";
        return;
      }
      if (!Number.isFinite(targetDelta) || targetDelta <= 0) {
        meta.textContent = "Target Delta must be > 0.";
        meta.className = "meta danger";
        return;
      }
      const roundedTargetDelta = Math.round(targetDelta);
      if (!snapshotDates.length) {
        meta.textContent = "No snapshot dates available for this symbol.";
        meta.className = "meta danger";
        return;
      }
      if (!hasSnapshotDate(snapshotFromDate) || !hasSnapshotDate(snapshotToDate)) {
        meta.textContent = "Choose Snapshot From/To dates from the available snapshot dates.";
        meta.className = "meta danger";
        return;
      }
      const effectiveFromDate = snapshotFromDate || snapshotDates[0];
      const effectiveToDate = snapshotToDate || snapshotDates[snapshotDates.length - 1];
      if (effectiveFromDate > effectiveToDate) {
        meta.textContent = "Snapshot From must not be after Snapshot To.";
        meta.className = "meta danger";
        return;
      }

      const params = new URLSearchParams({
        symbol,
        option_type: optionType,
        dte: String(dte),
        target_delta: String(roundedTargetDelta),
        entry_time: entryTime,
        target_side: side,
        strict_dte: "1",
      });
      params.set("snapshot_from", `${effectiveFromDate}T00:00:00`);
      params.set("snapshot_to", `${effectiveToDate}T23:59:59`);

      meta.textContent = "Resolving leg...";
      meta.className = "meta";
      const res = await fetch(`/api/options/resolve-leg?${params.toString()}`);
      const payload = await res.json();
      const resolvedContracts = Array.isArray(payload.contracts) ? payload.contracts : (payload.streamer_symbol ? [payload] : []);
      if (!res.ok || !resolvedContracts.length) {
        meta.textContent = "Could not resolve leg: " + (payload.error || "no match found.");
        meta.className = "meta danger";
        return;
      }

      const keptContracts = resolvedContracts.slice(0, MAX_STRATEGY_RESOLVED_CONTRACTS);
      const skippedCap = Math.max(0, resolvedContracts.length - keptContracts.length);
      if (!keptContracts.length) {
        meta.textContent = `No contracts added. Strategy leg cap is ${MAX_STRATEGY_RESOLVED_CONTRACTS}.`;
        meta.className = "meta danger";
        renderStrategyLegsTable();
        return;
      }
      strategyState.legs.push({
        id: strategyState.nextLegId++,
        side,
        quantity: 1,
        option_type: optionType,
        target_delta: roundedTargetDelta,
        target_dte: dte,
        entry_time: entryTime,
        isResolved: true,
        matched_count: keptContracts.length,
        entry_snapshot_ts: keptContracts[0] ? keptContracts[0].snapshot_ts : null,
        resolved_contracts: keptContracts,
      });
      const added = keptContracts.length;
      meta.textContent = `Resolved leg with ${added} matched contract${added === 1 ? "" : "s"}` + (skippedCap ? ` (${skippedCap} skipped at cap ${MAX_STRATEGY_RESOLVED_CONTRACTS})` : "");
      meta.className = "meta success";
      renderStrategyLegsTable();
    }

    function transformStrategySeriesRows(rows, spotSeries, tradePlans) {
      const nearestSpot = buildSpotLookup(spotSeries || []);
      const rowsByStreamer = new Map();
      rows.forEach((row) => {
        const streamer = row.streamer_symbol;
        if (!streamer) return;
        if (!rowsByStreamer.has(streamer)) rowsByStreamer.set(streamer, []);
        rowsByStreamer.get(streamer).push(row);
      });
      rowsByStreamer.forEach((list) => {
        list.sort((a, b) => parseTimestamp(a.snapshot_ts) - parseTimestamp(b.snapshot_ts));
      });

      const rowByStreamerTs = new Map();
      rowsByStreamer.forEach((list, streamer) => {
        const byTs = new Map();
        list.forEach((row) => {
          const ts = String(row.snapshot_ts || "");
          if (!ts || byTs.has(ts)) return;
          byTs.set(ts, row);
        });
        rowByStreamerTs.set(streamer, byTs);
      });

      const allTimestamps = Array.from(new Set(rows.map((row) => String(row.snapshot_ts || "")).filter(Boolean)))
        .sort((a, b) => parseTimestamp(a) - parseTimestamp(b));

      const enrichedTrades = tradePlans.map((trade) => {
        const legs = trade.legs.map((leg) => {
          const list = rowsByStreamer.get(leg.streamer_symbol) || [];
          const entryTs = parseTimestamp(leg.entry_snapshot_ts);
          let entryRow = null;
          if (entryTs) {
            entryRow = list.find((row) => {
              const rowTs = parseTimestamp(row.snapshot_ts);
              return rowTs && rowTs.getTime() === entryTs.getTime();
            }) || [...list].reverse().find((row) => {
              const rowTs = parseTimestamp(row.snapshot_ts);
              return rowTs && row.value != null && rowTs <= entryTs;
            });
          }
          if (!entryRow) {
            entryRow = list.find((row) => row.value != null && Number.isFinite(Number(row.value))) || null;
          }
          const entryValue = entryRow && entryRow.value != null && Number.isFinite(Number(entryRow.value)) ? Number(entryRow.value) : null;
          return {
            ...leg,
            entry_ts: entryTs,
            entry_value: entryValue,
          };
        });
        const entryTimes = legs
          .map((leg) => leg.entry_ts)
          .filter((ts) => ts && Number.isFinite(ts.getTime()))
          .map((ts) => ts.getTime());
        const tradeStartTs = entryTimes.length ? new Date(Math.max(...entryTimes)) : null;
        return { ...trade, legs, trade_start_ts: tradeStartTs };
      });

      const out = [];
      allTimestamps.forEach((ts) => {
        const perTradeRows = [];
        enrichedTrades.forEach((trade) => {
          const currentTs = parseTimestamp(ts);
          if (trade.trade_start_ts && currentTs && currentTs < trade.trade_start_ts) return;
          let strategyValue = 0;
          let strategyCost = 0;
          let complete = true;
          const tradeRows = [];
          trade.legs.forEach((leg) => {
            const streamerRows = rowByStreamerTs.get(leg.streamer_symbol);
            const row = streamerRows ? streamerRows.get(ts) : null;
            const value = row && row.value != null ? Number(row.value) : null;
            if (!row || value == null || !Number.isFinite(value) || leg.entry_value == null || !Number.isFinite(leg.entry_value)) {
              complete = false;
              return;
            }
            if (leg.entry_ts && currentTs && currentTs < leg.entry_ts) {
              complete = false;
              return;
            }
            const contribution = leg.sign * leg.quantity * value;
            strategyValue += contribution;
            strategyCost += leg.sign * leg.quantity * leg.entry_value;
            const indexed = leg.entry_value !== 0 ? (value / leg.entry_value) * 100 : null;
            tradeRows.push({
              ...row,
              snapshot_ts: ts,
              trade_index: trade.trade_index,
              indexed,
              spot_price: nearestSpot(ts),
              leg_contribution: contribution,
              resolved_contract: contractLabel(leg.contract),
              leg_label: strategyLegLabel(leg.leg_def),
              leg_side: leg.leg_def.side,
              isStrategySummary: false,
            });
          });
          if (!complete || tradeRows.length !== trade.legs.length || strategyCost === 0) return;
          const strategyIdx = (strategyValue / strategyCost) * 100;
          const strategyPnl = strategyValue - strategyCost;
          tradeRows.forEach((row) => {
            perTradeRows.push({
              ...row,
              strategy_price: strategyValue,
              strategy_cost: strategyCost,
              strategy_pnl: strategyPnl,
              strategy_indexed: strategyIdx,
            });
          });
        });

        perTradeRows.sort((a, b) => {
          if (a.trade_index !== b.trade_index) return a.trade_index - b.trade_index;
          return contractLabel(a).localeCompare(contractLabel(b));
        });
        perTradeRows.forEach((row) => out.push(row));
      });

      return out;
    }

    function renderStrategySeriesTable(rows) {
      const body = document.querySelector("#strategySeriesTable tbody");
      body.innerHTML = "";
      rows.forEach((row) => {
        const spread = row.isStrategySummary
          ? null
          : (row.bid_price == null || row.ask_price == null ? null : Number(row.ask_price) - Number(row.bid_price));
        const strategy = row.strategy_price == null ? "" : Number(row.strategy_price).toFixed(4);
        const strategyCost = row.strategy_cost == null ? "" : Number(row.strategy_cost).toFixed(4);
        const strategyPnl = row.strategy_pnl == null ? "" : Number(row.strategy_pnl).toFixed(4);
        const strategyIndexed = row.strategy_indexed == null ? "" : Number(row.strategy_indexed).toFixed(4);
        const contribution = row.leg_contribution == null ? "" : Number(row.leg_contribution).toFixed(4);
        const legLabel = row.isStrategySummary ? "" : escapeHtml(row.leg_label || "");
        const legSide = row.isStrategySummary ? "" : escapeHtml(row.leg_side || "");
        const price = row.value;
        const delta = row.isStrategySummary ? null : row.delta;
        const gamma = row.isStrategySummary ? null : row.gamma;
        const theta = row.isStrategySummary ? null : row.theta;
        const vega = row.isStrategySummary ? null : row.vega;
        const vol = row.isStrategySummary ? null : row.volatility;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.snapshot_ts ? formatLocalDateTime(row.snapshot_ts) : ""}</td>
          <td>${row.isStrategySummary ? "Avg" : escapeHtml(String(row.trade_index == null ? "" : row.trade_index))}</td>
          <td>${row.isStrategySummary ? "Strategy" : escapeHtml(row.isStrategySummary ? "Strategy" : row.resolved_contract || contractLabel(row))}</td>
          <td>${legLabel}</td>
          <td>${legSide}</td>
          <td>${row.spot_price == null ? "" : Number(row.spot_price).toFixed(4)}</td>
          <td>${price == null ? "" : Number(price).toFixed(4)}</td>
          <td>${row.indexed == null ? "" : Number(row.indexed).toFixed(4)}</td>
          <td>${delta == null ? "" : Number(delta).toFixed(4)}</td>
          <td>${gamma == null ? "" : Number(gamma).toFixed(4)}</td>
          <td>${theta == null ? "" : Number(theta).toFixed(4)}</td>
          <td>${vega == null ? "" : Number(vega).toFixed(4)}</td>
          <td>${vol == null ? "" : Number(vol).toFixed(4)}</td>
          <td>${spread == null ? "" : spread.toFixed(4)}</td>
          <td>${contribution}</td>
          <td>${strategy}</td>
          <td>${strategyCost}</td>
          <td>${strategyPnl}</td>
          <td>${strategyIndexed}</td>
        `;
        if (row.isStrategySummary) tr.className = "strategy-summary";
        body.appendChild(tr);
      });
    }

    function renderStrategyTradeMatrixTable(rows) {
      const table = document.getElementById("strategyTradeMatrixTable");
      const head = table ? table.querySelector("thead") : null;
      const body = table ? table.querySelector("tbody") : null;
      if (!head || !body) return;

      head.innerHTML = "";
      body.innerHTML = "";

      const detailRows = (rows || []).filter((row) => !row.isStrategySummary);
      if (!detailRows.length) return;

      const byTrade = new Map();
      detailRows.forEach((row) => {
        const key = String(row.trade_index == null ? "" : row.trade_index);
        if (!key) return;
        if (!byTrade.has(key)) {
          byTrade.set(key, new Map());
        }
        const price = row.strategy_price;
        if (price == null || !Number.isFinite(Number(price))) return;
        const tradeMap = byTrade.get(key);
        const ts = String(row.snapshot_ts || "");
        if (!ts || tradeMap.has(ts)) return;
        tradeMap.set(ts, Number(price));
      });

      const sortedTrades = Array.from(byTrade.entries()).sort((a, b) => Number(a[0]) - Number(b[0]));
      const normalizedRows = sortedTrades.map(([tradeKey, tradeMap]) => {
        const sortedTs = Array.from(tradeMap.keys()).sort((a, b) => parseTimestamp(a) - parseTimestamp(b));
        if (!sortedTs.length) return { tradeKey, entryTs: "", indexed: [] };
        const entryTs = sortedTs[0];
        const entryPrice = tradeMap.get(entryTs);
        if (entryPrice == null || !Number.isFinite(Number(entryPrice)) || Number(entryPrice) === 0) {
          return { tradeKey, entryTs, indexed: sortedTs.map(() => "") };
        }
        const indexed = sortedTs.map((ts) => {
          const px = tradeMap.get(ts);
          if (px == null || !Number.isFinite(Number(px))) return "";
          return ((Number(px) / Number(entryPrice)) * 100).toFixed(4);
        });
        return { tradeKey, entryTs, indexed };
      });

      const maxSteps = normalizedRows.reduce((m, row) => Math.max(m, row.indexed.length), 0);
      if (!maxSteps) return;
      const headerCells = ["Trade", "Entry Snapshot", ...Array.from({ length: maxSteps }, (_, idx) => `T+${idx}`)];
      const trHead = document.createElement("tr");
      trHead.innerHTML = headerCells.map((label) => `<th>${escapeHtml(label)}</th>`).join("");
      head.appendChild(trHead);

      normalizedRows.forEach((row) => {
        const tr = document.createElement("tr");
        const cells = [
          row.tradeKey,
          row.entryTs ? formatLocalDateTime(row.entryTs) : "",
          ...Array.from({ length: maxSteps }, (_, idx) => row.indexed[idx] || ""),
        ];
        tr.innerHTML = cells.map((value) => `<td>${escapeHtml(String(value))}</td>`).join("");
        body.appendChild(tr);
      });
    }

    function formatEtHm(dateObj) {
      if (!dateObj) return "";
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(dateObj);
    }

    function formatEtDateKey(dateObj) {
      if (!dateObj) return "";
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/New_York",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).formatToParts(dateObj);
      const y = parts.find((p) => p.type === "year")?.value || "";
      const m = parts.find((p) => p.type === "month")?.value || "";
      const d = parts.find((p) => p.type === "day")?.value || "";
      return y && m && d ? `${y}-${m}-${d}` : "";
    }

    function dateUtcFromYmd(ymd) {
      if (!ymd || !/^\d{4}-\d{2}-\d{2}$/.test(ymd)) return null;
      const [yy, mm, dd] = ymd.split("-").map((v) => Number(v));
      if (!Number.isFinite(yy) || !Number.isFinite(mm) || !Number.isFinite(dd)) return null;
      return Date.UTC(yy, mm - 1, dd);
    }

    function dteForTs(expirationYmd, tsDate) {
      const expUtc = dateUtcFromYmd(expirationYmd);
      const tickKey = formatEtDateKey(tsDate);
      const tickUtc = dateUtcFromYmd(tickKey);
      if (expUtc == null || tickUtc == null) return "";
      const diff = Math.round((expUtc - tickUtc) / 86400000);
      return String(Math.max(0, diff));
    }

    function formatStrategyHoverDelta(value, profitBelow100) {
      if (value == null || !Number.isFinite(Number(value))) return "";
      const rawDelta = Number(value) - 100;
      const signedDelta = profitBelow100 ? -rawDelta : rawDelta;
      const sign = signedDelta > 0 ? "+" : "";
      return `${sign}${signedDelta.toFixed(2)}%`;
    }

    function stepPath(points, xScale, yScale) {
      let d = "";
      points.forEach((p, idx) => {
        const x = xScale(p.x).toFixed(2);
        const y = yScale(p.y).toFixed(2);
        if (idx === 0) {
          d += `M${x},${y} `;
          return;
        }
        const prev = points[idx - 1];
        const prevY = yScale(prev.y).toFixed(2);
        d += `L${x},${prevY} L${x},${y} `;
      });
      return d.trim();
    }

    function renderStrategyIndexChart(rows) {
      const svg = document.getElementById("strategyIndexChartSvg");
      const meta = document.getElementById("strategyIndexChartMeta");
      const tooltip = document.getElementById("strategyIndexChartTooltip");
      const wrap = svg ? svg.closest(".chart-wrap") : null;
      if (!svg || !meta || !tooltip || !wrap) return;

      svg.innerHTML = "";
      tooltip.classList.remove("visible");
      tooltip.innerHTML = "";
      const detailRows = (rows || []).filter((row) => !row.isStrategySummary);
      if (!detailRows.length) {
        meta.textContent = "No strategy data to chart.";
        return;
      }

      const byTrade = new Map();
      detailRows.forEach((row) => {
        const tradeKey = String(row.trade_index == null ? "" : row.trade_index);
        if (!tradeKey) return;
        const ts = String(row.snapshot_ts || "");
        const tsDate = parseTimestamp(ts);
        const strategyPrice = row.strategy_price;
        const strategyCost = row.strategy_cost;
        if (!tsDate || strategyPrice == null || !Number.isFinite(Number(strategyPrice))) return;
        if (!byTrade.has(tradeKey)) byTrade.set(tradeKey, []);
        byTrade.get(tradeKey).push({
          ts,
          tsDate,
          strategyPrice: Number(strategyPrice),
          strategyCost: strategyCost == null ? null : Number(strategyCost),
          expirationDate: String(row.expiration_date || ""),
        });
      });

      const stepMs = 15 * 60 * 1000;
      const tradeSeries = [];
      Array.from(byTrade.entries())
        .sort((a, b) => Number(a[0]) - Number(b[0]))
        .forEach(([tradeKey, points]) => {
          const uniq = new Map();
          points
            .sort((a, b) => a.tsDate - b.tsDate)
            .forEach((p) => {
              if (!uniq.has(p.ts)) uniq.set(p.ts, p);
            });
          const sorted = Array.from(uniq.values());
          if (!sorted.length) return;
          const entry = sorted[0];
          if (!Number.isFinite(entry.strategyPrice) || entry.strategyPrice === 0) return;

          const normalized = sorted.map((p) => ({
            elapsedMs: p.tsDate.getTime() - entry.tsDate.getTime(),
            tsDate: p.tsDate,
            indexed: (p.strategyPrice / entry.strategyPrice) * 100,
          }));
          const maxElapsed = normalized[normalized.length - 1].elapsedMs;
          if (!Number.isFinite(maxElapsed) || maxElapsed < 0) return;

          function carryForward(ms) {
            if (ms < normalized[0].elapsedMs || ms > normalized[normalized.length - 1].elapsedMs) return null;
            let value = normalized[0].indexed;
            for (let i = 1; i < normalized.length; i += 1) {
              if (ms < normalized[i].elapsedMs) break;
              value = normalized[i].indexed;
            }
            return value;
          }

          const steps = [];
          const maxStep = Math.floor(maxElapsed / stepMs);
          for (let s = 0; s <= maxStep; s += 1) {
            const ms = s * stepMs;
            const val = carryForward(ms);
            steps.push(val == null ? null : Number(val));
          }

          tradeSeries.push({
            tradeKey,
            entryDate: entry.tsDate,
            entryCost: entry.strategyCost,
            expirationDate: entry.expirationDate,
            steps,
          });
        });

      if (!tradeSeries.length) {
        meta.textContent = "No plottable trade series after alignment.";
        return;
      }

      const maxSteps = tradeSeries.reduce((m, t) => Math.max(m, t.steps.length), 0);
      if (!maxSteps) {
        meta.textContent = "No aligned samples available for chart.";
        return;
      }

      const blended = [];
      for (let i = 0; i < maxSteps; i += 1) {
        const vals = tradeSeries.map((t) => t.steps[i]).filter((v) => v != null && Number.isFinite(v));
        blended.push(vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null);
      }

      const firstTrade = tradeSeries[0];
      const sampleTimes = Array.from({ length: maxSteps }, (_, i) => new Date(firstTrade.entryDate.getTime() + i * stepMs));
      const allY = tradeSeries
        .flatMap((t) => t.steps)
        .concat(blended)
        .filter((v) => v != null && Number.isFinite(v));
      if (!allY.length) {
        meta.textContent = "No numeric values available for chart.";
        return;
      }

      const maxAbs = Math.max(...allY.map((v) => Math.abs(v - 100)));
      const span = Math.max(1, maxAbs * 1.15);
      const yMin = 100 - span;
      const yMax = 100 + span;

      const width = 1200;
      const height = 320;
      const m = { top: 18, right: 16, bottom: 78, left: 56 };
      const innerW = width - m.left - m.right;
      const innerH = height - m.top - m.bottom;
      const xMax = Math.max(1, maxSteps - 1);
      const xScale = (x) => m.left + (x / xMax) * innerW;
      const yScale = (y) => m.top + ((yMax - y) / (yMax - yMin)) * innerH;

      const tradeTypes = new Set(
        tradeSeries.map((t) => (t.entryCost == null ? "unknown" : (t.entryCost < 0 ? "credit" : "debit")))
      );
      const mixedTradeTypes = tradeTypes.has("credit") && tradeTypes.has("debit");
      const referenceType = tradeSeries.find((t) => t.entryCost != null)?.entryCost < 0 ? "credit" : "debit";
      const profitBelow100 = referenceType === "credit";

      const dteRefExpiration = firstTrade.expirationDate;
      const gridLines = 6;
      for (let g = 0; g <= gridLines; g += 1) {
        const yVal = yMin + (g / gridLines) * (yMax - yMin);
        const y = yScale(yVal);
        const isBaseline = Math.abs(yVal - 100) < 1e-6;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", String(m.left));
        line.setAttribute("x2", String(m.left + innerW));
        line.setAttribute("y1", String(y));
        line.setAttribute("y2", String(y));
        line.setAttribute("stroke", isBaseline ? "#64748b" : "#e2e8f0");
        line.setAttribute("stroke-width", isBaseline ? "1.5" : "1");
        line.setAttribute("stroke-dasharray", isBaseline ? "4 3" : "0");
        svg.appendChild(line);

        if (g === 0 || g === Math.floor(gridLines / 2) || g === gridLines) {
          const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
          txt.setAttribute("x", String(m.left - 8));
          txt.setAttribute("y", String(y + 4));
          txt.setAttribute("text-anchor", "end");
          txt.setAttribute("font-size", "11");
          txt.setAttribute("fill", "#64748b");
          txt.textContent = `${yVal.toFixed(1)}`;
          svg.appendChild(txt);
        }
      }

      for (let i = 1; i < blended.length; i += 1) {
        const a = blended[i - 1];
        const b = blended[i];
        if (a == null || b == null) continue;
        const isProfit = profitBelow100 ? a <= 100 : a >= 100;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const x1 = xScale(i - 1);
        const x2 = xScale(i);
        const y = yScale(a);
        const yBase = yScale(100);
        path.setAttribute("d", `M${x1},${yBase} L${x1},${y} L${x2},${y} L${x2},${yBase} Z`);
        path.setAttribute("fill", isProfit ? "rgba(22,163,74,0.24)" : "rgba(220,38,38,0.22)");
        svg.appendChild(path);
      }

      tradeSeries.forEach((series) => {
        const pts = series.steps
          .map((v, i) => (v == null ? null : ({ x: i, y: v })))
          .filter(Boolean);
        if (pts.length < 2) return;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", stepPath(pts, xScale, yScale));
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#94a3b8");
        path.setAttribute("stroke-width", "1");
        path.setAttribute("opacity", "0.55");
        svg.appendChild(path);
      });

      const blendPts = blended
        .map((v, i) => (v == null ? null : ({ x: i, y: v })))
        .filter(Boolean);
      if (blendPts.length >= 2) {
        const blendPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
        blendPath.setAttribute("d", stepPath(blendPts, xScale, yScale));
        blendPath.setAttribute("fill", "none");
        blendPath.setAttribute("stroke", "#0f172a");
        blendPath.setAttribute("stroke-width", "2.4");
        svg.appendChild(blendPath);
      }

      const hoverGuide = document.createElementNS("http://www.w3.org/2000/svg", "line");
      hoverGuide.setAttribute("y1", String(m.top));
      hoverGuide.setAttribute("y2", String(m.top + innerH));
      hoverGuide.setAttribute("stroke", "#0f172a");
      hoverGuide.setAttribute("stroke-width", "1");
      hoverGuide.setAttribute("stroke-dasharray", "4 4");
      hoverGuide.setAttribute("opacity", "0");
      hoverGuide.setAttribute("pointer-events", "none");
      svg.appendChild(hoverGuide);

      const hoverMarker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      hoverMarker.setAttribute("r", "4.5");
      hoverMarker.setAttribute("fill", "#ffffff");
      hoverMarker.setAttribute("stroke", "#0f172a");
      hoverMarker.setAttribute("stroke-width", "2");
      hoverMarker.setAttribute("opacity", "0");
      hoverMarker.setAttribute("pointer-events", "none");
      svg.appendChild(hoverMarker);

      function hideTooltip() {
        tooltip.classList.remove("visible");
        tooltip.innerHTML = "";
        hoverGuide.setAttribute("opacity", "0");
        hoverMarker.setAttribute("opacity", "0");
      }

      function findNearestBlendedIndex(target) {
        if (!blended.length) return -1;
        let bestIdx = -1;
        let bestDist = Infinity;
        for (let i = 0; i < blended.length; i += 1) {
          const value = blended[i];
          if (value == null || !Number.isFinite(value)) continue;
          const dist = Math.abs(i - target);
          if (dist < bestDist) {
            bestDist = dist;
            bestIdx = i;
          }
        }
        return bestIdx;
      }

      svg.addEventListener("mouseleave", hideTooltip);
      svg.addEventListener("mousemove", (event) => {
        const rect = svg.getBoundingClientRect();
        const wrapRect = wrap.getBoundingClientRect();
        if (!rect.width || !rect.height) {
          hideTooltip();
          return;
        }

        const relX = ((event.clientX - rect.left) / rect.width) * width;
        if (relX < m.left || relX > m.left + innerW) {
          hideTooltip();
          return;
        }

        const target = Math.round(((relX - m.left) / innerW) * xMax);
        const idx = findNearestBlendedIndex(Math.max(0, Math.min(xMax, target)));
        if (idx < 0) {
          hideTooltip();
          return;
        }

        const ts = sampleTimes[idx];
        const value = blended[idx];
        const chartX = xScale(idx);
        const chartY = yScale(value);
        hoverGuide.setAttribute("x1", String(chartX));
        hoverGuide.setAttribute("x2", String(chartX));
        hoverGuide.setAttribute("opacity", "0.7");
        hoverMarker.setAttribute("cx", String(chartX));
        hoverMarker.setAttribute("cy", String(chartY));
        hoverMarker.setAttribute("opacity", "1");

        tooltip.innerHTML = `<div class="chart-tooltip-label">${escapeHtml(formatLocalDateTime(ts))}</div><div class="chart-tooltip-value">${escapeHtml(formatStrategyHoverDelta(value, profitBelow100))}</div>`;
        tooltip.classList.add("visible");

        const left = Math.min(wrapRect.width - 12, Math.max(12, event.clientX - wrapRect.left));
        const top = Math.min(wrapRect.height - 12, Math.max(12, event.clientY - wrapRect.top));
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
      });

      const tickTarget = 8;
      const tickEvery = Math.max(1, Math.ceil(maxSteps / tickTarget));
      for (let i = 0; i < maxSteps; i += tickEvery) {
        const x = xScale(i);
        const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
        tick.setAttribute("x1", String(x));
        tick.setAttribute("x2", String(x));
        tick.setAttribute("y1", String(m.top + innerH));
        tick.setAttribute("y2", String(m.top + innerH + 6));
        tick.setAttribute("stroke", "#94a3b8");
        tick.setAttribute("stroke-width", "1");
        svg.appendChild(tick);

        const ts = sampleTimes[i];
        const hm = formatEtHm(ts);
        const dte = dteForTs(dteRefExpiration, ts);
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", String(x));
        label.setAttribute("y", String(m.top + innerH + 22));
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("font-size", "10");
        label.setAttribute("fill", "#64748b");
        label.textContent = `${hm} (DTE ${dte})`;
        label.setAttribute("transform", `rotate(-28 ${x} ${m.top + innerH + 22})`);
        svg.appendChild(label);
      }

      const xAxis = document.createElementNS("http://www.w3.org/2000/svg", "line");
      xAxis.setAttribute("x1", String(m.left));
      xAxis.setAttribute("x2", String(m.left + innerW));
      xAxis.setAttribute("y1", String(m.top + innerH));
      xAxis.setAttribute("y2", String(m.top + innerH));
      xAxis.setAttribute("stroke", "#cbd5e1");
      xAxis.setAttribute("stroke-width", "1");
      svg.appendChild(xAxis);

      let metaMsg = `Blended ${tradeSeries.length} aligned trade series on a 15-minute ET grid.`;
      if (mixedTradeTypes) {
        metaMsg += " Mixed debit/credit entries detected; shading uses reference trade semantics.";
      }
      meta.textContent = metaMsg;
    }

    async function runStrategyAnalysis() {
      const meta = document.getElementById("strategyAnalysisMeta");
      const resolvedLegs = strategyState.legs.filter((leg) => leg.isResolved && Array.isArray(leg.resolved_contracts) && leg.resolved_contracts.length > 0);
      if (!resolvedLegs.length) {
        meta.textContent = "Please resolve at least one leg.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }

      const symbol = document.getElementById("strategySymbol").value || "SPX";
      const snapshotFromDate = document.getElementById("strategySnapshotFromDate").value || "";
      const snapshotToDate = document.getElementById("strategySnapshotToDate").value || "";
      const allDates = Array.isArray(strategyState.snapshotDates) ? strategyState.snapshotDates : [];
      if (!allDates.length) {
        meta.textContent = "No snapshot dates available for this symbol.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      const fromDate = snapshotFromDate || allDates[0];
      const toDate = snapshotToDate || allDates[allDates.length - 1];
      if (fromDate > toDate) {
        meta.textContent = "Snapshot From must not be after Snapshot To.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      const tradeDates = allDates.filter((d) => d >= fromDate && d <= toDate);
      if (!tradeDates.length) {
        meta.textContent = "No snapshot dates in the selected range.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }

      const tradePlans = [];
      let skippedDates = 0;
      for (const tradeDate of tradeDates) {
        const legResults = await Promise.all(
          resolvedLegs.map(async (leg) => {
            const params = new URLSearchParams({
              symbol,
              option_type: String(leg.option_type || "PUT"),
              dte: String(Number(leg.target_dte)),
              target_delta: String(Number(leg.target_delta)),
              entry_time: String(leg.entry_time || ""),
              entry_date: tradeDate,
              target_side: String(leg.side || "BUY"),
              window_minutes: "30",
              strict_dte: "1",
            });
            const res = await fetch(`/api/options/resolve-leg?${params.toString()}`);
            const payload = await res.json();
            if (!res.ok) return null;
            const contracts = Array.isArray(payload.contracts) ? payload.contracts : [];
            if (!contracts.length) return null;
            const best = contracts[0];
            return {
              leg_def: leg,
              sign: leg.side === "SELL" ? -1 : 1,
              quantity: Number(leg.quantity) > 0 ? Number(leg.quantity) : 1,
              streamer_symbol: best.streamer_symbol,
              entry_snapshot_ts: best.snapshot_ts,
              contract: {
                streamer_symbol: best.streamer_symbol,
                option_type: best.option_type,
                strike_price: best.strike_price,
                expiration_date: best.expiration_date,
              },
            };
          })
        );
        const legs = legResults.filter(Boolean);
        if (legs.length !== resolvedLegs.length) {
          skippedDates += 1;
          continue;
        }
        tradePlans.push({
          trade_index: tradePlans.length + 1,
          trade_date: tradeDate,
          legs,
        });
      }
      if (!tradePlans.length) {
        meta.textContent = "No daily trades could be opened at the requested entry time/delta in this range.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }

      const streamers = Array.from(new Set(tradePlans.flatMap((trade) => trade.legs.map((leg) => leg.streamer_symbol))));
      if (!streamers.length) {
        meta.textContent = "No contracts resolved for the current legs.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      if (streamers.length > MAX_STRATEGY_ANALYSIS_STREAMERS) {
        meta.textContent = `Resolved contracts exceed ${MAX_STRATEGY_ANALYSIS_STREAMERS} streamers for series analysis. Reduce selected range or legs.`;
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      const seriesParams = new URLSearchParams({
        symbol,
        streamers: streamers.join(","),
        field: "mid_price",
        from: `${fromDate}T00:00:00`,
        to: `${toDate}T23:59:59`,
      });
      const summaryParams = new URLSearchParams({
        symbol,
        from: `${fromDate}T00:00:00`,
        to: `${toDate}T23:59:59`,
      });

      const [seriesRes, summaryRes] = await Promise.all([
        fetch(`/api/options/series?${seriesParams.toString()}`),
        fetch(`/api/options/summary?${summaryParams.toString()}`),
      ]);
      const seriesData = await seriesRes.json();
      const summaryData = await summaryRes.json();
      if (!seriesRes.ok) {
        meta.textContent = "Error loading series: " + (seriesData.error || "unknown");
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      if (!summaryRes.ok) {
        meta.textContent = "Warning: could not load spot/summary data.";
        meta.className = "meta danger";
      }

      const rows = Array.isArray(seriesData.rows) ? seriesData.rows : [];
      if (!rows.length) {
        meta.textContent = "No data for the selected legs.";
        meta.className = "meta danger";
        renderStrategySeriesTable([]);
        renderStrategyTradeMatrixTable([]);
        renderStrategyIndexChart([]);
        return;
      }
      const transformed = transformStrategySeriesRows(rows, summaryData.market_series || [], tradePlans);
      renderStrategySeriesTable(transformed);
      renderStrategyTradeMatrixTable(transformed);
      renderStrategyIndexChart(transformed);
      meta.textContent = `Built ${tradePlans.length} daily trades from ${tradeDates.length} dates (${skippedDates} skipped), ${streamers.length} contracts, ${rows.length} option rows.`;
      meta.className = "meta success";
    }

    function initStrategyTab() {
      if (tabInitState.strategy) return;
      tabInitState.strategy = true;

      loadStrategySnapshotDateOptions();
      document
        .getElementById("strategySymbol")
        .addEventListener("change", loadStrategySnapshotDateOptions);
      document.getElementById("strategyResolveBtn").addEventListener("click", resolveStrategyLeg);
      document.getElementById("strategyRunBtn").addEventListener("click", runStrategyAnalysis);
      renderStrategyLegsTable();
    }

    async function loadStrategySnapshotDateOptions() {
      const symbol = document.getElementById("strategySymbol").value || "SPX";
      const fromEl = document.getElementById("strategySnapshotFromDate");
      const toEl = document.getElementById("strategySnapshotToDate");
      const fromList = document.getElementById("strategySnapshotFromDateList");
      const toList = document.getElementById("strategySnapshotToDateList");
      if (!fromEl || !toEl || !fromList || !toList) return;

      try {
        const res = await fetch(`/api/options/snapshot-dates?symbol=${encodeURIComponent(symbol)}`);
        const payload = await res.json();
        if (!res.ok) {
          strategyState.snapshotDates = [];
          fromList.innerHTML = "";
          toList.innerHTML = "";
          fromEl.value = "";
          toEl.value = "";
          return;
        }

        const dates = Array.isArray(payload.dates) ? payload.dates : [];
        strategyState.snapshotDates = dates;

        const html = dates.map((d) => `<option value="${escapeHtml(String(d))}"></option>`).join("");
        fromList.innerHTML = html;
        toList.innerHTML = html;

        if (dates.length) {
          fromEl.min = dates[0];
          fromEl.max = dates[dates.length - 1];
          toEl.min = dates[0];
          toEl.max = dates[dates.length - 1];
          if (!dates.includes(fromEl.value)) {
            fromEl.value = dates[0];
          }
          if (!dates.includes(toEl.value)) {
            toEl.value = dates[dates.length - 1];
          }
          if (fromEl.value > toEl.value) {
            toEl.value = fromEl.value;
          }
        } else {
          fromEl.min = "";
          fromEl.max = "";
          toEl.min = "";
          toEl.max = "";
          fromEl.value = "";
          toEl.value = "";
        }
      } catch {
        strategyState.snapshotDates = [];
        fromList.innerHTML = "";
        toList.innerHTML = "";
        fromEl.value = "";
        toEl.value = "";
      }
    }

    function appendAnalyzerOption(list, contract, selectedStreamers) {
      const option = document.createElement("option");
      option.value = contract.streamer_symbol;
      option.textContent = contractLabel(contract);
      option.selected = selectedStreamers.has(contract.streamer_symbol);
      list.appendChild(option);
    }

    function analyzerEnsureLeg(streamer, contract = null) {
      if (!analyzerState.legs.has(streamer)) {
        analyzerState.legs.set(streamer, {
          side: "BUY",
          quantity: 1,
          contract: null,
        });
      }
      const leg = analyzerState.legs.get(streamer);
      if (contract) leg.contract = contract;
      return leg;
    }

    function syncAnalyzerLegsWithSelection() {
      Array.from(analyzerState.legs.keys()).forEach((streamer) => {
        if (!analyzerState.selectedStreamers.has(streamer)) {
          analyzerState.legs.delete(streamer);
        }
      });
      analyzerState.selectedStreamers.forEach((streamer) => {
        const contract = analyzerState.contractByStreamer.get(streamer);
        if (contract) analyzerEnsureLeg(streamer, contract);
      });
    }

    function renderAnalyzerLegsTable() {
      const body = document.querySelector("#analyzerLegsTable tbody");
      body.innerHTML = "";
      Array.from(analyzerState.selectedStreamers).forEach((streamer) => {
        const contract = analyzerState.contractByStreamer.get(streamer);
        if (!contract) return;
        const leg = analyzerEnsureLeg(streamer, contract);
        const buyActive = leg.side === "BUY" ? "active" : "";
        const sellActive = leg.side === "SELL" ? "active" : "";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><button type="button" class="remove-leg" data-analyzer-streamer="${escapeHtml(streamer)}">x</button></td>
          <td>${escapeHtml(contractLabel(contract))}</td>
          <td>
            <div class="side-group">
              <button type="button" class="side-btn buy-btn ${buyActive}" data-analyzer-side-streamer="${escapeHtml(streamer)}" data-side="BUY">BUY</button>
              <button type="button" class="side-btn sell-btn ${sellActive}" data-analyzer-side-streamer="${escapeHtml(streamer)}" data-side="SELL">SELL</button>
            </div>
          </td>
          <td><input class="input qty-input" type="number" min="1" step="1" value="${Number(leg.quantity) || 1}" data-analyzer-qty-streamer="${escapeHtml(streamer)}" /></td>
        `;
        body.appendChild(tr);
      });

      body.querySelectorAll("[data-analyzer-streamer]").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          const streamer = event.currentTarget.getAttribute("data-analyzer-streamer");
          if (!streamer) return;
          analyzerState.selectedStreamers.delete(streamer);
          analyzerState.legs.delete(streamer);
          renderAnalyzerContractSelector();
          renderAnalyzerLegsTable();
        });
      });

      body.querySelectorAll("[data-analyzer-side-streamer]").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          const streamer = event.currentTarget.getAttribute("data-analyzer-side-streamer");
          const side = event.currentTarget.getAttribute("data-side");
          if (!streamer || (side !== "BUY" && side !== "SELL")) return;
          const leg = analyzerEnsureLeg(streamer, analyzerState.contractByStreamer.get(streamer) || null);
          leg.side = side;
          renderAnalyzerLegsTable();
        });
      });

      body.querySelectorAll("[data-analyzer-qty-streamer]").forEach((inputEl) => {
        inputEl.addEventListener("input", (event) => {
          const streamer = event.currentTarget.getAttribute("data-analyzer-qty-streamer");
          if (!streamer) return;
          const leg = analyzerEnsureLeg(streamer, analyzerState.contractByStreamer.get(streamer) || null);
          const parsed = parseInt(event.currentTarget.value || "1", 10);
          leg.quantity = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
        });
      });
    }

    function getAnalyzerFilterState() {
      return {
        symbol: document.getElementById("analyzerSymbol").value || "SPX",
        optionType: document.getElementById("analyzerOptionType").value || "",
        expiration: document.getElementById("analyzerExpiration").value || "",
      };
    }

    function filteredAnalyzerContracts(contracts) {
      const { optionType, expiration } = getAnalyzerFilterState();
      return contracts.filter((contract) => {
        if (optionType && contract.option_type !== optionType) return false;
        if (expiration && String(contract.expiration_date) !== String(expiration)) return false;
        return true;
      });
    }

    function populateAnalyzerExpirationFilter(contracts) {
      const { optionType } = getAnalyzerFilterState();
      const expirationFilter = document.getElementById("analyzerExpiration");
      const selected = expirationFilter.value;
      const values = [];
      const seen = new Set();
      contracts
        .filter((contract) => !optionType || contract.option_type === optionType)
        .forEach((contract) => {
          const value = String(contract.expiration_date || "").trim();
          if (!value || seen.has(value)) return;
          seen.add(value);
          values.push(value);
        });
      values.sort();

      expirationFilter.innerHTML = `<option value="">All Expirations</option>`;
      values.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        expirationFilter.appendChild(option);
      });
      if (selected && values.includes(selected)) expirationFilter.value = selected;
    }

    function renderAnalyzerContractSelector() {
      const list = document.getElementById("analyzerContracts");
      const filtered = filteredAnalyzerContracts(analyzerState.loadedContracts);
      const visibleSelected = new Set(filtered.map((contract) => contract.streamer_symbol));
      list.innerHTML = "";
      filtered.forEach((contract) => appendAnalyzerOption(list, contract, analyzerState.selectedStreamers));
      analyzerState.selectedStreamers.forEach((streamer) => {
        if (visibleSelected.has(streamer)) return;
        const contract = analyzerState.contractByStreamer.get(streamer);
        if (!contract) return;
        appendAnalyzerOption(list, contract, analyzerState.selectedStreamers);
      });
      syncAnalyzerLegsWithSelection();
      renderAnalyzerLegsTable();
    }

    async function loadAnalyzerContracts() {
      const symbol = document.getElementById("analyzerSymbol").value || "SPX";
      const optionType = document.getElementById("analyzerOptionType").value || "";
      const preservedSelections = new Set(analyzerState.selectedStreamers);
      const params = new URLSearchParams({ symbol, limit: "800" });
      if (optionType) params.set("type", optionType);

      const meta = document.getElementById("analyzerMeta");
      meta.textContent = "Loading contracts...";
      meta.className = "meta";
      const res = await fetch(`/api/options/contracts?${params.toString()}`);
      const payload = await res.json();
      if (!res.ok) {
        meta.textContent = "Error loading contracts: " + (payload.error || "unknown");
        meta.className = "meta danger";
        return;
      }

      analyzerState.loadedContracts = Array.isArray(payload.contracts) ? payload.contracts : [];
      analyzerState.contractByStreamer.clear();
      analyzerState.loadedContracts.forEach((contract) => {
        if (contract && contract.streamer_symbol) {
          analyzerState.contractByStreamer.set(contract.streamer_symbol, contract);
        }
      });
      preservedSelections.forEach((streamer) => {
        if (!analyzerState.contractByStreamer.has(streamer)) {
          analyzerState.selectedStreamers.delete(streamer);
        }
      });
      analyzerState.loadedContracts.sort((a, b) => {
        const dt = String(a.expiration_date).localeCompare(String(b.expiration_date));
        if (dt !== 0) return dt;
        return Number(a.strike_price) - Number(b.strike_price);
      });
      populateAnalyzerExpirationFilter(analyzerState.loadedContracts);
      renderAnalyzerContractSelector();
      meta.textContent = `Loaded ${analyzerState.loadedContracts.length} contracts`;
      meta.className = "meta success";
    }

    function bindAnalyzerSelectionUX() {
      const list = document.getElementById("analyzerContracts");
      list.addEventListener("mousedown", (event) => {
        const selectEl = event.currentTarget;
        const target = event.target;
        if (!target || target.tagName !== "OPTION") return;
        const streamer = target.value;
        if (!streamer) return;
        const meta = document.getElementById("analyzerMeta");
        const currentlySelectedCount = analyzerState.selectedStreamers.size;
        const isSelected = analyzerState.selectedStreamers.has(streamer);
        const priorScrollTop = selectEl.scrollTop;

        if (!isSelected && currentlySelectedCount >= MAX_ANALYZER_SELECTED_CONTRACTS) {
          event.preventDefault();
          meta.textContent = `You can select up to ${MAX_ANALYZER_SELECTED_CONTRACTS} contracts.`;
          meta.className = "meta danger";
          return;
        }
        event.preventDefault();
        if (isSelected) {
          analyzerState.selectedStreamers.delete(streamer);
        } else {
          analyzerState.selectedStreamers.add(streamer);
        }
        syncAnalyzerLegsWithSelection();
        renderAnalyzerContractSelector();
        requestAnimationFrame(() => {
          selectEl.scrollTop = priorScrollTop;
          selectEl.focus({ preventScroll: true });
        });
      });

      list.addEventListener("click", (event) => {
        const target = event.target;
        if (!target || target.tagName !== "OPTION") return;
        event.preventDefault();
      });

      list.addEventListener("change", () => {
        const selectedVisible = new Set(Array.from(list.selectedOptions).map((optionEl) => optionEl.value));
        Array.from(list.options).forEach((optionEl) => {
          const streamer = optionEl.value;
          if (!streamer) return;
          if (selectedVisible.has(streamer)) {
            analyzerState.selectedStreamers.add(streamer);
          } else {
            analyzerState.selectedStreamers.delete(streamer);
          }
        });
        syncAnalyzerLegsWithSelection();
        renderAnalyzerLegsTable();
      });
    }

    function transformAnalyzerRows(rows, spotSeries) {
      const grouped = {};
      rows.forEach((row) => {
        const key = row.streamer_symbol;
        if (!grouped[key]) grouped[key] = [];
        grouped[key].push(row);
      });
      const nearestSpot = buildSpotLookup(spotSeries || []);
      const transformed = [];
      Object.entries(grouped).forEach(([, contractRows]) => {
        const sorted = [...contractRows].sort((a, b) => parseTimestamp(a.snapshot_ts) - parseTimestamp(b.snapshot_ts));
        const first = sorted.find((row) => row.value !== null && Number.isFinite(Number(row.value)));
        const firstValue = first && first.value != null && Number.isFinite(Number(first.value)) ? Number(first.value) : null;

        sorted.forEach((row) => {
          const value = row.value == null ? null : Number(row.value);
          const indexed = value != null && firstValue != null && firstValue !== 0 && Number.isFinite(value) && Number.isFinite(firstValue)
            ? (value / firstValue) * 100
            : null;
          const spread = row.bid_price == null || row.ask_price == null ? null : Number(row.ask_price) - Number(row.bid_price);
          transformed.push({
            ...row,
            indexed,
            spread,
            spot_price: nearestSpot(row.snapshot_ts),
          });
        });
      });
      return transformed.sort((a, b) => {
        const aTs = parseTimestamp(a.snapshot_ts);
        const bTs = parseTimestamp(b.snapshot_ts);
        if ((aTs - bTs) !== 0) return aTs - bTs;
        const aLabel = contractLabel(a);
        const bLabel = contractLabel(b);
        return aLabel.localeCompare(bLabel);
      });
    }

    function renderAnalyzerSeriesTable(rows) {
      const body = document.querySelector("#analyzerSeriesTable tbody");
      body.innerHTML = "";
      rows.forEach((row) => {
        const spread = row.spread == null ? "" : Number(row.spread).toFixed(4);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.snapshot_ts ? formatLocalDateTime(row.snapshot_ts) : ""}</td>
          <td>${escapeHtml(contractLabel(row))}</td>
          <td>${row.spot_price == null ? "" : Number(row.spot_price).toFixed(4)}</td>
          <td>${row.value == null ? "" : Number(row.value).toFixed(4)}</td>
          <td>${row.indexed == null ? "" : Number(row.indexed).toFixed(4)}</td>
          <td>${row.delta == null ? "" : Number(row.delta).toFixed(4)}</td>
          <td>${row.gamma == null ? "" : Number(row.gamma).toFixed(4)}</td>
          <td>${row.theta == null ? "" : Number(row.theta).toFixed(4)}</td>
          <td>${row.vega == null ? "" : Number(row.vega).toFixed(4)}</td>
          <td>${row.volatility == null ? "" : Number(row.volatility).toFixed(4)}</td>
          <td>${spread}</td>
        `;
        body.appendChild(tr);
      });
    }

    function transformAnalyzerStrategyRows(rows, spotSeries) {
      const grouped = {};
      rows.forEach((row) => {
        const key = row.streamer_symbol;
        if (!grouped[key]) grouped[key] = [];
        grouped[key].push(row);
      });
      const nearestSpot = buildSpotLookup(spotSeries || []);
      const transformedRows = [];
      const legMetaByStreamer = {};

      Object.entries(grouped).forEach(([streamer, contractRows]) => {
        const leg = analyzerState.legs.get(streamer);
        const contract = analyzerState.contractByStreamer.get(streamer);
        if (!leg || !contract) return;
        const sorted = [...contractRows].sort((a, b) => parseTimestamp(a.snapshot_ts) - parseTimestamp(b.snapshot_ts));
        const sign = leg.side === "SELL" ? -1 : 1;
        const quantity = Number(leg.quantity) > 0 ? Number(leg.quantity) : 1;
        const first = sorted.find((row) => row.value !== null && Number.isFinite(Number(row.value)));
        const entryValue = first ? Number(first.value) : null;
        legMetaByStreamer[streamer] = {
          side: leg.side,
          quantity,
          contract,
          sign,
          entryValue,
        };

        sorted.forEach((row) => {
          const value = row.value == null ? null : Number(row.value);
          const contribution = value != null && Number.isFinite(value) ? sign * quantity * value : null;
          const indexed = value != null && entryValue != null && entryValue !== 0 && Number.isFinite(value) && Number.isFinite(entryValue)
            ? (value / entryValue) * 100
            : null;
          transformedRows.push({
            ...row,
            indexed,
            spot_price: nearestSpot(row.snapshot_ts),
            leg_contribution: contribution,
            isStrategySummary: false,
          });
        });
      });

      const bySnapshot = new Map();
      transformedRows.forEach((row) => {
        const key = row.snapshot_ts || "";
        if (!bySnapshot.has(key)) bySnapshot.set(key, []);
        bySnapshot.get(key).push(row);
      });

      const spreadRows = [];
      const sortedTs = Array.from(bySnapshot.keys()).sort((a, b) => parseTimestamp(a) - parseTimestamp(b));
      sortedTs.forEach((ts) => {
        const rowsForTs = bySnapshot.get(ts) || [];
        rowsForTs.sort((a, b) => contractLabel(a).localeCompare(contractLabel(b)));
        rowsForTs.forEach((row) => spreadRows.push(row));

        let hasAllContributions = true;
        let strategyValue = 0;
        let strategyCost = null;
        rowsForTs.forEach((row) => {
          const meta = legMetaByStreamer[row.streamer_symbol];
          if (!meta) return;
          if (row.leg_contribution == null || !Number.isFinite(row.leg_contribution)) {
            hasAllContributions = false;
            return;
          }
          strategyValue += row.leg_contribution;
          if (meta.entryValue != null && Number.isFinite(meta.entryValue)) {
            strategyCost = (strategyCost == null ? 0 : strategyCost) + meta.sign * meta.quantity * meta.entryValue;
          }
        });

        const strategyIdx = strategyCost && Number.isFinite(strategyCost) && strategyCost !== 0 ? (strategyValue / strategyCost) * 100 : null;
        spreadRows.push({
          snapshot_ts: ts,
          spot_price: rowsForTs.length ? rowsForTs[0].spot_price : null,
          value: hasAllContributions ? strategyValue : null,
          indexed: strategyIdx,
          strategy_price: hasAllContributions ? strategyValue : null,
          strategy_cost: strategyCost,
          strategy_pnl: strategyCost != null && hasAllContributions ? strategyValue - strategyCost : null,
          strategy_indexed: strategyIdx,
          isStrategySummary: true,
        });
      });

      return spreadRows;
    }

    function renderAnalyzerStrategySeriesTable(rows) {
      const body = document.querySelector("#analyzerStrategySeriesTable tbody");
      body.innerHTML = "";
      rows.forEach((row) => {
        const spread = row.bid_price == null || row.ask_price == null ? "" : (Number(row.ask_price) - Number(row.bid_price)).toFixed(4);
        const strategy = row.strategy_price == null ? "" : Number(row.strategy_price).toFixed(4);
        const strategyCost = row.strategy_cost == null ? "" : Number(row.strategy_cost).toFixed(4);
        const strategyPnl = row.strategy_pnl == null ? "" : Number(row.strategy_pnl).toFixed(4);
        const strategyIndexed = row.strategy_indexed == null ? "" : Number(row.strategy_indexed).toFixed(4);
        const contribution = row.leg_contribution == null ? "" : Number(row.leg_contribution).toFixed(4);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.snapshot_ts ? formatLocalDateTime(row.snapshot_ts) : ""}</td>
          <td>${row.isStrategySummary ? "Strategy" : escapeHtml(contractLabel(row))}</td>
          <td>${row.spot_price == null ? "" : Number(row.spot_price).toFixed(4)}</td>
          <td>${row.value == null ? "" : Number(row.value).toFixed(4)}</td>
          <td>${row.indexed == null ? "" : Number(row.indexed).toFixed(4)}</td>
          <td>${contribution}</td>
          <td>${strategy}</td>
          <td>${strategyCost}</td>
          <td>${strategyPnl}</td>
          <td>${strategyIndexed}</td>
          <td>${spread}</td>
        `;
        if (row.isStrategySummary) tr.className = "strategy-summary";
        body.appendChild(tr);
      });
    }

    async function runAnalyzerAnalysis() {
      const meta = document.getElementById("analyzerSeriesMeta");
      const streamers = Array.from(analyzerState.selectedStreamers).filter((streamer) => analyzerState.contractByStreamer.has(streamer));
      if (!streamers.length) {
        meta.textContent = "Please select at least one contract.";
        meta.className = "meta danger";
        renderAnalyzerSeriesTable([]);
        return;
      }

      const symbol = document.getElementById("analyzerSymbol").value || "SPX";
      const seriesParams = new URLSearchParams({ symbol, streamers: streamers.join(","), field: "mid_price" });
      const [seriesRes, summaryRes] = await Promise.all([
        fetch(`/api/options/series?${seriesParams.toString()}`),
        fetch(`/api/options/summary?${new URLSearchParams({ symbol })}`),
      ]);
      const seriesData = await seriesRes.json();
      const summaryData = await summaryRes.json();
      if (!seriesRes.ok) {
        meta.textContent = "Error loading series: " + (seriesData.error || "unknown");
        meta.className = "meta danger";
        return;
      }
      if (!summaryRes.ok) {
        meta.textContent = "Warning: could not load spot/summary data.";
        meta.className = "meta danger";
      }

      const rows = Array.isArray(seriesData.rows) ? seriesData.rows : [];
      if (!rows.length) {
        meta.textContent = "No data for the selected contracts.";
        meta.className = "meta danger";
        renderAnalyzerSeriesTable([]);
        return;
      }

      const transformed = transformAnalyzerRows(rows, summaryData.market_series || []);
      renderAnalyzerSeriesTable(transformed);
      meta.textContent = `Loaded ${streamers.length} contracts, ${rows.length} rows.`;
      meta.className = "meta success";
    }

    async function runAnalyzerStrategyAnalysis() {
      const meta = document.getElementById("analyzerStrategyMeta");
      const streamers = Array.from(analyzerState.selectedStreamers).filter((streamer) => analyzerState.contractByStreamer.has(streamer));
      if (!streamers.length) {
        meta.textContent = "Please select at least one contract.";
        meta.className = "meta danger";
        renderAnalyzerStrategySeriesTable([]);
        return;
      }

      const usableLegs = streamers.filter((streamer) => {
        const leg = analyzerState.legs.get(streamer);
        return leg && Number(leg.quantity) > 0 && (leg.side === "BUY" || leg.side === "SELL");
      });
      if (!usableLegs.length) {
        meta.textContent = "Please set at least one valid leg (BUY/SELL with qty > 0).";
        meta.className = "meta danger";
        renderAnalyzerStrategySeriesTable([]);
        return;
      }

      const symbol = document.getElementById("analyzerSymbol").value || "SPX";
      const seriesParams = new URLSearchParams({ symbol, streamers: usableLegs.join(","), field: "mid_price" });
      const [seriesRes, summaryRes] = await Promise.all([
        fetch(`/api/options/series?${seriesParams.toString()}`),
        fetch(`/api/options/summary?${new URLSearchParams({ symbol })}`),
      ]);
      const seriesData = await seriesRes.json();
      const summaryData = await summaryRes.json();
      if (!seriesRes.ok) {
        meta.textContent = "Error loading strategy series: " + (seriesData.error || "unknown");
        meta.className = "meta danger";
        renderAnalyzerStrategySeriesTable([]);
        return;
      }
      if (!summaryRes.ok) {
        meta.textContent = "Warning: could not load spot/summary data.";
        meta.className = "meta danger";
      }

      const rows = Array.isArray(seriesData.rows) ? seriesData.rows : [];
      if (!rows.length) {
        meta.textContent = "No data for selected strategy legs.";
        meta.className = "meta danger";
        renderAnalyzerStrategySeriesTable([]);
        return;
      }

      const transformed = transformAnalyzerStrategyRows(rows, summaryData.market_series || []);
      renderAnalyzerStrategySeriesTable(transformed);
      meta.textContent = `Loaded ${usableLegs.length} strategy legs, ${rows.length} rows.`;
      meta.className = "meta success";
    }

    function initAnalyzerTab() {
      if (tabInitState.analyzer) return;
      tabInitState.analyzer = true;

      bindAnalyzerSelectionUX();
      document.getElementById("analyzerOptionType").addEventListener("change", loadAnalyzerContracts);
      document.getElementById("analyzerExpiration").addEventListener("change", () => {
        populateAnalyzerExpirationFilter(analyzerState.loadedContracts);
        renderAnalyzerContractSelector();
      });
      document.getElementById("analyzerSymbol").addEventListener("change", loadAnalyzerContracts);
      document.getElementById("analyzerRunStrategyBtn").addEventListener("click", runAnalyzerStrategyAnalysis);
      renderAnalyzerLegsTable();
      loadAnalyzerContracts();
    }

    function initPage() {
      initTabs();
      initStrategyTab();
      initAnalyzerTab();
      initSqlTab();
    }

    initPage();
  </script>
</body>
</html>
"""
if __name__ == "__main__":
    main()
