# HarvestIQ

An AI-assisted **tax-loss harvesting engine** for stock and crypto portfolios. HarvestIQ tracks your holdings at the tax-lot level, values them against live market prices, finds the positions worth selling to reduce your tax bill, checks each sale against the IRS wash-sale rule, suggests replacement securities so you stay invested, and explains the whole plan in plain English.

> ⚠️ **Educational tool, not tax advice.** HarvestIQ makes simplifying assumptions (see [Known limitations](#known-limitations)). It's for learning and decision *support*, not for filing taxes.

---

## What is tax-loss harvesting?

When you sell an investment for less than you paid, that **capital loss** can offset your **capital gains** (and up to $3,000 of ordinary income per year), lowering your tax bill. Harvesting is the deliberate practice of selling losing positions to capture those losses — while staying invested. The catch is the details: you need to know your exact cost basis per lot, your holding period (short- vs long-term), how much tax a sale actually saves, and whether the **wash-sale rule** (rebuying the same security within ±30 days disallows the loss) would nullify the benefit. HarvestIQ automates all of it.

**Worked example.** You own 100 AAPL bought at $200 ($20,000 cost basis); it's now $150 ($15,000). You also have an $8,000 realized gain this year. Sell the AAPL → realize a $5,000 loss → it cancels $5,000 of your gain → at a ~20% rate you save ~$1,000 in tax. Rebuy AAPL immediately and the loss is disallowed (wash sale); instead buy a similar-but-not-identical security (e.g. a tech ETF) and you keep both the tax saving and your market exposure.

---

## Features

- **Transaction recording & validation** — buys/sells with type, quantity, price, date; rejects bad data and unknown symbols.
- **Security master** — a canonical catalog of tradable assets (equity or crypto), each with a sector.
- **Tax-lot tracking** — every purchase is its own lot with fee-inclusive cost basis.
- **FIFO cost basis** — sells consume the oldest lots first, with an oversell guard.
- **Realized gains/losses** — computed on sale, classified short- vs long-term by holding period.
- **Unrealized gains/losses** — open lots valued against current market prices.
- **Wash-sale detection** — flags losses tainted by a purchase within the ±30-day window (correctly exempts crypto).
- **Harvest opportunity ranking** — ranks losing positions by estimated tax savings using *configurable* tax rates.
- **Replacement suggestions** — same-sector, different-symbol alternatives to avoid wash sales.
- **AI explanations** — natural-language harvest strategy generated via the Groq API.
- **Live market data** — prices and 90-day history pulled from Yahoo Finance (`yfinance`).
- **Dashboard** — a single-page UI with holdings, harvest table, price charts, AI summary, realized-gains log, and CSV export.

---

## Tech stack

- **Backend:** FastAPI, SQLAlchemy, PostgreSQL
- **Migrations:** Alembic
- **Market data:** yfinance (Yahoo Finance)
- **AI:** Groq API (`openai/gpt-oss-20b`)
- **Frontend:** vanilla HTML/JS + Chart.js (served by FastAPI)
- **Tests:** pytest + FastAPI TestClient (in-memory SQLite)

---

## How it works

```
transactions ─▶ validation ─▶ security master
     │
     ▼
  tax lots ─▶ FIFO cost basis ─▶ realized gains (short/long term)
     │
     ▼
 live prices ─▶ unrealized gains ─▶ harvest opportunities (ranked, rate-aware)
                                          │
                        wash-sale check ──┤
                                          ▼
                      replacement suggestions ─▶ AI explanation ─▶ dashboard
```

---

## Setup

**Prerequisites:** Python 3.9+, PostgreSQL running locally, and a free [Groq API key](https://console.groq.com).

```bash
# 1. Clone and enter the project
git clone https://github.com/mtjia6/harvestiq.git
cd harvestiq

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create the database
createdb harvestiq

# 5. Add your Groq API key
echo "GROQ_API_KEY=your_key_here" > .env

# 6. Create the schema
alembic upgrade head

# 7. Run the server
uvicorn app.main:app --reload
```

Then open the dashboard at **http://127.0.0.1:8000/dashboard** and the interactive API docs at **http://127.0.0.1:8000/docs**.

### Load demo data (optional)

With the server running, in a second terminal:

```bash
python3 seed.py
```

This creates a diversified sample portfolio (equities + crypto across several sectors) and pulls live prices, so the dashboard has realistic data to show.

---

## Key API endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /transactions` | Record a buy/sell |
| `POST /transactions/market` | Buy/sell at the current live price |
| `POST /securities` | Add a security to the master list |
| `POST /refresh-prices` | Pull live prices for all securities |
| `GET /portfolio` | Current holdings, derived from lots |
| `GET /portfolio-summary` | Totals: value, unrealized/realized P&L, harvestable savings |
| `GET /tax-lots` | All tax lots |
| `GET /realized-gains` | Realized gains/losses with term |
| `GET /unrealized-gains` | Unrealized P&L on open lots |
| `GET /wash-sales` | Wash-sale flags on realized losses |
| `GET /harvest-opportunities` | Ranked harvest candidates (accepts `short_term_rate`, `long_term_rate`) |
| `GET /replacement-suggestions` | Wash-sale-safe alternatives |
| `GET /harvest-explanation` | AI-written harvest summary |
| `GET /price-history/{symbol}` | 90-day price history for charts |

---

## Running the tests

```bash
python3 -m pytest -v
```

The suite covers the core tax math: fee-inclusive cost basis, FIFO ordering, the oversell guard, realized gains and term classification, wash-sale detection, and the crypto wash-sale exemption.

---

## Known limitations

HarvestIQ is a learning project and deliberately simplifies some real-world tax mechanics:

- **Not multi-user** — there's no authentication; all data is shared.
- **Flat tax rates** — you supply short-/long-term rates; it doesn't model full brackets or the 0% long-term band.
- **No wash-sale basis adjustment** — disallowed losses are flagged but not rolled into the replacement lot's cost basis / holding period.
- **Simplified loss budget** — the "useful loss" figure is an approximation (realized gains + $3,000).
- **Single-account** — wash-sale checks don't span multiple accounts (real rules include IRAs and spouses).
- **Manual price refresh** — prices update when you click refresh, not automatically.

---

## Disclaimer

HarvestIQ provides general information for educational purposes only. It is **not** personalized tax, legal, or financial advice. Consult a qualified professional before making investment or tax decisions.
