from datetime import date

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SPXMarketSnapshot(Base):
    __tablename__ = "spx_market_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_ts",
            "symbol",
            name="uq_spx_market_snapshots_snapshot_ts_symbol",
        ),
        Index("ix_spx_market_snapshots_symbol_ts", "symbol", "snapshot_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_ts: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)

    spot_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    market_data_updated_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metrics_updated_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    implied_volatility_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_volatility_30_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    historical_volatility_30_day: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )


class SPXOptionSnapshot(Base):
    __tablename__ = "spx_option_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_ts",
            "streamer_symbol",
            name="uq_spx_option_snapshots_snapshot_ts_streamer_symbol",
        ),
        Index("ix_spx_option_snapshots_symbol_ts", "symbol", "snapshot_ts"),
        Index(
            "ix_spx_option_snapshots_expiry_strike",
            "expiration_date",
            "strike_price",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_ts: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    streamer_symbol: Mapped[str] = mapped_column(String(128), index=True)

    expiration_date: Mapped[date] = mapped_column(Date, index=True)
    strike_price: Mapped[float] = mapped_column(Float, index=True)
    option_type: Mapped[str] = mapped_column(String(8), index=True)

    bid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    volatility: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)
