"""Single entry point for every outbound market-data call.

Two vendors, split strictly by capability:

  * Finnhub  -- live prices. The websocket trade stream (see price_stream.py)
                and the REST `/quote` endpoint for one-off spot prices.
  * yfinance -- historical daily bars ONLY. Finnhub's candle endpoints
                (/stock/candle, /crypto/candle) are premium and return
                403 "You don't have access to this resource" on our plan,
                so history stays with Yahoo.

Nothing else in the codebase is allowed to call a market-data vendor
directly. Centralising it here is what makes it possible to state, in one
place, that every outbound call has a timeout and a bounded retry -- and it
means swapping a vendor is a change to this file rather than a grep across
the app.

Symbol conventions differ between the two vendors (Finnhub wants
"BINANCE:BTCUSDT", Yahoo wants "BTC-USD"), which is why SecurityDB carries
both `symbol` (canonical = Finnhub) and `history_symbol` (Yahoo override).
Callers resolve that mapping; this module just takes whatever it is given.
"""

import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

import json
import redis
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

FINNHUB_BASE = "https://finnhub.io/api/v1"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Outbound calls are bounded so a slow vendor can never hold a request
# thread (or a Celery worker) open indefinitely.
HTTP_TIMEOUT = 5          # seconds per attempt
MAX_ATTEMPTS = 2          # initial call + one retry
RETRY_BACKOFF = 0.5       # seconds, doubled per attempt

# Daily bars change once per session, so re-fetching them on every dashboard
# load is pure waste. One hour is short enough to pick up today's close
# reasonably promptly and long enough to absorb a page-refresh loop.
HISTORY_CACHE_TTL = 3600

_redis = redis.Redis.from_url(REDIS_URL)


class PriceUnavailable(Exception):
    """Raised when no vendor could supply a usable price."""


def _finnhub_get(path: str, params: dict) -> dict:
    """GET a Finnhub endpoint with a timeout and one bounded retry."""
    params = {**params, "token": os.getenv("FINNHUB_API_KEY")}
    last_error = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.get(
                f"{FINNHUB_BASE}{path}",
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            if response.status_code == 403:
                # Premium endpoint on a free key. Retrying will never help.
                raise PriceUnavailable(
                    f"Finnhub {path} requires a paid plan (403)"
                )
            response.raise_for_status()
            return response.json()
        except PriceUnavailable:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < MAX_ATTEMPTS:
                import time
                time.sleep(RETRY_BACKOFF * (2 ** attempt))

    raise PriceUnavailable(f"Finnhub {path} failed: {last_error}")


def get_quote(symbol: str, history_symbol: Optional[str] = None) -> Decimal:
    """Current price for one symbol.

    Finnhub's /quote covers equities on the free plan. It does NOT cover
    crypto (that lives behind the premium candle endpoints), and for an
    unrecognised symbol it returns a well-formed response with c=0 rather
    than an error -- so a zero is treated as "no price", not as free money.

    When Finnhub can't help and the caller supplied a `history_symbol`, we
    fall back to the most recent daily close from Yahoo. That's staler than
    a real quote, but it is a genuine traded price and it keeps crypto
    market orders working on a free plan.
    """
    try:
        data = _finnhub_get("/quote", {"symbol": symbol})
        current = data.get("c")
        if current:  # non-zero and not None
            return Decimal(str(current))
    except PriceUnavailable:
        pass

    if history_symbol:
        history = get_daily_history(history_symbol, days=5)
        if history:
            return Decimal(str(history[-1]["close"]))

    raise PriceUnavailable(f"No price available for {symbol}")


def get_daily_history(symbol: str, days: int = 90) -> List[dict]:
    """Daily closes for the last `days` days, oldest first.

    Yahoo, via yfinance, because Finnhub's candles are premium. Cached in
    Redis so a dashboard refresh doesn't re-hit the vendor. Returns
    [{"date": "YYYY-MM-DD", "close": float}, ...]; an empty list means the
    symbol returned nothing (unknown ticker, or delisted).
    """
    cache_key = f"price_history:{symbol}:{days}"

    cached = _redis.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    end = datetime.now()
    start = end - timedelta(days=days)

    try:
        data = yf.Ticker(symbol).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
    except Exception as exc:  # yfinance raises a variety of network errors
        raise PriceUnavailable(f"History fetch failed for {symbol}: {exc}")

    if data.empty:
        history: List[dict] = []
    else:
        history = [
            {"date": idx.strftime("%Y-%m-%d"), "close": round(float(row["Close"]), 2)}
            for idx, row in data.iterrows()
        ]

    # Cache even an empty result, briefly, so a bad symbol can't turn into a
    # tight retry loop against Yahoo.
    _redis.setex(cache_key, HISTORY_CACHE_TTL if history else 60, json.dumps(history))
    return history
