# SPX Data Collector

Collects index data from tastytrade and stores snapshots in SQLite/Postgres.

Current behavior per run:
- Inserts market snapshot rows for `SPX` and `VIX` into `spx_market_snapshots`.
- Inserts SPX option contract snapshot rows into `spx_option_snapshots`.

## Data Model

`spx_market_snapshots` columns:
- `snapshot_ts`, `symbol`
- `spot_price`, `bid_price`, `ask_price`, `last_price`
- `market_data_updated_at`, `metrics_updated_at`
- `implied_volatility_index`, `implied_volatility_30_day`, `historical_volatility_30_day`

`spx_option_snapshots` columns:
- `snapshot_ts`, `symbol`, `streamer_symbol`
- `expiration_date`, `strike_price`, `option_type`
- `bid_price`, `ask_price`, `mid_price`
- `volatility`, `delta`, `gamma`, `theta`, `vega`

## Scheduler Window

Daemon runs only:
- Monday-Friday
- Every 15 minutes
- `06:00 <= Pacific time < 14:00`

`run-once` uses the same window check.  
If you need an off-hours forced test, call collector directly (see "Forced Snapshot Test").

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Create `.env` with at least:
- `TASTYTRADE_CLIENT_SECRET`
- `TASTYTRADE_REFRESH_TOKEN`

Common runtime settings:
- `DB_URL` (default `sqlite:///spx_options.db`)
- `UNDERLYING_SYMBOL` (default `SPX`)
- `OPTION_EXPIRIES_PER_RUN` (default `2`)
- `OPTION_STRIKES_PER_SIDE` (default `25`)
- `OPTIONS_STREAM_TIMEOUT_SECONDS` (default `20`)
- `COLLECTOR_LOG_LEVEL` (default `INFO`)

## Run Commands

One scheduled-window run:

```bash
spx-collector run-once
```

Daemon:

```bash
spx-collector daemon
```

Spot-only auth/diagnostics:

```bash
spx-collector diagnose-spot
```

## Forced Snapshot Test (Off-Hours)

Bypasses scheduler time window and executes full collector logic once:

```bash
python -c "import asyncio; from spx_collector.config import Settings; from spx_collector.db import build_session_factory; from spx_collector.collector import SPXCollector; s=Settings(); sf=build_session_factory(s.db_url); db=sf(); n=asyncio.run(SPXCollector(s).run_snapshot(db)); db.close(); print('FORCED_INSERTED=', n)"
```

## SQL Checks

List tables:

```sql
SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
```

Row counts:

```sql
SELECT COUNT(*) FROM spx_market_snapshots;
SELECT COUNT(*) FROM spx_option_snapshots;
```

Latest market rows:

```sql
SELECT snapshot_ts, symbol, spot_price
FROM spx_market_snapshots
ORDER BY snapshot_ts DESC
LIMIT 10;
```

Latest option rows:

```sql
SELECT snapshot_ts, expiration_date, strike_price, option_type, bid_price, ask_price, delta
FROM spx_option_snapshots
ORDER BY snapshot_ts DESC, expiration_date, strike_price, option_type
LIMIT 50;
```

Export options to CSV:

```bash
sqlite3 -header -csv spx_options.db "SELECT * FROM spx_option_snapshots ORDER BY snapshot_ts DESC, expiration_date, strike_price, option_type;" > spx_option_snapshots.csv
```

## Deploy Update (Lightsail)

```bash
cd ~/SPX-Data-Collector
git checkout main
git pull origin main
source .venv/bin/activate
pip install -e .
sudo systemctl restart spx-collector
journalctl -u spx-collector -n 80 --no-pager
```

## Notes

- OAuth auth path only (no username/password login path).
- Logs include `snapshot_id` and stage names for debugging.
- Collector appends rows; no automatic cleanup/delete.
