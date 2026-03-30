# Add Leg Performance Findings

Date of investigation: March 20, 2026 and March 21, 2026

## Summary

The slow `Add leg` behavior is primarily server-side on `/api/options/resolve-leg`, and it gets much worse as the selected snapshot range grows.

The strongest confirmed causes were:

- date-range explosion: wider snapshot ranges return far more candidate contracts
- payload size: the endpoint was returning the full `contracts` array even when the UI only used the top match
- environment sensitivity: the same route was much faster locally than on the public site, so prod infrastructure and data volume amplify the issue

## Measured Requests

Public site measurement:

- `snapshot-dates`:
  - `0.34s`, `193 B`
- `resolve-leg`, one day, `DTE=7`, `09:30 ET`, March 20, 2026 only:
  - about `0.38s`, `80.6 KB`
- `resolve-leg`, full live range, March 4, 2026 through March 20, 2026:
  - about `3.97s`, `1.05 MB`

Local prod app against the local SQLite file:

- `resolve-leg`, one day, March 6, 2026 only:
  - about `0.03s`, `80.8 KB`
- `resolve-leg`, local full range, March 4, 2026 through March 6, 2026:
  - about `0.09s`, `242.7 KB`
- `resolve-leg`, same local full range with `best_only=1`:
  - about `0.03s`, `581 B`

## What Was Confirmed

1. The slowness is not mainly caused by the frontend table rerender on `Add leg`.
   The live endpoint itself spends most of the time before first byte.

2. Wider date ranges materially increase latency.
   The live route jumped from about `0.38s` to about `3.97s` when the snapshot range expanded from one day to the full available range.

3. Returning the full contract list is unnecessary for the current `Add leg` and per-date strategy-analysis callers.
   Those callers only consume the best match.

## What Was Not Confirmed

- A browser-only scripting bottleneck was not confirmed.
- A SQLite schema/index migration was not applied in this pass because the first safe win was reducing unnecessary response work without changing the default API contract.

## Implemented Quick Win

The backtest apps now support an optional `best_only=1` mode on `/api/options/resolve-leg`.

Behavior:

- default endpoint behavior stays unchanged for existing callers
- `Add leg` now requests only the best match
- per-date strategy-analysis leg resolution also requests only the best match
- the server no longer returns the full `contracts` array for those optimized callers
- `option_type` filtering now uses the normalized stored value directly instead of `UPPER(option_type)`

## Next Recommendation

After this repo change is deployed to prod, re-measure the live `Add leg` flow first.

If prod is still materially above the `< 1s` target for typical requests, the next fix should be a DB-focused pass:

- benchmark the live SQLite query plan with the current production DB
- consider a composite index aligned to `symbol`, `option_type`, `dte`, and `snapshot_ts`
- only then decide whether a query rewrite or additional indexing is needed
