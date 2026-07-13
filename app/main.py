from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Literal
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
from groq import Groq
import json
import yfinance as yf
from fastapi.responses import FileResponse

from app.database import get_db
from app.model import TransactionDB, SecurityDB, TaxLotDB, RealizedGainDB
from app.celery_app import celery_app, refresh_prices, redis_client, generate_harvest_explanation, run_tax_analysis
from app.harvest import build_harvest_opportunities

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


@app.get("/dashboard")
def dashboard():
    return FileResponse("static/index.html")


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
    db.flush()
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

    if transaction.transaction_type == "SELL":
        quantity_to_sell = transaction.quantity

        for lot in open_lots:
            if quantity_to_sell <= 0:
                break

            if lot.quantity_remaining <= quantity_to_sell:
                quantity_sold = lot.quantity_remaining
                lot.quantity_remaining = 0
                lot.status = "closed"
            else:
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
    open_lots = db.query(TaxLotDB).filter(TaxLotDB.status == "open").all()

    holdings = {}
    for lot in open_lots:
        h = holdings.setdefault(lot.symbol, {
            "symbol": lot.symbol,
            "quantity": Decimal("0"),
            "cost_basis": Decimal("0"),
        })
        h["quantity"] += lot.quantity_remaining
        h["cost_basis"] += lot.quantity_remaining * lot.cost_basis_per_unit

    result = []
    for symbol, h in holdings.items():
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == symbol)
            .first()
        )
        if security and security.latest_price is not None:
            h["market_value"] = h["quantity"] * security.latest_price
        else:
            h["market_value"] = h["cost_basis"]
        h["unrealized_gain_loss"] = h["market_value"] - h["cost_basis"]
        result.append(h)

    return result


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
    sector: str = Field(min_length=1)


@app.post("/securities")
def create_security(security: Security, db: Session = Depends(get_db)):
    db_security = SecurityDB(
        symbol=security.symbol,
        name=security.name,
        asset_class=security.asset_class,
        currency=security.currency,
        sector=security.sector,
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
            continue

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
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == loss.symbol)
            .first()
        )
        is_crypto = security is not None and security.asset_class == "crypto"

        replacement = None
        if not is_crypto:
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


@app.get("/harvest-opportunities")
def get_harvest_opportunities(
    short_term_rate: Decimal = SHORT_TERM_TAX_RATE,
    long_term_rate: Decimal = LONG_TERM_TAX_RATE,
    db: Session = Depends(get_db),
):
    return build_harvest_opportunities(db, short_term_rate, long_term_rate)


@app.get("/harvest-explanation")
def get_harvest_explanation():
    job = generate_harvest_explanation.delay()
    return {"job_id": job.id}


@app.post("/tax-analysis/run")
def start_tax_analysis(
    short_term_rate: Decimal = SHORT_TERM_TAX_RATE,
    long_term_rate: Decimal = LONG_TERM_TAX_RATE,
):
    # Trigger a fresh recompute in the background; result gets cached in Redis.
    job = run_tax_analysis.delay(str(short_term_rate), str(long_term_rate))
    return {"job_id": job.id}


@app.get("/tax-analysis")
def get_tax_analysis():
    # Serve the cached analysis instantly (no recompute).
    raw = redis_client.get("tax_analysis")
    if raw is None:
        return {"status": "not_computed", "message": "Run POST /tax-analysis/run first."}
    return json.loads(raw)


@app.post("/refresh-prices")
def start_refreshing_prices():
    job = refresh_prices.delay()
    return {"job_id": job.id}


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    # Check on a background job by its id.
    result = celery_app.AsyncResult(job_id)
    return {
        "job_id": job_id,
        "status": result.status,  # PENDING, STARTED, SUCCESS, FAILURE
        "result": result.result if result.successful() else None,
    }


@app.get("/price-history/{symbol}")
def get_price_history(symbol: str, days: int = 90):
    ticker = yf.Ticker(symbol)
    end = datetime.now()
    start = end - timedelta(days=days)
    data = ticker.history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    if data.empty:
        raise HTTPException(status_code=404, detail=f"No price history for {symbol}")

    history = [
        {"date": idx.strftime("%Y-%m-%d"), "close": round(float(row["Close"]), 2)}
        for idx, row in data.iterrows()
    ]
    return {"symbol": symbol, "history": history}


@app.get("/portfolio-summary")
def get_portfolio_summary(
    short_term_rate: Decimal = SHORT_TERM_TAX_RATE,
    long_term_rate: Decimal = LONG_TERM_TAX_RATE,
    db: Session = Depends(get_db),
):
    open_lots = db.query(TaxLotDB).filter(TaxLotDB.status == "open").all()

    total_market_value = Decimal("0")
    total_cost_basis = Decimal("0")
    for lot in open_lots:
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == lot.symbol)
            .first()
        )
        cost = lot.quantity_remaining * lot.cost_basis_per_unit
        total_cost_basis += cost
        if security and security.latest_price is not None:
            total_market_value += lot.quantity_remaining * security.latest_price
        else:
            total_market_value += cost  # no price yet: value at cost

    total_unrealized = total_market_value - total_cost_basis

    today = date.today()
    year_start = date(today.year, 1, 1)
    realized = (
        db.query(RealizedGainDB)
        .filter(RealizedGainDB.sell_date >= year_start)
        .all()
    )
    realized_short = sum((r.gain_loss for r in realized if r.term == "short"), Decimal("0"))
    realized_long = sum((r.gain_loss for r in realized if r.term == "long"), Decimal("0"))
    realized_total = realized_short + realized_long

    opps = build_harvest_opportunities(db, short_term_rate, long_term_rate)
    rec = [o for o in opps if o["recommended"]]
    harvestable_savings = sum((o["estimated_tax_savings"] for o in rec), Decimal("0"))
    harvestable_loss = sum((-o["unrealized_loss"] for o in rec), Decimal("0"))

    useful_loss_budget = max(realized_total, Decimal("0")) + Decimal("3000")

    raw = redis_client.get("prices_last_refreshed")
    prices_as_of = raw.decode() if raw else None

    return {
        "total_market_value": total_market_value,
        "total_cost_basis": total_cost_basis,
        "total_unrealized_gain_loss": total_unrealized,
        "realized_ytd_short": realized_short,
        "realized_ytd_long": realized_long,
        "realized_ytd_total": realized_total,
        "harvestable_recommended_loss": harvestable_loss,
        "harvestable_recommended_savings": harvestable_savings,
        "useful_loss_budget": useful_loss_budget,
        "prices_as_of": prices_as_of,
    }


@app.get("/replacement-suggestions")
def get_replacement_suggestions(db: Session = Depends(get_db)):
    opportunities = build_harvest_opportunities(db, SHORT_TERM_TAX_RATE, LONG_TERM_TAX_RATE)

    suggestions = []
    for o in opportunities:
        security = (
            db.query(SecurityDB)
            .filter(SecurityDB.symbol == o["symbol"])
            .first()
        )

        if security is None or security.sector is None:
            replacements = []
        else:
            alternatives = (
                db.query(SecurityDB)
                .filter(SecurityDB.sector == security.sector)
                .filter(SecurityDB.symbol != security.symbol)
                .all()
            )
            replacements = [a.symbol for a in alternatives]

        suggestions.append({
            "sell_symbol": o["symbol"],
            "estimated_tax_savings": o["estimated_tax_savings"],
            "replacements": replacements,
        })

    return suggestions