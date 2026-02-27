# SPX Spot + Market Metrics Collector

This project collects SPX index spot data and market metrics from tastytrade and stores one snapshot row per run in SQL.

## What It Collects

Each run writes one row to `spx_market_snapshots` with:
- `snapshot_ts`
- `symbol` (`SPX` by default)
- Spot fields from market-data: `spot_price`, `bid_price`, `ask_price`, `last_price`, `mark_price`
- Selected market-metrics fields: `implied_volatility_index`, `implied_volatility_30_day`, `historical_volatility_30_day`

## Setup

1. Create and activate a virtualenv:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -e .
```

3. Configure `.env`:

Credentials:
- OAuth only: `TASTYTRADE_CLIENT_SECRET` + `TASTYTRADE_REFRESH_TOKEN`

Runtime settings:
- `DB_URL` (default `sqlite:///spx_options.db`)
- `UNDERLYING_SYMBOL` (default `SPX`)
- `COLLECTOR_LOG_LEVEL` (default `INFO`)
- `COLLECTOR_DEBUG_EVENTS` (default `false`)
- `COLLECTOR_DEBUG_SAMPLE_EVENTS` (default `3`)

## Run

One snapshot now:

```bash
spx-collector run-once
```

Continuous scheduler (weekdays, every 15 minutes from 6:00 AM to 2:00 PM Pacific):

```bash
spx-collector daemon
```

Spot diagnostics only:

```bash
spx-collector diagnose-spot
```

## Database

Main table:
- `spx_market_snapshots`

Inspect schema:

```sql
SELECT name
FROM sqlite_master
WHERE type = 'table'
ORDER BY name;
```

```sql
PRAGMA table_info('spx_market_snapshots');
```

```sql
PRAGMA index_list('spx_market_snapshots');
```

Latest rows:

```sql
SELECT *
FROM spx_market_snapshots
WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM spx_market_snapshots)
ORDER BY id;
```

## Debugging Flow

1. Confirm auth + spot:

```bash
spx-collector diagnose-spot
```

2. Run one full insert:

```bash
spx-collector run-once
```

3. Confirm DB write:

```sql
SELECT MAX(snapshot_ts), COUNT(*)
FROM spx_market_snapshots;
```

## Notes

- Logs include `snapshot_id` and stage markers so you can see exactly where failures happen.
- Collector appends rows; nothing is deleted automatically.
- Scheduler window is enforced as: Monday-Friday and `06:00 <= local Pacific time < 14:00`.
