# Time Zone Notes

## General rule
- Do not hardcode time zone offsets in US projects, or really in any project that uses real civil time.
- Fixed offsets like `-5 hours`, `-8 hours`, or assumptions like "Eastern is always UTC-5" will break across daylight saving transitions and can also fail for historical rule changes.

## Preferred approach
- Store canonical timestamps in UTC.
- Convert to local time zones only at the edges:
  - when interpreting user-entered local times
  - when rendering labels, reports, and charts
  - when deriving local calendar concepts such as business day, session day, cutoff time, or days-to-expiry
- Use named IANA zones like `America/New_York`, `America/Los_Angeles`, etc., not raw offsets.
- In Python, prefer `zoneinfo.ZoneInfo`.
- In browser JS, prefer `Intl.DateTimeFormat(..., { timeZone: "Region/City" })`.

## Calendar-day and business-rule caution
- If logic depends on a local calendar day, derive the date in the target time zone first and then compute business rules from that local date.
- Do not assume UTC date boundaries match local business boundaries.

## Graphs and aligned time series
- Aligning data on elapsed real time is usually safer than aligning by manually offset local clock values.
- Local-time labels on charts should come from timezone-aware formatting, not arithmetic on timestamps.
- If a chart spans a DST transition, skipped or repeated local clock times may be correct and should not be "smoothed away" by custom offset logic.

## Ambiguous and missing local times
- Naive local datetimes can be ambiguous during the fall DST rollback and can be nonexistent during the spring-forward jump.
- Prefer UTC or explicit offsets in APIs whenever possible.
- If a system must accept local wall-clock input, treat the zone as part of the input and handle DST edge cases deliberately.

## Concrete repo example
- This repo had a bug where SQLite backfill used `datetime(snapshot_ts, '-5 hours')` to derive Eastern time.
- That was wrong during daylight saving time; the fix was to convert with `America/New_York` timezone rules in Python instead of using a hardcoded offset.
