from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, Numeric, Date
import datetime
import decimal


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
    symbol: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    asset_class: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)