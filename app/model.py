from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, Numeric, Date, ForeignKey
import datetime
import decimal
from typing import Optional


class Base(DeclarativeBase):
    pass


class TransactionDB(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    quantity: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    price: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    transaction_type: Mapped[str] = mapped_column(String)
    trade_date: Mapped[datetime.date] = mapped_column(Date)
    broker: Mapped[str] = mapped_column(String)
    fees: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(String)

class SecurityDB(Base):
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # `symbol` is canonical and is the FINNHUB symbol (e.g. "AAPL",
    # "BINANCE:BTCUSDT") because that's what the live stream subscribes to.
    symbol: Mapped[str] = mapped_column(String, unique=True)
    # Yahoo uses a different convention for anything that isn't a plain US
    # equity ("BTC-USD" vs "BINANCE:BTCUSDT"), and Yahoo is our only source
    # of historical bars. Null means the canonical symbol works as-is.
    history_symbol: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    asset_class: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)
    latest_price: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String, nullable=True)

class TaxLotDB(Base):
    __tablename__ = "tax_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    open_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    quantity_opened: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    quantity_remaining: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    broker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cost_basis_per_unit: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    opened_date: Mapped[datetime.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String)
    # Shares of this lot already claimed as wash-sale replacement shares, so
    # the same shares can't absorb two different disallowed losses.
    wash_replacement_used: Mapped[decimal.Decimal] = mapped_column(
        Numeric(20, 8), default=decimal.Decimal("0")
    )

class RealizedGainDB(Base):
    __tablename__ = "realized_gains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    sell_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    lot_id: Mapped[int] = mapped_column(ForeignKey("tax_lots.id"))
    quantity_sold: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    proceeds: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    cost_basis: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    # gain_loss is the TAX-REPORTABLE figure (post wash-sale disallowance).
    # The economic result is gain_loss - disallowed_wash_loss; the disallowed
    # portion lives on in the replacement lot's adjusted basis.
    gain_loss: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    disallowed_wash_loss: Mapped[decimal.Decimal] = mapped_column(
        Numeric(20, 8), default=decimal.Decimal("0")
    )
    term: Mapped[str] = mapped_column(String)
    sell_date: Mapped[datetime.date] = mapped_column(Date)


class TaxSettingsDB(Base):
    """Single-row table (id=1) of tax inputs the engine can't derive from
    transactions — currently the prior-year capital-loss carryforwards.

    Carryforwards are stored as POSITIVE loss amounts, split by character
    (short/long), because a carryforward re-enters the new year as a loss of
    the same character it had when it was generated.
    """
    __tablename__ = "tax_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    carryforward_st: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8), default=decimal.Decimal("0"))
    carryforward_lt: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8), default=decimal.Decimal("0"))