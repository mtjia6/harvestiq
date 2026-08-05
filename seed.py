"""Seed a demo portfolio.

Run with the API up (`uvicorn app.main:app --reload`) in another terminal:

    python3 seed.py

Symbols are canonical FINNHUB symbols, because that's what the live price
stream subscribes to. Anything Yahoo names differently carries a
`history_symbol` so the 90-day chart still resolves.
"""

import requests

from app.prices import get_quote, PriceUnavailable

BASE = "http://127.0.0.1:8000"

securities = [
    {"symbol": "AAPL",    "name": "Apple Inc",            "asset_class": "equity", "currency": "USD", "sector": "technology"},
    {"symbol": "MSFT",    "name": "Microsoft Corp",       "asset_class": "equity", "currency": "USD", "sector": "technology"},
    {"symbol": "NVDA",    "name": "NVIDIA Corp",          "asset_class": "equity", "currency": "USD", "sector": "technology"},
    {"symbol": "JNJ",     "name": "Johnson & Johnson",    "asset_class": "equity", "currency": "USD", "sector": "healthcare"},
    {"symbol": "PFE",     "name": "Pfizer Inc",           "asset_class": "equity", "currency": "USD", "sector": "healthcare"},
    {"symbol": "JPM",     "name": "JPMorgan Chase",       "asset_class": "equity", "currency": "USD", "sector": "financials"},
    {"symbol": "XOM",     "name": "Exxon Mobil",          "asset_class": "equity", "currency": "USD", "sector": "energy"},
    {"symbol": "KO",      "name": "Coca-Cola Co",         "asset_class": "equity", "currency": "USD", "sector": "consumer"},
    {"symbol": "PG",      "name": "Procter & Gamble",     "asset_class": "equity", "currency": "USD", "sector": "consumer"},
    {"symbol": "VOO",     "name": "Vanguard S&P 500 ETF", "asset_class": "equity", "currency": "USD", "sector": "etf"},
    {"symbol": "QQQ",     "name": "Invesco QQQ ETF",      "asset_class": "equity", "currency": "USD", "sector": "etf"},
    # Crypto: Finnhub streams these as BINANCE:*, Yahoo charts them as *-USD.
    {"symbol": "BINANCE:BTCUSDT", "history_symbol": "BTC-USD", "name": "Bitcoin",  "asset_class": "crypto", "currency": "USD", "sector": "crypto"},
    {"symbol": "BINANCE:ETHUSDT", "history_symbol": "ETH-USD", "name": "Ethereum", "asset_class": "crypto", "currency": "USD", "sector": "crypto"},
]

buys = {
    "AAPL":            (40,  0.55, "2021-03-15"),
    "MSFT":            (15,  1.25, "2024-02-10"),
    "NVDA":            (60,  0.30, "2020-06-01"),
    "JNJ":             (30,  1.10, "2025-09-01"),
    "PFE":             (100, 1.30, "2024-01-15"),
    "JPM":             (25,  0.70, "2022-08-01"),
    "XOM":             (50,  1.15, "2025-10-01"),
    "KO":              (80,  0.90, "2023-04-01"),
    "PG":              (40,  1.05, "2025-11-01"),
    "VOO":             (20,  0.75, "2022-01-10"),
    "QQQ":             (10,  1.20, "2024-11-01"),
    "BINANCE:BTCUSDT": (0.5, 1.10, "2026-06-20"),
    "BINANCE:ETHUSDT": (5,   0.60, "2024-05-01"),
}

# sells made earlier THIS year -> these create realized gains/losses (YTD)
# symbol: (quantity, sell date)   sold at ~live price
sells = {
    "NVDA": (20, "2026-03-15"),  # big long-term gain
    "AAPL": (10, "2026-05-01"),  # long-term gain
    "JPM":  (10, "2026-02-20"),  # gain
    "XOM":  (20, "2026-04-10"),  # short-term loss
    "MSFT": (5, "2026-05-01"),
}

# 1. add securities
for s in securities:
    r = requests.post(f"{BASE}/securities", json=s)
    print("security", s["symbol"], r.status_code)

# 2. seed prices so cost bases / sell prices are realistic.
#    The old POST /refresh-prices task was retired with the polling pipeline;
#    in normal operation the streamer keeps prices current. For seeding we
#    just pull one quote per symbol through the same vendor module the app
#    uses, then write it via the existing price endpoint.
live = {}
for s in securities:
    try:
        price = get_quote(s["symbol"], s.get("history_symbol"))
    except PriceUnavailable as exc:
        print("price", s["symbol"], "SKIPPED —", exc)
        continue
    requests.post(
        f"{BASE}/securities/{s['symbol']}/price",
        json={"latest_price": str(price)},
    )
    live[s["symbol"]] = price
    print("price", s["symbol"], price)

# 3. buys (cost = live * multiplier)
for symbol, (qty, mult, tdate) in buys.items():
    price = live.get(symbol)
    if price is None:
        continue
    cost = round(float(price) * mult, 2)
    requests.post(f"{BASE}/transactions", json={
        "symbol": symbol, "quantity": qty, "price": cost,
        "transaction_type": "BUY", "trade_date": tdate,
        "broker": "fidelity", "fees": 0.0, "currency": "USD",
    })
    print("buy", symbol, qty, "@", cost)

# 4. sells this year (sold at ~live price) -> realized gains/losses
for symbol, (qty, tdate) in sells.items():
    price = live.get(symbol)
    if price is None:
        continue
    sell_price = round(float(price), 2)
    r = requests.post(f"{BASE}/transactions", json={
        "symbol": symbol, "quantity": qty, "price": sell_price,
        "transaction_type": "SELL", "trade_date": tdate,
        "broker": "fidelity", "fees": 0.0, "currency": "USD",
    })
    print("sell", symbol, qty, "@", sell_price, r.status_code)

print("done — reload the dashboard")
