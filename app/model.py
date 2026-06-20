from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, Float

class Base(DeclarativeBase):
    pass


class TransactionDB(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    symbol: Mapped[str] = mapped_column(String)

    quantity: Mapped[int] = mapped_column(Integer)

    price: Mapped[float] = mapped_column(Float)