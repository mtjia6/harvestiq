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
        price=transaction.price
    )

    db.add(db_transaction)
    db.commit()

    return {"message": "Transaction saved"}
