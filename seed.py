import requests

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
    {"symbol": "BTC-USD", "name": "Bitcoin",              "asset_class": "crypto", "currency": "USD", "sector": "crypto"},
    {"symbol": "ETH-USD", "name": "Ethereum",             "asset_class": "crypto", "currency": "USD", "sector": "crypto"},
]

# symbol: (quantity, cost multiplier vs. live price, trade_date)
#   multiplier < 1  -> bought cheaper than today  = a winner (held)
#   multiplier > 1  -> bought higher than today   = underwater = harvest candidate
plan = {
    "AAPL":    (40,  0.55, "2021-03-15"),  # long-term winner
    "MSFT":    (15,  1.25, "2024-02-10"),  # long-term loss -> harvest
    "NVDA":    (60,  0.30, "2020-06-01"),  # big long-term winner
    "JNJ":     (30,  1.10, "2025-09-01"),  # SHORT-term loss -> harvest
    "PFE":     (100, 1.30, "2024-01-15"),  # long-term loss -> harvest
    "JPM":     (25,  0.70, "2022-08-01"),  # winner
    "XOM":     (50,  1.15, "2025-10-01"),  # short-term loss -> harvest
    "KO":      (80,  0.90, "2023-04-01"),  # winner
    "PG":      (40,  1.05, "2025-11-01"),  # small short-term loss
    "VOO":     (20,  0.75, "2022-01-10"),  # winner
    "QQQ":     (10,  1.20, "2024-11-01"),  # long-term loss -> harvest
    "BTC-USD": (0.5, 1.10, "2026-06-20"),  # recent buy + loss, but crypto = no wash sale
    "ETH-USD": (5,   0.60, "2024-05-01"),  # crypto winner
}

# 1. add the securities
for s in securities:
    r = requests.post(f"{BASE}/securities", json=s)
    print("security", s["symbol"], r.status_code)

# 2. pull live prices so cost bases are realistic
requests.post(f"{BASE}/refresh-prices")
live = {s["symbol"]: s["latest_price"] for s in requests.get(f"{BASE}/securities").json()}

# 3. create the buys at cost = live price * multiplier
for symbol, (qty, mult, tdate) in plan.items():
    price = live.get(symbol)
    if price is None:
        print("skip (no live price)", symbol)
        continue
    cost = round(float(price) * mult, 2)
    txn = {
        "symbol": symbol, "quantity": qty, "price": cost,
        "transaction_type": "BUY", "trade_date": tdate,
        "broker": "fidelity", "fees": 0.0, "currency": "USD",
    }
    r = requests.post(f"{BASE}/transactions", json=txn)
    print("buy", symbol, qty, "@", cost, r.status_code)

print("done — reload the dashboard")