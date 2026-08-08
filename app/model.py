from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, Numeric, Date, ForeignKey, Boolean
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
    # DRIP guard inputs. HarvestIQ can't see broker settings, so the user
    # declares whether automatic dividend reinvestment is on for this holding
    # and when the next (or most recent) dividend lands. A DRIP purchase is a
    # BUY the wash-sale rule counts — the classic "I sold everything, why is
    # the IRS still saying I broke the rule" trap.
    drip_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_dividend_date: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)

class TaxLotDB(Base):
    __tablename__ = "tax_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    open_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    quantity_opened: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    quantity_remaining: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    broker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cost_basis_per_unit: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    # opened_date is ALWAYS the actual purchase date (FIFO ordering and the
    # wash-sale window both need the real acquisition date).
    opened_date: Mapped[datetime.date] = mapped_column(Date)
    # holding_period_date is the date used for short/long-term classification.
    # Null means "same as opened_date". It diverges only when a wash sale
    # tacks the sold shares' holding period onto this lot's shares.
    holding_period_date: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)
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
    # Where a disallowed loss went: internal replacement lots carry it in
    # their adjusted basis, but a loss washed by an EXTERNAL account purchase
    # either belongs to that account's books (taxable) or is permanently
    # destroyed (retirement). This note says which, in plain English.
    wash_sale_note: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    term: Mapped[str] = mapped_column(String)
    sell_date: Mapped[datetime.date] = mapped_column(Date)


# Account types where a wash-sale replacement purchase PERMANENTLY destroys
# the loss (Rev. Rul. 2008-5): there is no basis to adjust in a tax-advantaged
# account, so the disallowed loss can never come back.
RETIREMENT_ACCOUNT_TYPES = {"traditional_ira", "roth_ira", "401k", "hsa"}


class ExternalAccountDB(Base):
    """A watch-only account at another broker (or a retirement account).

    HarvestIQ never suggests selling from these — they exist so purchases
    made in them can be counted against the wash-sale rule, which applies
    to the TAXPAYER across all accounts while each broker only sees its own.
    """
    __tablename__ = "external_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)          # "Fidelity Roth IRA"
    broker: Mapped[str] = mapped_column(String)
    account_type: Mapped[str] = mapped_column(String)  # taxable / traditional_ira / roth_ira / 401k / hsa / other

    @property
    def is_retirement(self) -> bool:
        return self.account_type in RETIREMENT_ACCOUNT_TYPES


class ExternalActivityDB(Base):
    """A purchase observed in a watch-only account (manual entry for now).

    Only BUYs matter to the wash-sale rule. `replacement_used` tracks how
    many of these shares have already absorbed a disallowed loss, so the
    same external purchase can't wash two different sales.
    """
    __tablename__ = "external_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("external_accounts.id"))
    symbol: Mapped[str] = mapped_column(String)
    quantity: Mapped[decimal.Decimal] = mapped_column(Numeric(20, 8))
    trade_date: Mapped[datetime.date] = mapped_column(Date)
    replacement_used: Mapped[decimal.Decimal] = mapped_column(
        Numeric(20, 8), default=decimal.Decimal("0")
    )


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
    # The LT capital-gains rate the user expects to face when they eventually
    # sell the replacement position. Harvesting lowers cost basis, so the
    # "saved" tax largely comes back due at THIS rate — if it's higher than
    # today's, harvesting can lose money. Null = assume today's LT rate.
    expected_future_lt_rate: Mapped[Optional[decimal.Decimal]] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    # How much a dollar of NEW carryforward is worth to this user, as a
    # fraction of its face value at the future rate (0..1). This is the
    # explicit strategic dial the plan uses instead of silently assuming:
    #   0 = no gains on the horizon — don't spend scarce unrealized loss
    #       past the current-year cap (plan sells partial lots to land on it)
    #   1 = a large gain is coming (property sale, RSU cliff, exit) — bank
    #       every loss now; carryforward is near face value
    carryforward_discount: Mapped[decimal.Decimal] = mapped_column(
        Numeric(6, 4), default=decimal.Decimal("0")
    )