from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any


ALLOWED_EVENT_NAMES = {
    "page_view",
    "strategy_leg_add_attempt",
    "strategy_leg_add_result",
    "strategy_run_attempt",
    "strategy_run_result",
    "strategy_share_attempt",
    "strategy_share_result",
    "strategy_share_open",
}
DEFAULT_METRICS_WINDOW_DAYS = 14
MAX_STRING_LENGTH = 255
MAX_LIST_ITEMS = 24
MAX_DICT_ITEMS = 32
MAX_DATA_BYTES = 16_384

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS site_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at_utc TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,
    event_name TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    anonymous_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    page_path TEXT NOT NULL,
    referrer_host TEXT,
    outcome TEXT,
    event_data_json TEXT NOT NULL
)
"""

_CREATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_site_usage_events_occurred_at ON site_usage_events (occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_site_usage_events_event_time ON site_usage_events (event_name, occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_site_usage_events_session_time ON site_usage_events (session_id, occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_site_usage_events_anon_time ON site_usage_events (anonymous_id, occurred_at_utc)",
)

METRICS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Local Tracking Metrics</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --ink: #171717;
      --muted: #57534e;
      --line: rgba(23, 23, 23, 0.1);
      --bg: #f5f3f0;
      --card: rgba(255, 255, 255, 0.88);
      --accent: #ea580c;
      --accent-2: #0f766e;
      --accent-3: #1d4ed8;
      --accent-4: #7c3aed;
      --shadow: 0 18px 44px rgba(28, 25, 23, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Plus Jakarta Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(60rem 24rem at 0% 0%, rgba(251, 146, 60, 0.12), transparent 55%),
        radial-gradient(54rem 22rem at 100% 0%, rgba(13, 148, 136, 0.1), transparent 50%),
        var(--bg);
    }
    .wrap {
      max-width: 1320px;
      margin: 0 auto;
      padding: 24px;
    }
    .hero {
      border-radius: 26px;
      padding: 24px;
      color: #fafaf9;
      background: linear-gradient(135deg, rgba(28,25,23,0.95), rgba(68,64,60,0.88));
      box-shadow: var(--shadow);
    }
    .hero-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    .hero h1 {
      margin: 0;
      font-size: clamp(1.8rem, 3vw, 2.8rem);
      letter-spacing: -0.04em;
    }
    .hero p {
      margin: 10px 0 0;
      max-width: 58ch;
      color: rgba(245, 245, 244, 0.8);
      line-height: 1.6;
    }
    .hero a {
      color: #fed7aa;
      text-decoration: none;
      font-weight: 600;
    }
    .surface {
      margin-top: 20px;
      border-radius: 30px;
      padding: 18px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.56);
      backdrop-filter: blur(12px);
    }
    .filters,
    .cards,
    .grid {
      display: grid;
      gap: 14px;
    }
    .filters {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      align-items: end;
    }
    .cards {
      margin-top: 16px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .grid {
      margin-top: 16px;
      grid-template-columns: 1.35fr 1fr;
    }
    .card {
      border-radius: 22px;
      border: 1px solid var(--line);
      background: var(--card);
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .label {
      margin-bottom: 8px;
      display: block;
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .input,
    button {
      width: 100%;
      border-radius: 16px;
      border: 1px solid var(--line);
      padding: 12px 14px;
      font: inherit;
    }
    .input {
      background: #fff;
      color: var(--ink);
    }
    button {
      border: 0;
      color: #fff;
      background: linear-gradient(135deg, #f97316, #c2410c);
      cursor: pointer;
      font-weight: 700;
      box-shadow: 0 12px 24px rgba(234, 88, 12, 0.18);
    }
    .metric-label {
      font-size: 0.76rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .metric-value {
      margin-top: 10px;
      font-size: clamp(1.2rem, 2vw, 1.8rem);
      font-weight: 700;
      letter-spacing: -0.03em;
    }
    .metric-note {
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.9rem;
    }
    .chart-wrap {
      margin-top: 14px;
      border-radius: 18px;
      padding: 14px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.82);
    }
    svg {
      width: 100%;
      height: 320px;
      display: block;
    }
    .legend {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 0.9rem;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 999px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 0.92rem;
    }
    th,
    td {
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .status {
      margin-top: 12px;
      min-height: 24px;
      color: var(--muted);
      font-size: 0.94rem;
    }
    .danger { color: #b91c1c; }
    .empty {
      color: var(--muted);
      font-style: italic;
    }
    @media (max-width: 980px) {
      .filters,
      .cards,
      .grid {
        grid-template-columns: 1fr;
      }
      .hero-top {
        align-items: flex-start;
        flex-direction: column;
      }
      svg {
        height: 240px;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-top">
        <div>
          <h1>Local Tracking Metrics</h1>
          <p>Usage metrics for the local tracking database. This page reads first-party tracking events only and does not touch production infrastructure.</p>
        </div>
        <a href="/">Back To App</a>
      </div>
    </section>

    <section class="surface">
      <div class="filters">
        <div>
          <label class="label" for="fromDate">From</label>
          <input id="fromDate" class="input" type="date" />
        </div>
        <div>
          <label class="label" for="toDate">To</label>
          <input id="toDate" class="input" type="date" />
        </div>
        <div>
          <label class="label" for="rangePreset">Preset</label>
          <select id="rangePreset" class="input">
            <option value="7">Last 7 days</option>
            <option value="14" selected>Last 14 days</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
          </select>
        </div>
        <div>
          <label class="label" for="refreshBtn">Refresh</label>
          <button id="refreshBtn" type="button">Refresh Metrics</button>
        </div>
      </div>

      <div id="cards" class="cards"></div>

      <div class="grid">
        <div class="card">
          <div class="metric-label">Daily Activity</div>
          <div class="chart-wrap">
            <svg id="timeseriesChart" viewBox="0 0 900 320" preserveAspectRatio="none"></svg>
            <div class="legend">
              <span class="legend-item"><span class="swatch" style="background:#ea580c;"></span>Visitors</span>
              <span class="legend-item"><span class="swatch" style="background:#0f766e;"></span>Sessions</span>
              <span class="legend-item"><span class="swatch" style="background:#1d4ed8;"></span>Add Leg</span>
              <span class="legend-item"><span class="swatch" style="background:#7c3aed;"></span>Run Strategy</span>
              <span class="legend-item"><span class="swatch" style="background:#0284c7;"></span>Share Clicks</span>
              <span class="legend-item"><span class="swatch" style="background:#16a34a;"></span>Share Opens</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="metric-label">Common Legs</div>
          <table>
            <thead>
              <tr><th>Leg</th><th>Count</th></tr>
            </thead>
            <tbody id="commonLegsBody"></tbody>
          </table>
        </div>
      </div>

      <div class="card" style="margin-top:16px;">
        <div class="metric-label">Recent Run Outcomes</div>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Outcome</th>
              <th>Symbol</th>
              <th>Legs</th>
              <th>Exit</th>
              <th>Range</th>
              <th>Counts</th>
            </tr>
          </thead>
          <tbody id="recentRunsBody"></tbody>
        </table>
      </div>

      <div id="status" class="status"></div>
    </section>
  </div>

  <script>
    function formatDateInputValue(date) {
      return date.toISOString().slice(0, 10);
    }

    function applyPreset(days) {
      const to = new Date();
      const from = new Date(to);
      from.setUTCDate(from.getUTCDate() - (days - 1));
      document.getElementById("fromDate").value = formatDateInputValue(from);
      document.getElementById("toDate").value = formatDateInputValue(to);
    }

    function metricCard(label, value, note) {
      return `
        <div class="card">
          <div class="metric-label">${label}</div>
          <div class="metric-value">${value}</div>
          <div class="metric-note">${note || ""}</div>
        </div>
      `;
    }

    function formatPercent(value) {
      if (value == null || Number.isNaN(Number(value))) return "0.0%";
      return `${Number(value).toFixed(1)}%`;
    }

    function fetchJson(path) {
      return fetch(path).then(async (res) => {
        const payload = await res.json();
        if (!res.ok) {
          throw new Error(payload.error || "Request failed.");
        }
        return payload;
      });
    }

    function buildRangeQuery() {
      const from = document.getElementById("fromDate").value;
      const to = document.getElementById("toDate").value;
      return new URLSearchParams({ from, to }).toString();
    }

    function renderCards(payload) {
      const cards = document.getElementById("cards");
      cards.innerHTML = [
        metricCard("Pageviews", payload.pageviews, `${payload.range.from} to ${payload.range.to}`),
        metricCard("Sessions", payload.sessions, "Distinct session IDs"),
        metricCard("Unique Visitors", payload.unique_visitors, "Distinct anonymous browser IDs"),
        metricCard("Add Leg Clicks", payload.add_leg_attempts, `Success rate ${formatPercent(payload.add_leg_success_rate)}`),
        metricCard("Run Clicks", payload.run_attempts, `Success rate ${formatPercent(payload.run_success_rate)}`),
        metricCard("Share Clicks", payload.share_attempts, `Success rate ${formatPercent(payload.share_success_rate)}`),
        metricCard("Share Successes", payload.share_successes, "Created shareable links"),
        metricCard("Shared Link Opens", payload.share_opens, "Visitors who opened a shared strategy"),
        metricCard("Add Leg Successes", payload.add_leg_successes, "Successful leg resolutions"),
        metricCard("Run Successes", payload.run_successes, "Completed strategy analyses"),
        metricCard("Run Empty/Error", payload.run_non_success, "Empty or failed strategy runs"),
      ].join("");
    }

    function renderCommonLegs(rows) {
      const body = document.getElementById("commonLegsBody");
      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="2" class="empty">No run attempts in this range.</td></tr>';
        return;
      }
      body.innerHTML = rows.map((row) => `
        <tr>
          <td>${row.label}</td>
          <td>${row.count}</td>
        </tr>
      `).join("");
    }

    function renderRecentRuns(rows) {
      const body = document.getElementById("recentRunsBody");
      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="7" class="empty">No run result events in this range.</td></tr>';
        return;
      }
      body.innerHTML = rows.map((row) => `
        <tr>
          <td>${row.occurred_at_local || row.occurred_at_utc}</td>
          <td>${row.outcome || ""}</td>
          <td>${row.symbol || ""}</td>
          <td>${(row.leg_labels || []).join("<br/>")}</td>
          <td>${row.exit_label || ""}</td>
          <td>${row.range_label || ""}</td>
          <td>${row.counts_label || ""}</td>
        </tr>
      `).join("");
    }

    function renderTimeseries(rows) {
      const svg = document.getElementById("timeseriesChart");
      svg.innerHTML = "";
      if (!rows.length) {
        svg.innerHTML = '<text x="24" y="36" fill="#78716c" font-size="16">No data for the selected range.</text>';
        return;
      }

      const width = 900;
      const height = 320;
      const padLeft = 44;
      const padRight = 18;
      const padTop = 18;
      const padBottom = 38;
      const chartWidth = width - padLeft - padRight;
      const chartHeight = height - padTop - padBottom;
      const maxValue = Math.max(
        1,
        ...rows.map((row) => Math.max(
          Number(row.unique_visitors) || 0,
          Number(row.sessions) || 0,
          Number(row.add_leg_attempts) || 0,
          Number(row.run_attempts) || 0,
          Number(row.share_attempts) || 0,
          Number(row.share_opens) || 0
        ))
      );

      function x(index) {
        if (rows.length === 1) return padLeft + chartWidth / 2;
        return padLeft + (index / (rows.length - 1)) * chartWidth;
      }

      function y(value) {
        return padTop + chartHeight - ((Number(value) || 0) / maxValue) * chartHeight;
      }

      const series = [
        { key: "unique_visitors", color: "#ea580c" },
        { key: "sessions", color: "#0f766e" },
        { key: "add_leg_attempts", color: "#1d4ed8" },
        { key: "run_attempts", color: "#7c3aed" },
        { key: "share_attempts", color: "#0284c7" },
        { key: "share_opens", color: "#16a34a" },
      ];

      const grid = document.createElementNS("http://www.w3.org/2000/svg", "g");
      for (let i = 0; i <= 4; i += 1) {
        const yPos = padTop + (i / 4) * chartHeight;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", String(padLeft));
        line.setAttribute("x2", String(width - padRight));
        line.setAttribute("y1", String(yPos));
        line.setAttribute("y2", String(yPos));
        line.setAttribute("stroke", "rgba(23,23,23,0.12)");
        line.setAttribute("stroke-width", "1");
        grid.appendChild(line);
      }
      svg.appendChild(grid);

      series.forEach((seriesDef) => {
        const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        const points = rows.map((row, index) => `${x(index)},${y(row[seriesDef.key])}`).join(" ");
        polyline.setAttribute("fill", "none");
        polyline.setAttribute("stroke", seriesDef.color);
        polyline.setAttribute("stroke-width", "3");
        polyline.setAttribute("stroke-linejoin", "round");
        polyline.setAttribute("stroke-linecap", "round");
        polyline.setAttribute("points", points);
        svg.appendChild(polyline);
      });

      const axis = document.createElementNS("http://www.w3.org/2000/svg", "line");
      axis.setAttribute("x1", String(padLeft));
      axis.setAttribute("x2", String(width - padRight));
      axis.setAttribute("y1", String(height - padBottom));
      axis.setAttribute("y2", String(height - padBottom));
      axis.setAttribute("stroke", "rgba(23,23,23,0.22)");
      axis.setAttribute("stroke-width", "1");
      svg.appendChild(axis);

      const lastRow = rows[rows.length - 1];
      const topLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      topLabel.textContent = `Max daily count: ${maxValue}`;
      topLabel.setAttribute("x", String(padLeft));
      topLabel.setAttribute("y", "14");
      topLabel.setAttribute("fill", "#57534e");
      topLabel.setAttribute("font-size", "12");
      svg.appendChild(topLabel);

      const firstLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      firstLabel.textContent = rows[0].date || "";
      firstLabel.setAttribute("x", String(padLeft));
      firstLabel.setAttribute("y", String(height - 12));
      firstLabel.setAttribute("fill", "#57534e");
      firstLabel.setAttribute("font-size", "12");
      svg.appendChild(firstLabel);

      const lastLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      lastLabel.textContent = lastRow.date || "";
      lastLabel.setAttribute("x", String(width - padRight));
      lastLabel.setAttribute("y", String(height - 12));
      lastLabel.setAttribute("text-anchor", "end");
      lastLabel.setAttribute("fill", "#57534e");
      lastLabel.setAttribute("font-size", "12");
      svg.appendChild(lastLabel);
    }

    async function loadMetrics() {
      const status = document.getElementById("status");
      status.textContent = "Loading metrics...";
      status.className = "status";
      const query = buildRangeQuery();
      try {
        const [overview, timeseries, runs, commonLegs] = await Promise.all([
          fetchJson(`/api/ops/metrics/overview?${query}`),
          fetchJson(`/api/ops/metrics/timeseries?${query}`),
          fetchJson(`/api/ops/metrics/runs?${query}`),
          fetchJson(`/api/ops/metrics/common-legs?${query}`),
        ]);
        renderCards(overview);
        renderTimeseries(timeseries.rows || []);
        renderRecentRuns(runs.rows || []);
        renderCommonLegs(commonLegs.rows || []);
        status.textContent = `Loaded ${overview.range.from} to ${overview.range.to}.`;
      } catch (error) {
        status.textContent = error.message || "Failed to load metrics.";
        status.className = "status danger";
      }
    }

    document.getElementById("rangePreset").addEventListener("change", (event) => {
      const days = parseInt(event.target.value || "14", 10);
      applyPreset(Number.isFinite(days) && days > 0 ? days : 14);
    });
    document.getElementById("refreshBtn").addEventListener("click", loadMetrics);

    applyPreset(14);
    loadMetrics();
  </script>
</body>
</html>
"""


def _resolve_sqlite_path(db_url: str) -> Path:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError(f"Tracking currently supports sqlite only. TRACKING_DB_URL was: {db_url!r}")
    raw_path = db_url[len(prefix) :]
    return Path(raw_path).expanduser().resolve()


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_iso_datetime(value: str | None, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required.")
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_date(value: str | None, label: str) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD.") from exc


def parse_metrics_date_range(
    from_value: str | None, to_value: str | None, *, now: datetime | None = None
) -> tuple[date, date]:
    current = (now or _utc_now()).date()
    to_date = _parse_date(to_value, "to") if to_value else current
    from_date = (
        _parse_date(from_value, "from")
        if from_value
        else to_date - timedelta(days=DEFAULT_METRICS_WINDOW_DAYS - 1)
    )
    if from_date > to_date:
        raise ValueError("from must not be after to.")
    return from_date, to_date


def ensure_tracking_db(db_url: str) -> Path:
    db_path = _resolve_sqlite_path(db_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_EVENTS_TABLE_SQL)
        for sql in _CREATE_INDEXES_SQL:
            conn.execute(sql)
        conn.commit()
    return db_path


def _sanitize_string(value: Any, *, allow_empty: bool = False, label: str = "value") -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise ValueError(f"{label} is required.")
    return text[:MAX_STRING_LENGTH]


def _sanitize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        raise ValueError("event data is nested too deeply.")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    if isinstance(value, list):
        return [_sanitize_json(item, depth=depth + 1) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                break
            out[str(key)[:64]] = _sanitize_json(item, depth=depth + 1)
        return out
    return str(value)[:MAX_STRING_LENGTH]


def validate_tracking_payload(
    payload: dict[str, Any], *, received_at: datetime | None = None
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("tracking payload must be a JSON object.")

    event_name = _sanitize_string(payload.get("event_name"), label="event_name")
    if event_name not in ALLOWED_EVENT_NAMES:
        raise ValueError("event_name is not allowed.")

    try:
        event_version = int(payload.get("event_version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("event_version must be an integer.") from exc
    if event_version < 1:
        raise ValueError("event_version must be >= 1.")

    occurred_at = _parse_iso_datetime(payload.get("occurred_at"), "occurred_at")
    stored_at = (received_at or _utc_now()).astimezone(UTC)

    anonymous_id = _sanitize_string(payload.get("anonymous_id"), label="anonymous_id")
    session_id = _sanitize_string(payload.get("session_id"), label="session_id")
    page_path = _sanitize_string(payload.get("page_path"), label="page_path")
    if not page_path.startswith("/"):
        raise ValueError("page_path must start with '/'.")

    referrer_host_raw = str(payload.get("referrer_host") or "").strip()
    referrer_host = referrer_host_raw[:MAX_STRING_LENGTH] if referrer_host_raw else None

    outcome_raw = payload.get("outcome")
    outcome = None
    if outcome_raw is not None:
        outcome = _sanitize_string(outcome_raw, label="outcome")

    data = payload.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("data must be an object when provided.")
    normalized_data = _sanitize_json(data)
    serialized_data = json.dumps(normalized_data, separators=(",", ":"), sort_keys=True)
    if len(serialized_data.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError("data payload is too large.")

    return {
        "occurred_at_utc": occurred_at.isoformat().replace("+00:00", "Z"),
        "received_at_utc": stored_at.isoformat().replace("+00:00", "Z"),
        "event_name": event_name,
        "event_version": event_version,
        "anonymous_id": anonymous_id,
        "session_id": session_id,
        "page_path": page_path,
        "referrer_host": referrer_host,
        "outcome": outcome,
        "event_data_json": serialized_data,
    }


def insert_tracking_event(db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_tracking_payload(payload)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO site_usage_events (
                occurred_at_utc,
                received_at_utc,
                event_name,
                event_version,
                anonymous_id,
                session_id,
                page_path,
                referrer_host,
                outcome,
                event_data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["occurred_at_utc"],
                normalized["received_at_utc"],
                normalized["event_name"],
                normalized["event_version"],
                normalized["anonymous_id"],
                normalized["session_id"],
                normalized["page_path"],
                normalized["referrer_host"],
                normalized["outcome"],
                normalized["event_data_json"],
            ),
        )
        conn.commit()
        return {"ok": True, "id": int(cursor.lastrowid)}


def _range_bounds(from_date: date, to_date: date) -> tuple[str, str]:
    start = datetime.combine(from_date, time.min, tzinfo=UTC)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


def _format_local_timestamp(value: str) -> str:
    dt = _parse_iso_datetime(value, "occurred_at_utc")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _load_events(
    db_path: Path,
    *,
    from_date: date,
    to_date: date,
    event_names: set[str] | None = None,
    limit: int | None = None,
    descending: bool = False,
) -> list[dict[str, Any]]:
    start_iso, end_iso = _range_bounds(from_date, to_date)
    sql = [
        """
        SELECT
            id,
            occurred_at_utc,
            received_at_utc,
            event_name,
            event_version,
            anonymous_id,
            session_id,
            page_path,
            referrer_host,
            outcome,
            event_data_json
        FROM site_usage_events
        WHERE occurred_at_utc >= ? AND occurred_at_utc < ?
        """
    ]
    params: list[Any] = [start_iso, end_iso]
    if event_names:
        ordered_names = sorted(event_names)
        placeholders = ",".join("?" for _ in ordered_names)
        sql.append(f"AND event_name IN ({placeholders})")
        params.extend(ordered_names)
    order = "DESC" if descending else "ASC"
    sql.append(f"ORDER BY occurred_at_utc {order}, id {order}")
    if limit is not None:
        sql.append("LIMIT ?")
        params.append(int(limit))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("\n".join(sql), params).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        data = {}
        raw_data = str(row["event_data_json"] or "").strip()
        if raw_data:
            try:
                parsed = json.loads(raw_data)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                data = {}
        events.append(
            {
                "id": int(row["id"]),
                "occurred_at_utc": str(row["occurred_at_utc"]),
                "received_at_utc": str(row["received_at_utc"]),
                "event_name": str(row["event_name"]),
                "event_version": int(row["event_version"]),
                "anonymous_id": str(row["anonymous_id"]),
                "session_id": str(row["session_id"]),
                "page_path": str(row["page_path"]),
                "referrer_host": str(row["referrer_host"] or ""),
                "outcome": str(row["outcome"] or ""),
                "data": data,
            }
        )
    return events


def build_overview_payload(db_path: Path, *, from_date: date, to_date: date) -> dict[str, Any]:
    events = _load_events(db_path, from_date=from_date, to_date=to_date)
    pageviews = sum(1 for event in events if event["event_name"] == "page_view")
    sessions = {event["session_id"] for event in events}
    unique_visitors = {event["anonymous_id"] for event in events}
    add_leg_attempts = sum(
        1 for event in events if event["event_name"] == "strategy_leg_add_attempt"
    )
    add_leg_successes = sum(
        1
        for event in events
        if event["event_name"] == "strategy_leg_add_result"
        and event["outcome"] == "success"
    )
    run_attempts = sum(
        1 for event in events if event["event_name"] == "strategy_run_attempt"
    )
    run_successes = sum(
        1
        for event in events
        if event["event_name"] == "strategy_run_result"
        and event["outcome"] == "success"
    )
    share_attempts = sum(
        1 for event in events if event["event_name"] == "strategy_share_attempt"
    )
    share_successes = sum(
        1
        for event in events
        if event["event_name"] == "strategy_share_result"
        and event["outcome"] == "success"
    )
    share_opens = sum(
        1
        for event in events
        if event["event_name"] == "strategy_share_open"
        and event["outcome"] == "success"
    )
    run_result_events = sum(
        1 for event in events if event["event_name"] == "strategy_run_result"
    )
    add_leg_success_rate = (
        (add_leg_successes / add_leg_attempts) * 100 if add_leg_attempts else 0.0
    )
    run_success_rate = (run_successes / run_attempts) * 100 if run_attempts else 0.0
    share_success_rate = (
        (share_successes / share_attempts) * 100 if share_attempts else 0.0
    )
    return {
        "range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "pageviews": pageviews,
        "sessions": len(sessions),
        "unique_visitors": len(unique_visitors),
        "add_leg_attempts": add_leg_attempts,
        "add_leg_successes": add_leg_successes,
        "add_leg_success_rate": add_leg_success_rate,
        "run_attempts": run_attempts,
        "run_successes": run_successes,
        "run_success_rate": run_success_rate,
        "run_non_success": max(0, run_result_events - run_successes),
        "share_attempts": share_attempts,
        "share_successes": share_successes,
        "share_success_rate": share_success_rate,
        "share_opens": share_opens,
    }


def build_timeseries_payload(db_path: Path, *, from_date: date, to_date: date) -> dict[str, Any]:
    events = _load_events(db_path, from_date=from_date, to_date=to_date)
    rows_by_day: dict[str, dict[str, Any]] = {}
    cursor = from_date
    while cursor <= to_date:
        rows_by_day[cursor.isoformat()] = {
            "date": cursor.isoformat(),
            "pageviews": 0,
            "sessions": set(),
            "unique_visitors": set(),
            "add_leg_attempts": 0,
            "run_attempts": 0,
            "share_attempts": 0,
            "share_opens": 0,
        }
        cursor += timedelta(days=1)

    for event in events:
        day = event["occurred_at_utc"][:10]
        bucket = rows_by_day.get(day)
        if not bucket:
            continue
        bucket["sessions"].add(event["session_id"])
        bucket["unique_visitors"].add(event["anonymous_id"])
        if event["event_name"] == "page_view":
            bucket["pageviews"] += 1
        elif event["event_name"] == "strategy_leg_add_attempt":
            bucket["add_leg_attempts"] += 1
        elif event["event_name"] == "strategy_run_attempt":
            bucket["run_attempts"] += 1
        elif event["event_name"] == "strategy_share_attempt":
            bucket["share_attempts"] += 1
        elif (
            event["event_name"] == "strategy_share_open"
            and event["outcome"] == "success"
        ):
            bucket["share_opens"] += 1

    rows: list[dict[str, Any]] = []
    for day in sorted(rows_by_day):
        bucket = rows_by_day[day]
        rows.append(
            {
                "date": day,
                "pageviews": bucket["pageviews"],
                "sessions": len(bucket["sessions"]),
                "unique_visitors": len(bucket["unique_visitors"]),
                "add_leg_attempts": bucket["add_leg_attempts"],
                "run_attempts": bucket["run_attempts"],
                "share_attempts": bucket["share_attempts"],
                "share_opens": bucket["share_opens"],
            }
        )

    return {"range": {"from": from_date.isoformat(), "to": to_date.isoformat()}, "rows": rows}


def _leg_label(leg: dict[str, Any]) -> str:
    side = str(leg.get("side") or "").upper()
    option_type = str(leg.get("option_type") or "").upper()
    target_delta = leg.get("target_delta")
    target_dte = leg.get("target_dte")
    quantity = leg.get("quantity")
    entry_time = str(leg.get("entry_time") or "")
    delta_label = "" if target_delta in (None, "") else f" Δ{target_delta}"
    dte_label = "" if target_dte in (None, "") else f" DTE {target_dte}"
    qty_label = "" if quantity in (None, "") else f" x{quantity}"
    entry_label = f" @ {entry_time}" if entry_time else ""
    return f"{side} {option_type}{delta_label}{dte_label}{qty_label}{entry_label}".strip()


def build_recent_runs_payload(
    db_path: Path, *, from_date: date, to_date: date, limit: int = 20
) -> dict[str, Any]:
    events = _load_events(
        db_path,
        from_date=from_date,
        to_date=to_date,
        event_names={"strategy_run_result"},
        limit=limit,
        descending=True,
    )
    rows: list[dict[str, Any]] = []
    for event in events:
        data = event["data"]
        legs = data.get("legs")
        legs_list = legs if isinstance(legs, list) else []
        leg_labels = [
            _leg_label(leg)
            for leg in legs_list
            if isinstance(leg, dict)
        ]
        hold_till_expiry = bool(data.get("hold_till_expiry"))
        exit_label = "Hold till expiry"
        if not hold_till_expiry:
            exit_days = data.get("exit_days")
            exit_time = data.get("exit_time")
            exit_label = f"Exit +{exit_days}d @ {exit_time}".strip()
        from_value = str(data.get("snapshot_from_date") or "")
        to_value = str(data.get("snapshot_to_date") or "")
        if from_value and to_value:
            range_label = f"{from_value} to {to_value}"
        else:
            range_label = from_value or to_value
        counts = []
        for label, key in (
            ("trade dates", "trade_dates_count"),
            ("trade plans", "trade_plan_count"),
            ("completed trades", "completed_trade_count"),
            ("streamers", "completed_contract_count"),
            ("skipped dates", "skipped_dates"),
        ):
            if key in data and data.get(key) not in (None, ""):
                counts.append(f"{label}: {data.get(key)}")
        rows.append(
            {
                "occurred_at_utc": event["occurred_at_utc"],
                "occurred_at_local": _format_local_timestamp(event["occurred_at_utc"]),
                "outcome": event["outcome"],
                "symbol": str(data.get("symbol") or ""),
                "leg_labels": leg_labels,
                "exit_label": exit_label,
                "range_label": range_label,
                "counts_label": " | ".join(counts),
            }
        )
    return {"range": {"from": from_date.isoformat(), "to": to_date.isoformat()}, "rows": rows}


def build_common_legs_payload(
    db_path: Path, *, from_date: date, to_date: date, limit: int = 10
) -> dict[str, Any]:
    events = _load_events(
        db_path,
        from_date=from_date,
        to_date=to_date,
        event_names={"strategy_run_attempt"},
    )
    counts: dict[str, int] = {}
    for event in events:
        legs = event["data"].get("legs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            label = _leg_label(leg)
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1

    rows = [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]
    return {"range": {"from": from_date.isoformat(), "to": to_date.isoformat()}, "rows": rows}
