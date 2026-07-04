from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Literal
from datetime import date, timedelta
from decimal import Decimal
from groq import Groq
import yfinance as yf

from app.database import get_db
from app.model import TransactionDB, SecurityDB, TaxLotDB, RealizedGainDB

from dotenv import load_dotenv
load_dotenv()

app = FastAPI()

SHORT_TERM_TAX_RATE = Decimal("0.40")  # default: ~35% federal + ~5% GA
LONG_TERM_TAX_RATE = Decimal("0.20")   # default: ~15% federal + ~5% GA


class Transaction(BaseModel):
    symbol: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)
    transaction_type: Literal["BUY", "SELL"]
    trade_date: date
    broker: str = Field(min_length=1)
    fees: Decimal = Field(ge=0)
    currency: str = Field(min_length=1)


class MarketTransaction(BaseModel):
    symbol: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    transaction_type: Literal["BUY", "SELL"]
    broker: str = Field(min_length=1)
    fees: Decimal = Field(ge=0)
    currency: str = Field(min_length=1)


@app.get("/")
def home():
    return {"message": "HarvestIQ running"}


def process_transaction(transaction: Transaction, db: Session):
    security = (
        db.query(SecurityDB)
        .filter(SecurityDB.symbol == transaction.symbol)
        .first()
    )
    if security is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown symbol: {transaction.symbol}",
        )

    # For a SELL, gather open lots oldest-first and confirm we own enough
    # BEFORE saving anything.
    open_lots = []
    if transaction.transaction_type == "SELL":
        open_lots = (
            db.query(TaxLotDB)
            .filter(TaxLotDB.symbol == transaction.symbol)
            .filter(TaxLotDB.status == "open")
            .order_by(TaxLotDB.opened_date)
            .all()
        )
        total_available = sum(lot.quantity_remaining for lot in open_lots)
        if total_available < transaction.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough {transaction.symbol} to sell: have {total_available}, tried {transaction.quantity}",
            )

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
    db.refresh(db_transaction)

    if transaction.transaction_type == "BUY":
        total_cost = transaction.price * transaction.quantity + transaction.fees
        cost_basis_per_unit = total_cost / transaction.quantity

        lot = TaxLotDB(
            symbol=transaction.symbol,
            open_transaction_id=db_transaction.id,
            quantity_opened=transaction.quantity,
            quantity_remaining=transaction.quantity,
            cost_basis_per_unit=cost_basis_per_unit,
            opened_date=transaction.trade_date,
            status="open",
        )
        db.add(lot)
        db.commit()

    if transaction.transaction_type == "SELL":
        quantity_to_sell = transaction.quantity

        for lot in open_lots:
            if quantity_to_sell <= 0:
                break

            if lot.quantity_remaining <= quantity_to_sell:
                # sell this entire lot
                quantity_sold = lot.quantity_remaining
                lot.quantity_remaining = 0
                lot.status = "closed"
            else:
                # sell part of this lot
                quantity_sold = quantity_to_sell
                lot.quantity_remaining -= quantity_to_sell

            quantity_to_sell -= quantity_sold

            proceeds = quantity_sold * transaction.price
            cost_basis = quantity_sold * lot.cost_basis_per_unit
            gain_loss = proceeds - cost_basis

            holding_days = (transaction.trade_date - lot.opened_date).days
            term = "long" if holding_days > 365 else "short"

            realized = RealizedGainDB(
                symbol=transaction.symbol,
                sell_transaction_id=db_transaction.id,
                lot_id=lot.id,
                quantity_sold=quantity_sold,
                proceeds=proceeds,
                cost_basis=cost_basis,
                gain_loss=gain_loss,
                term=term,
                sell_date=transaction.trade_date,
            )
            db.add(realized)

        db.commit()

    return {"message": "Transaction saved"}


@app.post("/transactions")
def create_transaction(transaction: Transaction, db: Session = Depends(get_db)):
    return process_transaction(transaction, db)


@app.post("/transactions/market")
def create_market_transaction(market: MarketTransaction, db: Session = Depends(get_db)):
    ticker = yf.Ticker(market.symbol)
    data = ticker.history(period="1d")

    if data.empty:
        raise HTTPException(
            status_code=400,
            detail=f"Could not fetch a market price for {market.symbol}",
        )

    current_price = Decimal(str(round(float(data["Close"].iloc[-1]), 2)))

    transaction = Transaction(
        symbol=market.symbol,
        quantity=market.quantity,
        price=current_price,
        transaction_type=market.transaction_type,
        trade_date=date.today(),
        broker=market.broker,
        fees=market.fees,
        currency=market.currency,
    )

    return process_transaction(transaction, db)


@app.get("/transactions")
def get_transactions(db: Session = Depends(get_db)):
    transactions = db.query(TransactionDB).all()
    return transactions


@app.get("/portfolio")
def get_portfolio(db: Session = Depends(get_db)):
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
def get_tax_lots(db: Session = Depends(get_db)):
    return db.query(TaxLotDB).all()


@app.get("/realized-gains")
def get_realized_gains(db: Session = Depends(get_db)):
    return db.query(RealizedGainDB).all()


class Security(BaseModel):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    asset_class: Literal["equity", "crypto"]
    currency: str = Field(min_length=1)


@app.post("/securities")
def create_security(security: Security, db: Session = Depends(get_db)):
    db_security = SecurityDB(
        symbol=security.symbol,
        name=security.name,
        asset_class=security.asset_class,
        currency=security.currency,
    )
    db.add(db_security)
    db.commit()
    return {"message": "Security saved"}


@app.get("/securities")
def get_securities(db: Session = Depends(get_db)):
    return db.query(SecurityDB).all()


class PriceUpdate(BaseModel):
    latest_price: Decimal = Field(ge=0)


@app.post("/securities/{symbol}/price")
def update_price(symbol: str, price: PriceUpdate, db: Session = Depends(get_db)):
    security = (
        db.query(SecurityDB)
        .filter(SecurityDB.symbol == symbol)
        .first()
    )
    if security is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")

    security.latest_price = price.latest_price
    db.commit()
    return {"message": "Price updated"}


@app.get("/unrealized-gains")
def get_unrealized_gains(db: Session = Depends(get_db)):
    open_lots = (
        db.query(TaxLotDB)
        .filter(TaxLotDB.status == "open")
        .all()
    )

    results = []
    for lot in open_lots:
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == lot.symbol)
            .first()
        )
        if security is None or security.latest_price is None:
            continue  # can't value a lot with no current price

        market_value = lot.quantity_remaining * security.latest_price
        cost_basis = lot.quantity_remaining * lot.cost_basis_per_unit
        unrealized = market_value - cost_basis

        results.append({
            "lot_id": lot.id,
            "symbol": lot.symbol,
            "quantity_remaining": lot.quantity_remaining,
            "cost_basis": cost_basis,
            "market_value": market_value,
            "unrealized_gain_loss": unrealized,
        })

    return results


@app.get("/wash-sales")
def get_wash_sales(db: Session = Depends(get_db)):
    losses = (
        db.query(RealizedGainDB)
        .filter(RealizedGainDB.gain_loss < 0)
        .all()
    )

    results = []
    for loss in losses:
        window_start = loss.sell_date - timedelta(days=30)
        window_end = loss.sell_date + timedelta(days=30)

        replacement = (
            db.query(TransactionDB)
            .filter(TransactionDB.symbol == loss.symbol)
            .filter(TransactionDB.transaction_type == "BUY")
            .filter(TransactionDB.trade_date >= window_start)
            .filter(TransactionDB.trade_date <= window_end)
            .first()
        )

        results.append({
            "realized_gain_id": loss.id,
            "symbol": loss.symbol,
            "sell_date": loss.sell_date,
            "loss_amount": loss.gain_loss,
            "wash_sale": replacement is not None,
            "triggering_buy_id": replacement.id if replacement else None,
        })

    return results


def build_harvest_opportunities(db, short_term_rate, long_term_rate):
    open_lots = (
        db.query(TaxLotDB)
        .filter(TaxLotDB.status == "open")
        .all()
    )

    today = date.today()
    opportunities = []

    for lot in open_lots:
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == lot.symbol)
            .first()
        )
        if security is None or security.latest_price is None:
            continue

        market_value = lot.quantity_remaining * security.latest_price
        cost_basis = lot.quantity_remaining * lot.cost_basis_per_unit
        unrealized = market_value - cost_basis

        if unrealized >= 0:
            continue

        holding_days = (today - lot.opened_date).days
        if holding_days > 365:
            term = "long"
            tax_rate = long_term_rate
        else:
            term = "short"
            tax_rate = short_term_rate

        estimated_tax_savings = -unrealized * tax_rate

        window_start = today - timedelta(days=30)
        recent_buy = (
            db.query(TransactionDB)
            .filter(TransactionDB.symbol == lot.symbol)
            .filter(TransactionDB.transaction_type == "BUY")
            .filter(TransactionDB.trade_date >= window_start)
            .first()
        )
        wash_sale_risk = recent_buy is not None

        opportunities.append({
            "lot_id": lot.id,
            "symbol": lot.symbol,
            "quantity_remaining": lot.quantity_remaining,
            "cost_basis": cost_basis,
            "market_value": market_value,
            "unrealized_loss": unrealized,
            "term": term,
            "estimated_tax_savings": estimated_tax_savings,
            "wash_sale_risk": wash_sale_risk,
            "recommended": not wash_sale_risk,
        })

    opportunities.sort(key=lambda o: o["estimated_tax_savings"], reverse=True)
    return opportunities


@app.get("/harvest-opportunities")
def get_harvest_opportunities(
    short_term_rate: Decimal = SHORT_TERM_TAX_RATE,
    long_term_rate: Decimal = LONG_TERM_TAX_RATE,
    db: Session = Depends(get_db),
):
    return build_harvest_opportunities(db, short_term_rate, long_term_rate)


@app.get("/harvest-explanation")
def get_harvest_explanation(
    short_term_rate: Decimal = SHORT_TERM_TAX_RATE,
    long_term_rate: Decimal = LONG_TERM_TAX_RATE,
    db: Session = Depends(get_db),
):
    opportunities = build_harvest_opportunities(db, short_term_rate, long_term_rate)

    if not opportunities:
        return {"explanation": "No harvesting opportunities right now — none of your open lots are at a loss."}

    lines = []
    for o in opportunities:
        lines.append(
            f"- {o['symbol']}: {o['quantity_remaining']} units, "
            f"unrealized loss {o['unrealized_loss']}, {o['term']}-term, "
            f"estimated tax savings {o['estimated_tax_savings']}, "
            f"wash-sale risk: {o['wash_sale_risk']}"
        )
    summary = "\n".join(lines)

    prompt = (
        "You are explaining tax-loss harvesting to a beginner investor. "
        "Here are the current harvest opportunities in their portfolio:\n\n"
        f"{summary}\n\n"
        "In plain English, explain which positions they might consider selling and why, "
        "referencing the estimated tax savings and any wash-sale risks. Be concise. "
        "End with a one-line reminder that this is not personalized tax advice."
    )

    client = Groq()
    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    return {"explanation": response.choices[0].message.content}


@app.post("/refresh-prices")
def refresh_prices(db: Session = Depends(get_db)):
    securities = db.query(SecurityDB).all()

    updated = []
    for security in securities:
        ticker = yf.Ticker(security.symbol)
        data = ticker.history(period="1d")

        if data.empty:
            continue  # no data returned for this symbol, skip it

        latest_close = float(data["Close"].iloc[-1])
        security.latest_price = Decimal(str(round(latest_close, 2)))
        updated.append({
            "symbol": security.symbol,
            "latest_price": security.latest_price,
        })

    db.commit()
    return {"updated": updated}