from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint
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
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)

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
