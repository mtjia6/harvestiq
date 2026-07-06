from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer
from typing import Annotated, List, Literal, Optional
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
from groq import Groq
import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
import redis.asyncio as aioredis

from app.database import get_db, SessionLocal
from app.model import TransactionDB, SecurityDB, TaxLotDB, RealizedGainDB
from app.celery_app import celery_app, redis_client, generate_harvest_explanation, run_tax_analysis
from app.harvest import build_harvest_opportunities, get_ytd_realized_totals, build_portfolio_summary
from app import prices
from app.prices import PriceUnavailable

from dotenv import load_dotenv
load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Floor on how often we rebuild + push the summary, in seconds. The streamer can
# publish once a second all day; without this, every tick costs a full portfolio
# recompute. Bursts inside the window collapse into a single push.
MIN_PUSH_INTERVAL = float(os.getenv("WS_MIN_PUSH_INTERVAL", "2"))

# How long one client may take to accept a push before we give up and drop it.
# Stops a single stalled socket from holding up the broadcast to everyone else.
CLIENT_SEND_TIMEOUT = 5.0

# Every live /ws/prices socket. There is exactly ONE Redis subscription and ONE
# summary computation per tick for this whole set - not one per client.
price_clients = set()

# Set by the Redis listener, consumed by the broadcaster. An Event (not a queue)
# is what makes coalescing free: N publishes inside one window set the same flag.
# Created inside the lifespan, NOT at import: an asyncio.Event binds to the first
# running loop that awaits it, so a module-level one breaks the moment the app is
# run on a second loop (tests, reload workers, an app restarted in-process).
_price_dirty = None


async def _aclose(obj):
    # redis-py renamed the async close to aclose() in 5.x and deprecated close().
    closer = getattr(obj, "aclose", None) or obj.close
    await closer()


async def _pubsub_listener(pubsub, dirty):
    # Owns the single Redis subscription. Does no work beyond raising the flag,
    # so a slow recompute can never back up the pub/sub connection.
    async for message in pubsub.listen():
        if message.get("type") == "message":
            dirty.set()


async def _broadcast(payload):
    async def send(ws):
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=CLIENT_SEND_TIMEOUT)
        except Exception:
            # Dead or stalled client: drop it. The socket handler's finally block
            # discards it too; set.discard is idempotent so the race is harmless.
            price_clients.discard(ws)

    await asyncio.gather(*(send(ws) for ws in list(price_clients)))


async def _broadcaster(dirty):
    # Single consumer: wait for a price change, rebuild the summary ONCE, fan it
    # out to every client, then sleep out the rate-limit window.
    while True:
        await dirty.wait()
        dirty.clear()
        try:
            if price_clients:  # nobody watching: skip the DB work entirely
                payload = await run_in_threadpool(_summary_json)
                await _broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one bad tick kill the broadcaster
            print("[ws] broadcast failed: %r" % (exc,))
        await asyncio.sleep(MIN_PUSH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _price_dirty
    _price_dirty = asyncio.Event()
    redis_conn = aioredis.from_url(REDIS_URL)
    pubsub = redis_conn.pubsub()
    await pubsub.subscribe("prices")
    tasks = [
        asyncio.create_task(_pubsub_listener(pubsub, _price_dirty)),
        asyncio.create_task(_broadcaster(_price_dirty)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await pubsub.unsubscribe("prices")
        await _aclose(pubsub)
        await _aclose(redis_conn)
        _price_dirty = None


TAGS_METADATA = [
    {"name": "Transactions", "description": "Record buys and sells. Every buy opens a tax lot; every sell consumes lots FIFO and writes realized gains."},
    {"name": "Securities", "description": "The security master. `symbol` is the canonical Finnhub symbol; `history_symbol` overrides it for Yahoo (historical bars only)."},
    {"name": "Portfolio", "description": "Read-side views of holdings and P&L, derived from tax lots."},
    {"name": "Tax analysis", "description": "Harvest opportunities and the cached portfolio-level analysis. Expensive to compute, so it runs on a worker and is served from a Redis cache."},
    {"name": "Jobs", "description": "Poll background jobs enqueued by the 202-returning endpoints."},
    {"name": "Dashboard", "description": "The single-page UI and its live price socket."},
]

app = FastAPI(
    title="HarvestIQ",
    version="0.3.0",
    description=(
        "An AI-assisted **tax-loss harvesting engine** for stock and crypto "
        "portfolios. Tracks holdings at the tax-lot level, values them against "
        "live prices streamed from Finnhub, ranks losing positions by estimated "
        "tax savings, and checks each against the IRS wash-sale rule.\n\n"
        "**Educational tool, not tax advice.** See the README for the list of "
        "deliberate simplifications — flat tax rates, same-symbol-only wash-sale "
        "matching, FIFO-only lot selection, and per-position (not portfolio-level) "
        "savings estimates."
    ),
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)

SHORT_TERM_TAX_RATE = Decimal("0.40")  # default: ~35% federal + ~5% GA
LONG_TERM_TAX_RATE = Decimal("0.20")   # default: ~15% federal + ~5% GA


def _plain_decimal(value: Decimal) -> str:
    """Render a Decimal as a plain string — no exponent, no trailing zeros.

    Money crosses the wire as a string so no client can silently round it
    through a float. `Numeric(20, 8)` means Postgres hands back values like
    `20.00000000`; normalising keeps that readable without losing precision
    where it matters.
    """
    normalized = value.normalize()
    if normalized.as_tuple().exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


# Money and quantities are Decimals internally and strings on the wire.
Money = Annotated[Decimal, PlainSerializer(_plain_decimal, return_type=str)]

# Tax rates are fractions, not percentages. Without bounds a caller can pass
# `short_term_rate=50` and get a confidently wrong answer back.
RateQuery = Annotated[
    Decimal,
    Query(ge=0, le=1, description="Marginal tax rate as a fraction (0.40 = 40%)."),
]


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    quantity: Money
    price: Money
    transaction_type: str
    trade_date: date
    broker: str
    fees: Money
    currency: str


class SecurityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    history_symbol: Optional[str] = None
    name: str
    asset_class: str
    currency: str
    latest_price: Optional[Money] = None
    sector: Optional[str] = None


class TaxLotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    open_transaction_id: int
    quantity_opened: Money
    quantity_remaining: Money
    broker: Optional[str] = None
    cost_basis_per_unit: Money
    opened_date: date
    status: str


class RealizedGainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    sell_transaction_id: int
    lot_id: int
    quantity_sold: Money
    proceeds: Money
    cost_basis: Money
    gain_loss: Money
    term: str
    sell_date: date


class JobAccepted(BaseModel):
    """Returned by endpoints that enqueue background work.

    The job id is polled at `GET /jobs/{job_id}`; the same path is echoed in
    the `Location` response header.
    """

    job_id: str


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


@app.get("/", tags=["Dashboard"], summary="Liveness ping")
def home():
    return {"message": "HarvestIQ running"}


@app.get(
    "/dashboard",
    tags=["Dashboard"],
    summary="The single-page dashboard",
    response_class=FileResponse,
)
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
            .filter(TaxLotDB.broker == transaction.broker)
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
            broker=transaction.broker,
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
    return db_transaction


@app.post(
    "/transactions",
    tags=["Transactions"],
    status_code=status.HTTP_201_CREATED,
    response_model=TransactionOut,
    summary="Record a buy or sell",
    description=(
        "A BUY opens a tax lot with fee-inclusive cost basis. A SELL consumes "
        "open lots FIFO within the same broker and writes a realized gain per "
        "lot consumed, classified short- or long-term by holding period.\n\n"
        "Returns the created transaction, including its id."
    ),
    responses={
        400: {"description": "Unknown symbol, or not enough of the position to sell."},
    },
)
def create_transaction(transaction: Transaction, db: Session = Depends(get_db)):
    return process_transaction(transaction, db)


@app.post(
    "/transactions/market",
    tags=["Transactions"],
    status_code=status.HTTP_201_CREATED,
    response_model=TransactionOut,
    summary="Record a buy or sell at the current market price",
    description=(
        "Same as `POST /transactions`, but the fill price is taken from the "
        "streamed price already in the database, falling back to a Finnhub "
        "spot quote when the stream hasn't priced the symbol yet."
    ),
    responses={
        400: {"description": "Unknown symbol, or not enough of the position to sell."},
        502: {"description": "No price could be obtained from any vendor."},
    },
)
def create_market_transaction(market: MarketTransaction, db: Session = Depends(get_db)):
    security = (
        db.query(SecurityDB)
        .filter(SecurityDB.symbol == market.symbol)
        .first()
    )
    if security is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown symbol: {market.symbol}",
        )

    # Prefer the price the streamer already put in the DB -- it's fresher than
    # any REST call and costs no network I/O in the request path. Only reach
    # for a vendor quote when the stream hasn't priced this symbol yet.
    if security.latest_price is not None:
        current_price = security.latest_price
    else:
        try:
            current_price = prices.get_quote(market.symbol, security.history_symbol)
        except PriceUnavailable as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not fetch a market price for {market.symbol}: {exc}",
            )

    current_price = Decimal(str(round(float(current_price), 2)))

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


@app.get(
    "/transactions",
    tags=["Transactions"],
    response_model=List[TransactionOut],
    summary="List recorded transactions",
)
def get_transactions(db: Session = Depends(get_db)):
    transactions = db.query(TransactionDB).all()
    return transactions


@app.get(
    "/portfolio",
    tags=["Portfolio"],
    summary="Current holdings aggregated from open lots",
)
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


@app.get(
    "/tax-lots",
    tags=["Portfolio"],
    response_model=List[TaxLotOut],
    summary="All tax lots, open and closed",
)
def get_tax_lots(db: Session = Depends(get_db)):
    return db.query(TaxLotDB).all()


@app.get(
    "/realized-gains",
    tags=["Portfolio"],
    response_model=List[RealizedGainOut],
    summary="Realized gains and losses, with short/long term classification",
)
def get_realized_gains(db: Session = Depends(get_db)):
    return db.query(RealizedGainDB).all()


class Security(BaseModel):
    # symbol is the Finnhub symbol (what the live stream subscribes to);
    # history_symbol is the Yahoo override, needed for anything Yahoo names
    # differently (crypto especially). Leave it unset for plain US equities.
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    asset_class: Literal["equity", "crypto"]
    currency: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    history_symbol: Optional[str] = None


@app.post(
    "/securities",
    tags=["Securities"],
    status_code=status.HTTP_201_CREATED,
    response_model=SecurityOut,
    summary="Add a security to the master list",
    responses={409: {"description": "A security with this symbol already exists."}},
)
def create_security(security: Security, db: Session = Depends(get_db)):
    # `symbol` is unique in the schema, so without this check a repeat insert
    # surfaces as an unhandled IntegrityError (500) rather than a 409.
    existing = (
        db.query(SecurityDB)
        .filter(SecurityDB.symbol == security.symbol)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Security already exists: {security.symbol}",
        )

    db_security = SecurityDB(
        symbol=security.symbol,
        history_symbol=security.history_symbol,
        name=security.name,
        asset_class=security.asset_class,
        currency=security.currency,
        sector=security.sector,
    )
    db.add(db_security)
    db.commit()
    return db_security


@app.get(
    "/securities",
    tags=["Securities"],
    response_model=List[SecurityOut],
    summary="List the security master",
)
def get_securities(db: Session = Depends(get_db)):
    return db.query(SecurityDB).all()


class PriceUpdate(BaseModel):
    latest_price: Decimal = Field(ge=0)


@app.post(
    "/securities/{symbol}/price",
    tags=["Securities"],
    response_model=SecurityOut,
    summary="Manually set a security's latest price",
    description=(
        "Normally the Finnhub streamer keeps this current; this endpoint exists "
        "for seeding and for symbols the stream doesn't cover."
    ),
    responses={404: {"description": "Unknown symbol."}},
)
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
    return security


@app.get(
    "/unrealized-gains",
    tags=["Portfolio"],
    summary="Unrealized P&L per open lot",
)
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


@app.get(
    "/wash-sales",
    tags=["Tax analysis"],
    summary="Flag realized losses tainted by a repurchase",
    description=(
        "Checks each realized loss for a purchase of the same symbol inside the "
        "±30-day window. Crypto is exempt (treated as property, not a security). "
        "Matching is same-symbol only, not 'substantially identical'."
    ),
)
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


@app.get(
    "/harvest-opportunities",
    tags=["Tax analysis"],
    summary="Rank losing positions by estimated tax savings",
    description=(
        "Computed live on every call — expensive. For the cached version that "
        "the dashboard reads, use `GET /tax-analysis`.\n\n"
        "**Caveat:** each opportunity is valued independently against the same "
        "starting net-gain position, so the individual figures are comparable "
        "but do not sum to a portfolio-level total."
    ),
)
def get_harvest_opportunities(
    short_term_rate: RateQuery = SHORT_TERM_TAX_RATE,
    long_term_rate: RateQuery = LONG_TERM_TAX_RATE,
    db: Session = Depends(get_db),
):
    return build_harvest_opportunities(db, short_term_rate, long_term_rate)


@app.post(
    "/harvest-explanations",
    tags=["Tax analysis"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Request an AI-written explanation of the harvest plan",
    description=(
        "Enqueues an LLM call and returns immediately. Poll `GET /jobs/{job_id}` "
        "for the result; the same URL is in the `Location` header.\n\n"
        "This is a POST because it costs money and is not idempotent — it must "
        "not be triggered by a prefetch or a browser retry."
    ),
)
def create_harvest_explanation(response: Response):
    job = generate_harvest_explanation.delay()
    response.headers["Location"] = f"/jobs/{job.id}"
    return {"job_id": job.id}


@app.post(
    "/tax-analysis/run",
    tags=["Tax analysis"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Recompute the cached tax analysis",
    description=(
        "Enqueues the expensive harvest computation on a worker, which writes "
        "the result to the Redis cache served by `GET /tax-analysis`. Poll "
        "`GET /jobs/{job_id}` to know when the cache is fresh."
    ),
)
def start_tax_analysis(
    response: Response,
    short_term_rate: RateQuery = SHORT_TERM_TAX_RATE,
    long_term_rate: RateQuery = LONG_TERM_TAX_RATE,
):
    # Trigger a fresh recompute in the background; result gets cached in Redis.
    job = run_tax_analysis.delay(str(short_term_rate), str(long_term_rate))
    response.headers["Location"] = f"/jobs/{job.id}"
    return {"job_id": job.id}


@app.get(
    "/tax-analysis",
    tags=["Tax analysis"],
    summary="Serve the cached harvest analysis",
    description="Reads the Redis cache written by the worker. Never recomputes.",
    responses={
        503: {"description": "No analysis has been computed yet — POST /tax-analysis/run first."},
    },
)
def get_tax_analysis():
    # Serve the cached analysis instantly (no recompute).
    raw = redis_client.get("tax_analysis")
    if raw is None:
        # A 200 with a "not_computed" body would force every client to inspect
        # the payload to find out whether it actually got an answer.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No analysis computed yet. Run POST /tax-analysis/run first.",
        )
    return json.loads(raw)


@app.get(
    "/jobs/{job_id}",
    tags=["Jobs"],
    summary="Poll a background job",
    description=(
        "Status is Celery's: PENDING, STARTED, SUCCESS, FAILURE. Note that "
        "PENDING also covers an unknown job id — Celery cannot distinguish "
        "'queued' from 'never existed'."
    ),
)
def get_job_status(job_id: str):
    # Check on a background job by its id.
    result = celery_app.AsyncResult(job_id)
    return {
        "job_id": job_id,
        "status": result.status,  # PENDING, STARTED, SUCCESS, FAILURE
        "result": result.result if result.successful() else None,
    }


@app.get(
    "/price-history/{symbol}",
    tags=["Securities"],
    summary="Daily closes for charting",
    description=(
        "Historical bars come from Yahoo (Finnhub's candle endpoints are "
        "premium) and are cached in Redis for an hour."
    ),
    responses={
        404: {"description": "No history available for this symbol."},
        502: {"description": "The history vendor could not be reached."},
    },
)
def get_price_history(
    symbol: str,
    days: Annotated[int, Query(ge=1, le=3650, description="Lookback window in days.")] = 90,
    db: Session = Depends(get_db),
):
    # `symbol` is the canonical (Finnhub) symbol. History comes from Yahoo,
    # which names some instruments differently, so resolve the override here
    # rather than making the caller know about vendor conventions.
    security = (
        db.query(SecurityDB)
        .filter(SecurityDB.symbol == symbol)
        .first()
    )
    lookup = (security.history_symbol or symbol) if security else symbol

    try:
        history = prices.get_daily_history(lookup, days=days)
    except PriceUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not history:
        raise HTTPException(status_code=404, detail=f"No price history for {symbol}")

    return {"symbol": symbol, "history": history}


def _build_summary(db):
    # Read the cached harvest opportunities (Redis) and hand them to the pure
    # summary computation in harvest.py. Both the HTTP endpoint and the
    # websocket push call this, so the math lives in exactly one place.
    raw = redis_client.get("tax_analysis")
    opportunities = json.loads(raw)["opportunities"] if raw else []
    summary = build_portfolio_summary(db, opportunities)
    raw_ts = redis_client.get("prices_last_refreshed")
    summary["prices_as_of"] = raw_ts.decode() if raw_ts else None
    return summary


def _summary_json():
    # Sync helper for the websocket. Opens its own session (it's not in a
    # request, so there's no Depends(get_db)), builds the summary, JSON-encodes
    # with default=str for the Decimals. Runs in a threadpool so this blocking
    # DB work never stalls the async event loop.
    db = SessionLocal()
    try:
        return json.dumps(_build_summary(db), default=str)
    finally:
        db.close()


@app.get(
    "/portfolio-summary",
    tags=["Portfolio"],
    summary="Headline totals for the dashboard",
    description=(
        "Cheap: reads live P&L from Postgres and the harvestable-savings figure "
        "from the Redis cache. The same payload is pushed over `WS /ws/prices`."
    ),
)
def get_portfolio_summary(db: Session = Depends(get_db)):
    return _build_summary(db)


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    # This handler does NOT subscribe to Redis and does NOT compute summaries -
    # the app-wide broadcaster does both, once, for everyone. All this coroutine
    # does is register the client and then sit on receive(), so that a browser
    # closing the tab is actually noticed.
    await websocket.accept()
    price_clients.add(websocket)
    try:
        # Cold start: push the current summary immediately on connect, so a
        # freshly-loaded page renders without waiting for the next price change.
        await websocket.send_text(await run_in_threadpool(_summary_json))

        # A client-initiated close arrives as a RECEIVE event in ASGI. Nothing
        # else in this app reads from the socket, so if we never await receive()
        # the disconnect is never delivered and this task leaks forever.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Starlette raises RuntimeError (not WebSocketDisconnect) when we touch a
        # socket the server has already torn down. Same outcome: we are done.
        pass
    finally:
        price_clients.discard(websocket)


@app.get(
    "/replacement-suggestions",
    tags=["Tax analysis"],
    summary="Wash-sale-safe alternatives for each harvest candidate",
    description=(
        "Same-sector, different-symbol securities from the master list. This is "
        "a placeholder for proper tracking-error-constrained replacement."
    ),
)
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