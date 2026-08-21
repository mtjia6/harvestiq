# HarvestIQ

An AI-assisted **tax-loss harvesting engine** for stock and crypto portfolios. HarvestIQ tracks your holdings at the tax-lot level across multiple wallets and accounts, values them against live market prices, and produces an ordered, executable **harvest plan**: which lots to sell, in what order, sized to what actually helps, with three honest numbers per step — what it saves now, what it costs later, and the net.

It applies the real IRS netting waterfall, **enforces** the wash-sale rule rather than just flagging it, and tells you when harvesting is a bad idea.

> ⚠️ **Educational tool, not tax advice.** The harvesting *logic* is rigorous; the *tax rate model* is a stated simplification you supply. Read [Known limitations](#known-limitations) before trusting a dollar figure — that section is not boilerplate.

---

## What is tax-loss harvesting?

When you sell an investment for less than you paid, that **capital loss** can offset your **capital gains** (and up to $3,000 of ordinary income per year), lowering your tax bill. Harvesting is the deliberate practice of selling losing positions to capture those losses while staying invested.

The catch is the details: your exact cost basis per lot, your holding period, how much tax a sale actually saves *given everything else you've already realized this year*, and whether the [**wash-sale rule**](#wash-sales-are-enforced-not-flagged) — which disallows your loss if you rebuy a substantially identical security within 30 days either side of the sale — would nullify the benefit.

**The part most tools skip:** harvesting is mostly a *deferral*, not a saving. Selling at a loss and rebuying resets your cost basis **downward** by exactly the loss you harvested, so the eventual sale owes more. If your future tax rate matches today's, the lifetime benefit is close to zero — you've taken an interest-free loan, not free money. And if the gain you're offsetting would have been taxed at 0% anyway, harvesting actively *costs* you.

HarvestIQ shows you both sides of that trade on every recommendation.

**Worked example.** You own 100 AAPL bought at $200 ($20,000 basis); it's now $150 ($15,000). You also have an $8,000 realized short-term gain this year. Sell the AAPL → realize a $5,000 loss → it cancels $5,000 of that gain → at a 37% rate you save $1,850 now. But your replacement position carries a $15,000 basis instead of $20,000, so you'll owe roughly $750 more when you eventually sell (at a 15% long-term rate). Net lifetime benefit ≈ $1,100, not $1,850. HarvestIQ reports all three numbers.

---

## The tax engine

This is the part worth reading. Most consumer tools stop at "this position is down, sell it."

### Real netting, not a flat multiply

Losses don't just get multiplied by a rate. They cancel gains of their **own character first** (short-term against short-term, long-term against long-term), then cross-net, then hit the **$3,000 ordinary-income cap**, then roll forward as carryforwards that keep their character. `marginal_harvest_value` prices every loss against your *effective* position — realized year-to-date, minus prior-year carryforwards — and returns current-year savings and carryforward generated as **two separate numbers**, never conflated.

This matters most in a year you're already at a net loss: an extra harvest saves you **$0**, and a flat-rate engine will happily report thousands in phantom savings.

### The plan is sequenced, not a ranked list

`GET /harvest-opportunities` prices each lot against the same starting position, so the figures are comparable but **do not sum** — harvest the first one and every other number is stale.

`GET /harvest-plan` simulates actually doing it: price every remaining candidate against the *current* net position, take the best, apply it, repeat. Once short-term gains are exhausted, a short-term loss that ranked first can legitimately fall behind a long-term one, and the plan sees that.

### Sells are sized, not all-or-nothing

Unrealized loss is a scarce, non-renewable resource — harvesting spends it permanently. Selling a $40,000 lot to capture $4,000 of benefit burns ten times what the job needed.

The `carryforward_discount` dial (0–1) is how you tell the engine what banked carryforward is worth to you, and it moves the sell size continuously:

```
sized = useful_loss_budget + (full_lot_loss − useful_loss_budget) × discount
```

At **0** ("no gains coming") sells are sized to land exactly on the remaining useful-loss budget. At **1** ("big gain next year") it banks whole lots. At **0.3** it takes the budget plus 30% of the excess.

### Which lot you sell is the whole trade

**What a lot is.** Every purchase of a security is its own **tax lot**, with its own cost basis and its own purchase date. If you bought AAPL three times, you don't own "100 AAPL at an average price" for tax purposes — you own three separate lots, and a sale of 40 shares has to be attributed to specific ones. Which ones you pick determines the gain or loss you realize and whether it's short- or long-term. Same trade, same proceeds, different tax bill.

**Why the default is the wrong one.** Absent an instruction, the IRS treats a sale as **first-in, first-out** — the oldest shares go first. In a position you've been adding to over time, the oldest shares are usually the cheapest, which means FIFO systematically sells your largest gain and your smallest loss. That is precisely backwards for harvesting: the default method is the one most hostile to the thing you're trying to do. Choosing a lot is not a micro-optimization on top of harvesting; **it is** harvesting.

**Three methods**, on any sell via `lot_selection`:

- `fifo` — oldest lot first. The IRS default, and what you get if you say nothing.
- `hifo` — highest cost basis first, ties broken by age. Realizes the largest loss (or the smallest gain) per share sold; the sane default when you aren't targeting a particular lot.
- `specific` — consume exactly the lots you name in `lot_ids`, in the order you give them. This is **specific identification**, the method the regulations actually contemplate, and the only one that lets a plan be executed as designed.

That last one is why the plan is executable rather than advisory. Each step of `GET /harvest-plan` names a lot id and a quantity, and the dashboard's execute button fires that lot id straight back into `POST /transactions`. Reproducing a recommendation by hand in a broker interface usually falls back to FIFO — so you'd realize a different taxable result from the one that was valued. The advice would be correct and the outcome still wrong.

**What the engine enforces.** Candidate lots are always filtered to *open* lots of that symbol **in that wallet** — sells are broker-scoped, so a lot at Fidelity can't satisfy a sale at Schwab. Specific ids are validated on all four counts (exists, right symbol, right wallet, still open) and rejected with a 400 rather than silently falling back. An oversell is refused before anything is written. And ordering always reads the real `opened_date`, never the wash-tacked holding-period date — tacking changes how a loss is *classified*, not which shares are physically oldest.

### Wash sales are enforced, not flagged

**The rule, plainly.** If you sell a security at a loss and buy the same — or a "substantially identical" — security within **30 days before or 30 days after** that sale, the IRS disallows the loss (IRC §1091). It is a 61-day window centered on the sale day, and buying *first* and selling *second* triggers it just as buying after does. The loss is not deducted this year, so the harvest you were counting on produces nothing.

The loss is usually deferred rather than destroyed. The disallowed amount is added to the **cost basis of the replacement shares**, and the holding period of the shares you sold is **tacked onto** them — so you recover the deduction when you eventually sell the replacement, and you keep credit for the time already served toward long-term treatment. The exceptions are the ones that hurt: if the replacement is bought in an IRA or 401k there is no taxable basis to adjust and the loss is **gone for good** (Rev. Rul. 2008-5).

Three details that catch people. The rule follows the **taxpayer**, not the account — your spouse's account and your IRA count, and no broker can see them, so no broker's 1099-B is complete. **Any** purchase counts, including ones you did not consciously make: a dividend reinvestment, an automated recurring buy, an option exercise. And it applies **only to losses** — selling at a gain and rebuying immediately is fine.

**How HarvestIQ handles it.** Detection runs *before* the sale — replacement shares are computed from open lots inside the ±30-day window, net of shares that already absorbed a previous disallowed loss (a share can only block one harvest, never two). Fully liquidating a fresh buy is correctly **not** flagged.

Enforcement runs at transaction time, in both directions. The disallowed loss doesn't vanish: it attaches to the replacement **shares** — basis up, holding period tacked back. Partial washes **split the replacement lot**, because Treas. Reg. §1.1091-1 matches share-by-share: sell 40 and rebuy 100 and the loss attaches to 40 of those shares, not pro-rata across all 100.

**Across accounts**, which is the gap nothing mainstream covers. The wash-sale rule applies to the *taxpayer*; each broker only sees its own account. Register watch-only external accounts (a spouse's taxable account, an IRA, a 401k) and their purchases count toward the window. When the replacement lands in a retirement account the loss is **permanently destroyed** rather than deferred — there's no basis to adjust (Rev. Rul. 2008-5) — and HarvestIQ says so.

Crypto is exempt (property, not a security, per Notice 2014-21).

### Warnings for the traps that actually bite people

- **DRIP guard** — automatic dividend reinvestment is a purchase the wash-sale rule counts, and it's the single most common way people lose a harvested loss. If a distribution falls inside the ±30-day window, you get told to turn reinvestment off *before* selling.
- **Seasoning deadline** — a short-term loss that crosses its one-year anniversary becomes long-term and is usually worth less. HarvestIQ prices the difference and only warns when waiting actually costs money. (It often doesn't: with only short-term gains on the books, a long-term loss cross-nets into them at the short rate anyway.)
- **Harvest capacity** — how much harvestable loss is *left*, and how far each position must fall before it offers anything. Repeated harvesting ratchets basis down until a portfolio is ossified: still holding losses on paper, but nothing takeable without a serious drawdown.

### Per-wallet cost basis

Universal pooled basis for crypto is dead. Rev. Proc. 2024-28 and Treas. Reg. §1.1012-1(j) made wallet-by-wallet basis **mandatory** for tax years beginning after 2024-12-31. HarvestIQ enforces it end-to-end: a sale can only draw on lots actually held in that wallet, and `POST /transfers` moves lots between wallets carrying basis and dates without realizing anything.

---

## Features

- **Ordered harvest plan** — sequenced, sized, with saves-now / costs-later / net on every step and two-click execute buttons.
- **Real netting waterfall** — ST/LT character, cross-netting, $3,000 cap, character-preserving carryforwards.
- **Wash-sale enforcement** — basis adjustment, holding-period tacking, lot splitting, spent-share tracking, cross-account and retirement-account scope.
- **Harvest capacity** — the remaining pool of unrealized loss, plus per-position drawdown-to-harvest.
- **Seasoning + DRIP warnings** — priced, not generic.
- **Multi-wallet** — accounts registry, basis-preserving transfers, per-wallet basis (Rev. Proc. 2024-28).
- **[Lot selection](#which-lot-you-sell-is-the-whole-trade)** — FIFO, HIFO, or specific identification by lot id on any sell, wallet-scoped and validated.
- **Tax-lot tracking** — every purchase is its own lot with fee-inclusive cost basis and an oversell guard.
- **Live market data** — prices stream over a Finnhub websocket; the dashboard updates itself over its own websocket.
- **Price history** — 90-day daily bars from Yahoo Finance, Redis-cached.
- **AI explanations** — natural-language walkthrough of the plan via the Groq API.
- **Dashboard** — single-page UI: plan hero panel, capacity tiles, holdings, opportunities with wash/DRIP/seasoning badges, realized-gains log with washed amounts, settings, wallets and transfers.

---

## Tech stack

- **Backend:** FastAPI, SQLAlchemy, PostgreSQL
- **Async:** Redis (broker + cache + pub/sub) and Celery
- **Migrations:** Alembic
- **Market data:** Finnhub websocket for live prices and spot quotes; yfinance for historical daily bars only (Finnhub's candle endpoints are premium). Every outbound call goes through `app/prices.py`.
- **AI:** Groq API (`openai/gpt-oss-20b`)
- **Frontend:** vanilla HTML/JS + Chart.js (served by FastAPI)
- **Tests:** pytest + FastAPI TestClient (in-memory SQLite)

---

## How it works

```
transactions ─▶ validation ─▶ security master
     │
     ▼
  tax lots ─▶ FIFO / HIFO / specific-lot consumption
     │              │
     │              ▼
     │        wash-sale ENFORCEMENT (basis + tacking + lot split,
     │        across internal, external and retirement accounts)
     │              │
     ▼              ▼
 live prices ─▶ realized gains (tax-reportable, washes applied)
     │
     ▼
 unrealized ─▶ netting waterfall ─▶ per-lot opportunities
                                          │
                     DRIP + seasoning ────┤
                                          ▼
                              sequenced harvest PLAN
                        (sized to the useful-loss budget)
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
            harvest capacity      AI explanation           dashboard
           (what's left to
            harvest, ever)
```

Import direction is one-way and must stay that way:
`main.py → celery_app.py → harvest.py → model.py`

---

## Setup

**Prerequisites:** Python 3.9+, PostgreSQL and Redis running locally, a free [Finnhub API key](https://finnhub.io/register), and a free [Groq API key](https://console.groq.com).

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

# 5. Add your API keys
cat > .env <<'ENV'
GROQ_API_KEY=your_key_here
FINNHUB_API_KEY=your_key_here
ENV

# 6. Create the schema
alembic upgrade head

# 7. Run the stack — web + worker + streamer in one terminal
honcho start

# 8. Seed the tax cache once
curl -X POST localhost:8000/tax-analysis/run
```

`honcho start` reads the `Procfile`. To run the processes separately instead:

```bash
uvicorn app.main:app --reload
celery -A app.celery_app:celery_app worker --loglevel=info --pool=solo
python -m app.price_stream          # live Finnhub price feed
```

> On macOS the Celery worker must run with `--pool=solo` (fork-safety segfaults otherwise).

Then open the dashboard at **http://127.0.0.1:8000/dashboard** and the interactive API docs at **http://127.0.0.1:8000/docs**.

### API conventions

- Writes return **201** with the created resource; endpoints that enqueue background work return **202** with a job id and a `Location` header pointing at `/jobs/{id}`.
- Money and quantities cross the wire as **strings**, so no client can silently round them through a float.
- Errors use FastAPI's `{"detail": ...}` shape throughout.

### Load demo data (optional)

With the server running, in a second terminal:

```bash
python3 seed.py
```

This creates a diversified sample portfolio (equities + crypto across several sectors) and pulls live prices, so the dashboard has realistic data to show.

---

## Key API endpoints

### Tax analysis

| Endpoint | Purpose |
| --- | --- |
| `GET /harvest-plan` | **The main one.** Ordered, sized, sequentially-priced plan |
| `GET /harvest-opportunities` | Per-lot view with wash / DRIP / seasoning flags |
| `GET /harvest-capacity` | Harvestable loss remaining + per-position drawdown-to-harvest |
| `GET /wash-sales` | Enforced wash-sale records with disallowed amounts |
| `GET` / `PUT /tax-settings` | Carryforwards, expected future rate, carryforward dial |
| `GET /replacement-suggestions` | Wash-sale-safe alternatives (still a sector-based stub) |
| `POST /harvest-explanations` | Enqueue an AI-written harvest summary (202 + job id) |
| `POST /tax-analysis/run` · `GET /tax-analysis` | Recompute / serve the cached analysis |

### Transactions, lots and portfolio

| Endpoint | Purpose |
| --- | --- |
| `POST /transactions` | Record a buy/sell (`lot_selection`: `fifo` \| `hifo` \| `specific`) |
| `POST /transactions/market` | Buy/sell at the current live price |
| `GET /tax-lots` | All tax lots, including tacked `holding_period_date` |
| `GET /realized-gains` | Tax-reportable gain/loss + `disallowed_wash_loss` |
| `GET /portfolio` · `GET /unrealized-gains` | Holdings and open-lot P&L |
| `GET /portfolio-summary` | Headline totals, plan savings, capacity |
| `WS /ws/prices` | Live summary pushed on each price change |

### Wallets, external accounts and securities

| Endpoint | Purpose |
| --- | --- |
| `POST` / `GET /accounts` | Wallet / brokerage / exchange registry |
| `POST` / `GET /transfers` | Inter-wallet moves that carry basis and realize nothing |
| `POST` / `GET /external-accounts` (+ `/activities`) | Watch-only accounts for cross-account wash sales |
| `POST` / `GET /securities` · `POST /securities/{symbol}/price` | Security master and prices |
| `PUT /securities/{symbol}/dividends` | DRIP flag and next dividend date |
| `GET /price-history/{symbol}` | 90-day price history for charts (Redis-cached) |
| `GET /jobs/{job_id}` | Poll a background job |

---

## Running the tests

```bash
python3 -m pytest -v
```

63 tests. They cover the netting waterfall and its edge cases (the $3,000 cap, net-loss years where an extra harvest saves nothing, character preservation), wash-sale enforcement (basis adjustment, holding-period tacking, lot splitting on partial washes, spent replacement shares, cross-account and retirement-account cases), plan sequencing and sell sizing across the carryforward dial, harvest capacity, seasoning deadlines, per-wallet basis through transfers, and the crypto exemption.

`tests/test_ws_prices.py` needs a live Redis; `tests/test_engine.py` runs standalone on in-memory SQLite.

---

## Known limitations

The honest split: **the harvesting logic is rigorous, the tax rate model is a stated simplification.** The machinery for producing a defensible answer is there and it's fed a rate you type in. That rate can be wrong by a factor of two or three in either direction, and everything downstream inherits the error. Treat `estimated_tax_savings` as *directionally* sound and the dollar figure as indicative.

**Rate modelling (the big one):**

- **Flat rates, no brackets.** You supply a short- and long-term rate. There's no bracket stacking and no 0% / 15% / 20% breakpoints. If your income puts you in the **0% long-term band**, HarvestIQ will tell you a long-term harvest saves 15% when it saves nothing — and it's worse than nothing, because you've ratcheted basis down against a future taxable gain.
- **No NIIT.** The 3.8% surtax isn't modelled, and its treatment of capital losses is unintuitive (net losses count against net investment income only up to the same $3,000).
- **No §1(h) rate groups.** Collectibles at 28% (including physical-metal ETFs like GLD and SLV) and unrecaptured §1250 at 25% are treated as ordinary long-term.
- **No AMT, no state tax.** Pennsylvania in particular has no carryforward, no $3,000 offset, no ST/LT distinction — and the federal wash-sale rules don't apply for PA personal income tax. New Jersey has no carryforward either.
- **No cliff modelling.** IRMAA, ACA subsidy cliffs and Social Security's provisional-income phase-in all interact with capital losses and none are represented.

**Scope and mechanics:**

- **Not multi-user** — no authentication; all data is shared. This is the blocker to being genuinely real.
- **Carryforward has no present value or mortality discount.** It's reported honestly as separate from current-year savings, but a dollar banked for a user with no foreseeable gains is worth much less than face — and unused carryforwards expire at death (Rev. Rul. 74-175).
- **"Substantially identical" is not solved.** Replacement suggestions are same-sector-different-symbol, with no correlation or tracking-error screening. Nobody has solved this; the IRS has never defined the term.
- **The mandatory-use trap isn't caught.** §1211(b) requires the loss to be used to the maximum extent in the current year — there's no election to defer. A taxpayer with no gains and income below the standard deduction destroys $3,000 of carryforward for nothing, and HarvestIQ will still recommend the harvest.
- **Some hidden costs aren't modelled:** the §852(b)(4)(A) six-month rule (a capital gain distribution recharacterizes a short-term loss as long-term), the 61-day qualified-dividend rule under §246(c), and return-of-capital distributions reducing basis under §301(c)(2).
- **Special regimes invalidate the engine entirely** for some users: a §475(f) mark-to-market trader is exempt from §1091 and has no $3,000 cap; §1092 straddles and §1259 constructive sales aren't handled.
- **Crypto:** wallet-by-wallet basis is enforced, but the pre-2025 safe-harbor allocation isn't modelled, and crypto ETPs (which *are* securities and *do* get §1091) aren't yet distinguished from spot crypto in the schema.
- **Transfer fees** aren't added to basis; currency is stored but unused in P&L.
- Tests run on SQLite rather than Postgres.

---

## Disclaimer

HarvestIQ provides general information for educational purposes only. It is **not** personalized tax, legal, or financial advice. Consult a qualified professional before making investment or tax decisions.
