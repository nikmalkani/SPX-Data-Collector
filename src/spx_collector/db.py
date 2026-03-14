from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


_EASTERN_TZ = ZoneInfo("America/New_York")


def _parse_sqlite_snapshot_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        elif "T" not in raw and "+" not in raw and raw.count("-") == 2:
            raw = raw.replace(" ", "T") + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_engine(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, future=True, connect_args=connect_args)


def build_session_factory(db_url: str) -> sessionmaker[Session]:
    engine = build_engine(db_url)
    Base.metadata.create_all(engine)
    _ensure_sqlite_option_columns(engine, db_url)
    return sessionmaker(engine, expire_on_commit=False, future=True)


def _ensure_sqlite_option_columns(engine, db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        return

    with engine.begin() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info('spx_option_snapshots')").fetchall()
        existing = {str(row[1]) for row in columns}

        if "dte" not in existing:
            conn.exec_driver_sql("ALTER TABLE spx_option_snapshots ADD COLUMN dte INTEGER")
        if "time_in_day_est" not in existing and "time_in_day" in existing:
            conn.exec_driver_sql(
                "ALTER TABLE spx_option_snapshots RENAME COLUMN time_in_day TO time_in_day_est"
            )
        elif "time_in_day_est" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE spx_option_snapshots ADD COLUMN time_in_day_est VARCHAR(5)"
            )

        missing_rows = conn.exec_driver_sql(
            """
            SELECT id, snapshot_ts, expiration_date
            FROM spx_option_snapshots
            WHERE dte IS NULL OR time_in_day_est IS NULL
            """
        ).fetchall()

        updates: list[dict[str, object]] = []
        for row in missing_rows:
            snapshot_dt = _parse_sqlite_snapshot_ts(row[1])
            expiration = row[2]
            if snapshot_dt is None:
                continue
            if isinstance(expiration, date):
                expiration_date = expiration
            else:
                try:
                    expiration_date = date.fromisoformat(str(expiration))
                except ValueError:
                    continue
            snapshot_et = snapshot_dt.astimezone(_EASTERN_TZ)
            updates.append(
                {
                    "id": row[0],
                    "dte": max(0, (expiration_date - snapshot_et.date()).days),
                    "time_in_day_est": snapshot_et.strftime("%H:%M"),
                }
            )

        if updates:
            conn.exec_driver_sql(
                """
                UPDATE spx_option_snapshots
                SET dte = :dte,
                    time_in_day_est = :time_in_day_est
                WHERE id = :id
                """,
                updates,
            )
