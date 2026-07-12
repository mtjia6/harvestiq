from celery import Celery
from app.database import SessionLocal
from app.model import SecurityDB
import yfinance as yf
from decimal import Decimal
from datetime import datetime, timezone
import redis

celery_app = Celery(
    "harvestiq",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
)

# Shared store both the worker and the web app can read/write.
redis_client = redis.Redis.from_url("redis://localhost:6379/0")


@celery_app.task
def add(x: int, y: int):
    return x + y


@celery_app.task
def refresh_prices():
    db = SessionLocal()
    try:
        securities = db.query(SecurityDB).all()
        updated = []
        for security in securities:
            ticker = yf.Ticker(security.symbol)
            data = ticker.history(period="1d")

            if data.empty:
                continue

            latest_close = float(data["Close"].iloc[-1])
            security.latest_price = Decimal(str(round(latest_close, 2)))
            updated.append({
                "symbol": security.symbol,
                "latest_price": float(security.latest_price),
            })
        db.commit()
        redis_client.set(
            "prices_last_refreshed",
            datetime.now(timezone.utc).isoformat(),
        )
        return {"updated": updated}
    finally:
        db.close()
