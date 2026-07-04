from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Literal
from datetime import date
from decimal import Decimal

from app.database import get_db
from app.model import TransactionDB

app = FastAPI()


class Transaction(BaseModel):
    symbol: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)
    transaction_type: Literal["BUY", "SELL"]
    trade_date: date
    broker: str = Field(min_length=1)
    fees: Decimal = Field(ge=0)
    currency: str = Field(min_length=1)


@app.get("/")
def home():
    return {"message": "HarvestIQ running"}


@app.post("/transactions")
def create_transaction(
    transaction: Transaction,
    db: Session = Depends(get_db)
):
    db_transaction = TransactionDB(
        symbol=transaction.symbol,
        quantity=transaction.quantity,
        price=transaction.price,
        transaction_type=transaction.transaction_type,
        trade_date=transaction.trade_date,
        broker=transaction.broker,
        fees=transaction.fees,
        currency=transaction.currency
    )

    db.add(db_transaction)
    db.commit()

    return {"message": "Transaction saved"}


@app.get("/transactions")
def get_transactions(
    db: Session = Depends(get_db)
):
    transactions = db.query(TransactionDB).all()
    return transactions


@app.get("/portfolio")
def get_portfolio(
    db: Session = Depends(get_db)
):
    transactions = db.query(TransactionDB).all()

    holdings = {}

    for transaction in transactions:

        if transaction.symbol not in holdings:
            holdings[transaction.symbol] = 0

        if transaction.transaction_type == "BUY":
            holdings[transaction.symbol] += transaction.quantity
        else:
            holdings[transaction.symbol] -= transaction.quantity

    return holdings


@app.get("/tax-lots")
def get_tax_lots(
    db: Session = Depends(get_db)
):
    return {"message": "not implemented yet"}