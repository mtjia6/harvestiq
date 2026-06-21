from fastapi import Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.model import TransactionDB
from fastapi import FastAPI
from pydantic import BaseModel
from app.database import engine

app = FastAPI()

class Transaction(BaseModel):
    symbol: str
    quantity: int
    price: float
    transaction_type: str

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
        transaction_type=transaction.transaction_type
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