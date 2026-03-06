from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


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

        # Backfill old rows safely for SQLite text/datetime storage.
        conn.exec_driver_sql(
            """
            UPDATE spx_option_snapshots
            SET
                dte = MAX(0, CAST((julianday(date(expiration_date)) - julianday(date(snapshot_ts))) AS INTEGER)),
                time_in_day_est = substr(time(datetime(snapshot_ts, '-5 hours')), 1, 5)
            WHERE dte IS NULL OR time_in_day_est IS NULL
            """
        )
