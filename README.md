# SPX Data Collector

This repo has three working parts:

- the collector that pulls SPX data from tastytrade and stores snapshots
- local backtest/playground HTTP apps for dev and staging work
- the prod backtest app that can be served publicly behind a reverse proxy

Current collector behavior per run:
- Inserts one market snapshot row for `SPX` into `spx_market_snapshots`.
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
- `dte` (days to expiration at snapshot date)
- `time_in_day_est` (`HH:MM` in US Eastern Time derived from snapshot timestamp)
- `bid_price`, `ask_price`, `mid_price`
- `volatility`, `delta`, `gamma`, `theta`, `vega`

Notes:
- `dte` is computed as `max(0, expiration_date - date(snapshot_ts))`.
- `time_in_day_est` is stored in Eastern time for easier intraday grouping.
- SQLite startup migration auto-adds/backfills these columns for existing DBs.

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

Create `.env` from `.env.example` with at least:
- `TASTYTRADE_CLIENT_SECRET`
- `TASTYTRADE_REFRESH_TOKEN`

Common runtime settings:
- `DB_URL` (default `sqlite:///spx_options.db`)
- `UNDERLYING_SYMBOL` (default `SPX`)
- `OPTION_EXPIRIES_PER_RUN` (default `2`)
- `OPTION_STRIKES_COUNT` (default `140`)
- `OPTIONS_STREAM_TIMEOUT_SECONDS` (default `20`)
- `COLLECTOR_LOG_LEVEL` (default `INFO`)

## App Roles

- `src/spx_collector/backtest_dev.py`: local dev UI, default port `8787`
- `src/spx_collector/backtest_staging.py`: local staging UI, default port `8788`
- `src/spx_collector/backtest_prod.py`: prod UI, default port `8789`

All three backtest apps read through Python HTTP handlers. The browser does not connect to SQLite directly.

## Website Deployment Shape

The public website runs with this request path:

`Browser -> Caddy -> backtest_prod.py on 127.0.0.1:8789 -> SQLite`

Current prod hosting layout in this repo:

- Caddy can reverse proxy a public hostname to the prod app
- `deploy/systemd/spx-backtest-prod.service` is a sanitized example that runs the public UI on loopback
- the prod UI and collector can share the same local SQLite file on the host instance

Contributor-level deployment notes live here:

- `docs/lightsail_prod_setup.md`
- `docs/architecture.md`

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

Local backtest apps:

```bash
PYTHONPATH=src python -m spx_collector.backtest_dev
PYTHONPATH=src python -m spx_collector.backtest_staging
PYTHONPATH=src python -m spx_collector.backtest_prod
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

## Deploy Update (Example Host)

Treat the local repo as the source of truth and your server as a deploy target. Normal flow is:

1. Make and test changes locally.
2. Merge reviewed changes into `main`.
3. On the host, fast-forward `main` and restart services.

Typical update commands:

```bash
cd /path/to/SPX-Data-Collector
git checkout main
git pull --ff-only origin main
source .venv/bin/activate
pip install -e .
sudo systemctl restart spx-collector
journalctl -u spx-backtest-prod -n 80 --no-pager
journalctl -u spx-collector -n 80 --no-pager
```

For a sanitized deployment template, use `docs/lightsail_prod_setup.md`.

## Notes

- OAuth auth path only (no username/password login path).
- Logs include `snapshot_id` and stage names for debugging.
- Collector appends rows; no automatic cleanup/delete.
