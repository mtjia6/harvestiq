# HarvestIQ — System Status & Design (as of 2026-08-09)

This document is a snapshot of where the project is, written so that "me in 5
months" can rebuild the mental model without re-reading every file. It focuses on
**system design and data flow** — what runs where, and how data moves from one
piece to the next — plus, since the 2026-08-08 overhaul, the **tax engine's
actual rules**, because the domain logic is no longer a placeholder.

---

## 1. What HarvestIQ is (one paragraph)

An AI-assisted tax-loss-harvesting engine. It tracks holdings at the **tax-lot**
level across **multiple wallets/accounts**, values them against **live market
prices**, and produces an ordered, executable **harvest plan**: which lots to
sell, in what order, sized to what actually helps, with three honest numbers per
step (saves now / costs later via lowered basis / net). It applies the real IRS
netting waterfall (ST/LT, $3,000 ordinary cap, carryforwards), **enforces** the
wash-sale rule (basis adjustment + holding-period tacking, across watch-only
external accounts including the IRA permanent-destruction case), guards against
DRIP-triggered washes, and explains the plan in natural language.

---

## 2. The processes (this is the key mental model)

Unchanged from the streaming migration — the system is several OS processes that
do NOT share memory and communicate only through **Postgres** (durable) and
**Redis** (cache + pub/sub). See §5 for the flows.

| Process | Command | Job |
|---|---|---|
| **Web** | `uvicorn app.main:app --reload` | FastAPI. HTTP endpoints + browser websocket. |
| **Streamer** | `python -m app.price_stream` | Finnhub websocket. Owns in-memory `latest` prices. |
| **Worker** | `celery -A app.celery_app:celery_app worker --pool=solo` | Background jobs (tax recompute, AI explanation). `--pool=solo` REQUIRED on macOS. |
| **Redis** | (service) | Pub/sub `"prices"` + caches (`tax_analysis`). |
| **Postgres** | (service, db `harvestiq`) | All durable state. |

**One-terminal startup:** `honcho start` (see §10) runs web + worker + streamer
together from the `Procfile`. Redis/Postgres stay as brew services.

### The two websockets (do not confuse them)
- **A: Finnhub → Streamer** — how prices arrive.
- **B: Web → Browser** (`/ws/prices`) — how the computed summary reaches the screen.

---

## 3. File / responsibility map

Import direction is **one-way** and must stay that way:
`main.py → celery_app.py → harvest.py → model.py`, plus `main.py → harvest.py`
and `price_stream.py → celery_app.py`.

- **`app/main.py`** — FastAPI endpoints (web layer) + `process_transaction`
  (lot consumption with FIFO/HIFO/specific selection, wash-sale hooks) +
  `create_transfer` (inter-wallet moves). Enqueues jobs, serves reads.
- **`app/harvest.py`** — pure business logic, now the heart of the product:
  - *Netting engine:* `net_capital_position`, `tax_outcome`, `tax_owed`,
    `marginal_harvest_value` (→ savings, carryforward), `deferral_math`
    (→ future cost, net benefit), `holding_term` (calendar >1yr rule),
    `holding_period_anniversary` (last short-term day),
    `effective_holding_date` (wash-tacked date).
  - *Analysis:* `build_harvest_opportunities` (per-lot view, wash + DRIP +
    seasoning flags), `build_harvest_plan` (sequenced, sized, scored plan;
    `_size_harvest` for the sell size), `build_harvest_capacity` (the
    remaining pool), `build_portfolio_summary`.
  - *Wash-sale enforcement:* `_transfer_disallowed_loss` (lot splitting),
    `apply_wash_sale_on_sell/on_buy`, `_disallow_against_external`,
    `apply_external_wash_on_sell/on_activity`.
  - *Settings readers:* `get_carryforwards`, `get_expected_future_rate`,
    `get_carryforward_discount`, `get_effective_net_position`.
- **`app/celery_app.py`** — Celery config + tasks. `run_tax_analysis` computes
  opportunities AND the plan, caches both under Redis `tax_analysis`.
- **`app/model.py`** — `TransactionDB`, `SecurityDB` (+drip fields), `TaxLotDB`
  (+holding_period_date, wash_replacement_used), `RealizedGainDB`
  (+disallowed_wash_loss, wash_sale_note), `TaxSettingsDB`, `AccountDB`,
  `TransferDB`, `ExternalAccountDB`, `ExternalActivityDB`.
- **`static/index.html`** — the dashboard (redesigned 2026-08-08: warm paper
  light theme, Fraunces/Inter). Plan hero panel with two-click execute buttons.

---

## 4. THE TAX ENGINE — rules actually implemented (the 2026-08-08 overhaul)

1. **Netting waterfall** — ST nets vs ST, LT vs LT, cross-netting, surviving
   losses keep character; $3,000/yr ordinary-income cap; excess = carryforward.
   `marginal_harvest_value` prices every loss against the *effective* position
   (realized YTD − prior-year carryforwards from `tax_settings`), returning
   (current-year savings, carryforward generated) — never conflated.
2. **Honest deferral math** — harvesting lowers replacement basis, so each
   opportunity/plan step carries `future_tax_cost` (loss × expected future LT
   rate, a user setting) and `net_benefit`. Same-bracket case nets to $0.
3. **The plan** (`GET /harvest-plan`) — greedy sequential: re-prices remaining
   candidates each round against the running position. Score =
   `savings + (future_rate × carryforward_discount) × carryforward`.
   `carryforward_discount` (0..1, tax_settings) is the strategic dial, and it
   moves the score AND the sell size **continuously** (`_size_harvest`):
   `sized = budget + (full_loss − budget) × discount`, where budget is the
   remaining useful loss (`max(net + 3000, 0)`). 0 = no gains coming → sized
   to the budget, never burns a big lot for nothing; 1 = big gain coming →
   banks whole lots; 0.3 → budget plus 30% of the excess. Sizing keys off the
   *discount*, not `carryforward_price` (= `future_rate × discount`, a tax
   rate) — using the latter would cap dial 1 at ~15% of the excess. Ties
   prefer less loss spent, then whole-lot sells. Output: steps
   (quantity_to_sell/partial), carryforward_only, excluded_wash_sale,
   three-number totals.
4. **Wash sales — detected AND enforced (IRC §1091)**
   - *Detection (before sale):* replacement shares = open window lots (≤30
     days) net of `wash_replacement_used`, excluding the candidate lot, PLUS
     watch-only external-account buys. Full liquidation of a fresh buy is NOT
     flagged. `safe_to_rebuy_after` on every opportunity. Retirement-account
     replacements set `wash_sale_permanent` (Rev. Rul. 2008-5).
   - *Enforcement (at transaction time, both directions):* loss disallowed
     pro-rata; partial washes SPLIT the replacement lot (washed shares get
     adjusted basis + tacked `holding_period_date`; clean shares untouched;
     carved shares marked spent so nothing washes twice). `gain_loss` on
     realized records is the TAX-reportable figure; `disallowed_wash_loss` and
     `wash_sale_note` carry the rest. External-account washes disallow with a
     note (taxable: basis belongs to that account; retirement: destroyed).
   - *Crypto is exempt* (property, not a security).
5. **Lot selection** — sells take `lot_selection: fifo | hifo | specific`
   (+`lot_ids`), broker-scoped. `opened_date` is always the REAL purchase date
   (FIFO + wash windows need it); term classification reads
   `effective_holding_date` (calendar more-than-one-year rule).
6. **DRIP guard** — per-security `drip_enabled` + `next_dividend_date`; warns
   (doesn't veto) when a dividend lands inside ±30d of a harvest, both the
   "turn it off first" and "already reinvested" variants.
7. **Multi-wallet** — `accounts` registry (auto-created from broker names);
   `POST /transfers` moves lots between wallets carrying basis + dates
   (splitting on partial moves), realizing nothing. Per-wallet cost basis
   (Rev. Proc. 2024-28) enforced end-to-end.
8. **Harvest capacity** (`GET /harvest-capacity`) — the remaining POOL of
   unrealized loss, deliberately ignoring the useful-loss budget and the
   dial. Harvesting spends the pool permanently (replacement basis resets
   down); only new contributions refill it, and nothing else in the engine
   tracked that. Reports total / available / wash-blocked (blocked loss is
   unavailable today, **not** gone, so it counts in the total), capacity as
   a % of market value, and per position `drawdown_to_harvest` — the
   fraction price must FALL before that position's *highest-basis* lot goes
   underwater. 0 = harvestable now; 0.30 = ossified, needs a 30% drop.
   Headline scalars ride on `build_portfolio_summary` (lots/securities
   passed through, so no extra queries on the websocket push).
9. **Seasoning deadline** — a short-term loss crossing its anniversary
   becomes long-term and is usually worth less. Priced, not just flagged:
   the same loss is re-run as if already long-term and the difference is
   `seasoning_savings_at_risk`; `seasoning_risk` only fires when that is
   positive. Non-obvious: with only ST gains on the books a long-term loss
   cross-nets into them at the short rate, so character is worth **zero**
   and the flag correctly stays down — it needs LT gain competing for the
   loss. Deadlines read `effective_holding_date`, so wash-tacked lots report
   their real anniversary. Fields ride on opportunities, plan steps, and
   `carryforward_only` (most important there — those are the lots being
   deferred into next year). Window: `SEASONING_WARNING_DAYS = 30`.

---

## 5. DATA FLOW — unchanged mechanics, richer payloads

Live price flow is unchanged from the streaming migration: Finnhub → streamer
`latest` dict → 1s flush to Postgres + Redis `"updated"` publish → `/ws/prices`
rebuilds and pushes the summary (cold-start push on connect; change-gated
silence when quiet; browser reconnects after 2s). Two cadences still apply:
prices ~1s, tax recompute every 30s via the streamer's `recompute_tax_loop`.

What changed: `run_tax_analysis` now caches `{opportunities, plan, generated_at}`,
and `build_portfolio_summary` takes the plan so the headline savings number is
the PLAN total (sequentially priced — per-lot figures don't sum), plus
`harvestable_net_benefit`, carryforward fields, and the corrected
`useful_loss_budget` (`max(effective_net + 3000, 0)`).

---

## 6. Endpoint inventory (current)

Tax analysis: `GET /harvest-opportunities` (per-lot, live), `GET /harvest-plan`
(sequenced, live), `GET /harvest-capacity` (remaining pool + per-position
drawdown-to-harvest), `POST /tax-analysis/run` + `GET /tax-analysis` (cache),
`GET/PUT /tax-settings` (carryforwards, expected_future_lt_rate,
carryforward_discount), `GET /wash-sales` (enforced records),
`GET /replacement-suggestions` (still a sector-based stub),
`POST /harvest-explanations` + `GET /jobs/{id}`.

Transactions & lots: `POST /transactions` (+lot_selection/lot_ids),
`POST /transactions/market`, `GET /transactions`, `GET /tax-lots`
(+holding_period_date), `GET /realized-gains` (+disallowed_wash_loss,
wash_sale_note), `GET /portfolio`, `GET /portfolio-summary`, `WS /ws/prices`,
`GET /unrealized-gains`.

Wallets: `POST/GET /accounts`, `POST/GET /transfers`.
External (watch-only): `POST/GET /external-accounts`,
`POST/GET /external-accounts/{id}/activities`.
Securities: `POST/GET /securities`, `POST /securities/{symbol}/price`,
`PUT /securities/{symbol}/dividends` (DRIP), `GET /price-history/{symbol}`.

---

## 7. Migrations

Alembic chain: `9142459bba1d` (initial) → `46e6748cbe94` (broker on lots) →
`a1c4f2b9e07d` (history_symbol) → `b7e3d1a52c48` (tax_settings) →
`c9f4e8b31d75` (wash basis columns); readable ids from there:
`0006_drip_dividends` → `0007_external_accounts` → `0008_deferral_settings` →
`0009_holding_period_date` → `0010_carryforward_discount` →
`0011_wallets_and_transfers`. Run `alembic upgrade head` after pulling.

---

## 8. STATUS — what's DONE (2026-08-08 session)

- [x] Real netting waterfall + carryforwards + $3k cap (replaced flat-rate math
      that overvalued losses in net-loss years).
- [x] Sequenced harvest plan with explicit carryforward pricing + partial sizing.
- [x] Wash-sale enforcement both directions, lot splitting, holding-date
      tacking, spent-share tracking (four audit findings fixed, incl. the
      double-aging and basis-smearing lot bugs).
- [x] HIFO/specific-lot sells; calendar holding rule.
- [x] DRIP guard; watch-only external accounts (cross-account washes, IRA
      permanent destruction); honest deferral math (saves/costs/net).
- [x] Multi-wallet: accounts registry + basis-preserving transfers.
- [x] Dashboard rebuilt around the plan (execute buttons fire specific-lot
      market sells), then fully redesigned (warm paper theme, Fraunces/Inter).
- [x] Procfile + honcho for one-terminal startup.
- [x] Tests: 57 passing (`tests/test_engine.py` on in-memory SQLite +
      `tests/test_ws_prices.py`, which needs a live Redis).

## 8b. STATUS — the "tax alpha decay" thread (2026-08-09 session)

Prompted by a J.P. Morgan Private Bank piece arguing that harvesting decays
as basis ratchets down ("tax alpha decay" / ossification) and that regular
cash contributions of ~10–20% of account value per year are the fix.

**What the article actually contains is one strategy** — add new cash to
create fresh high-basis lots. Decay is the diagnosis, not the strategy;
contribution frequency (they found monthly/quarterly/annual barely differ)
and lump-sum-vs-gradual are parameters of it. Their headline figures (1–2%
of tax savings a year, ~80% of it in the first five years) are model output
under their own assumptions and are **deliberately not quoted anywhere in
the product** — published lifetime estimates span ~15bp to ~110bp depending
almost entirely on whether the study debits the future tax created. Given
the SEC actions against Wealthfront and Betterment were both about
overstating what harvesting did, borrowing a manager's point estimate would
be exactly the wrong move.

**Scope conclusion: mostly already handled.** The engine's real exposure to
decay was that it treated unrealized loss as free — and migration 0010's
partial sizing already fixed that. What was left was two small things and
one bug, all now done:

- [x] **Dial sizing was a switch, not a dial.** `build_harvest_plan` branched
      on `carryforward_price > 0`, so any nonzero `carryforward_discount`
      produced identical whole-lot sells; only the score varied continuously.
      Replaced with `_size_harvest` (§4.3). Existing dial tests only covered
      0 and 1, which is why it survived.
- [x] **Harvest capacity metric** (§4.8) — the pool, the wash-blocked split,
      `drawdown_to_harvest`, a fifth dashboard stat tile ("Harvest Capacity
      Left" + `X% of portfolio · $Y wash-blocked`) and a line naming the most
      ossified positions. Overview grid moved from `repeat(4, 1fr)` to
      `repeat(auto-fit, minmax(180px, 1fr))` to take the fifth tile.
- [x] **Seasoning deadline warning** (§4.9) — `Nd TO LONG-TERM` caution badge
      on plan steps, opportunity rows and carryforward-only entries.
- [x] Tests: 63 passing in `test_engine.py` (+6: 2 dial, 4 capacity, 4
      seasoning, minus overlap in counting — see the file).

**Audit item closed, not fixed:** the audit doc recommended ranking the plan
by savings *per dollar of loss consumed* rather than absolute score. Checked
against the code — the tie-break key `(score, -sized_loss, whole_lot)`
already prefers spending less loss, and with partial sizing in place the
plan never takes more than it needs anyway. No change made.

**Deliberately NOT built: the contribution planner.** Sizing a recommended
contribution (JPM's 10–20%) and routing it around open `safe_to_rebuy_after`
windows is a real feature nobody ships, and HarvestIQ has the cross-account
data to do it properly. It is parked because the advice half is where an
asset manager's incentive and a user's diverge — contributions don't create
value, they restore harvest *capacity*. The diagnostic half (§4.8) is the
honest half and stands alone. If it is ever built, the framing must be "if
you have the cash anyway, sequence it well", never "contribute in order to
harvest".

**Crypto caveat on all of the above:** the decay story assumes §1091 applies.
It doesn't to crypto (property, per Notice 2014-21), so a harvested position
can be rebought instantly — crypto ossifies through appreciation alone, with
none of the wash-sale-driven drift, and arguably needs its own capacity math.
Crypto ETPs are securities and behave like equities here.

## 9. TODO — what's left

- Replacement suggestions are still same-sector-different-symbol; no tracking
  error / "substantially identical" screening.
- Flat configurable rates: no brackets, NIIT, or 0% LT band.
- yfinance auto-populate of `next_dividend_date` (manual today).
- Broker/exchange linking to auto-feed external activities and transactions.
- Transfer fees not added to basis; pre-2025 crypto safe-harbor allocation not
  modeled; currency stored but unused in P&L.
- Auth / multi-user — still the blocker to being genuinely "real".
- Tests run on SQLite, not Postgres. Groq → Claude swap still pending.
- Dark mode for the redesigned dashboard (palette has a validated dark set).
- Capacity is a point-in-time number; no history, so the decay *curve*
  (snapshot capacity over time and plot it) isn't there yet. Cheapest next
  step on this thread if it's ever picked up again.
- Contribution planner — see §8b for why it's parked rather than pending.

---

## 10. How to run (local dev)

```bash
source venv/bin/activate            # Python 3.9; system Python lacks deps
alembic upgrade head                # after pulling schema changes

brew services start redis postgresql   # once, as background services
honcho start                        # web + worker + streamer, one terminal
# (or the three commands from §2 in separate terminals)

curl -X POST localhost:8000/tax-analysis/run   # seed the tax cache once
# open http://localhost:8000/dashboard          (API explorer: /docs)
```

Gotchas: activate the venv first; restart worker/uvicorn after editing
task/endpoint code (Ctrl-C honcho and rerun does both); Celery can't serialize
Decimal (rates cross as strings); `python3 -m pytest -q` after logic changes —
the ws tests need redis running.

---

## 11. Concept notes (for future-me)

The streaming/process notes from the previous snapshot still hold (threads vs
processes, durable vs transient stores, push vs poll, inversion of control).
New domain concepts live in `tax_harvesting_notes.md`; the short version:
carryforward = loss saved for future years (offsets gains unlimited, ordinary
income $3k/yr, keeps ST/LT character); wash-sale tacking = denied loss rolls
into replacement basis AND the replacement inherits the sold shares' age (cuts
both ways — earlier long-term on gains, but downgrades a future loss to
long-term); per-wallet basis = a sale can only draw on lots actually held in
that wallet, so transfers must carry basis with them.
