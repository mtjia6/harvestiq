# HarvestIQ — System Status & Design (as of 2026-08-02)

This document is a snapshot of where the project is, written so that "me in 5
months" can rebuild the mental model without re-reading every file. It focuses on
**system design and data flow** — what runs where, and how data moves from one
piece to the next. Read the "Data flow" sections top-to-bottom; they're the point.

---

## 1. What HarvestIQ is (one paragraph)

An AI-assisted tax-loss-harvesting engine. It tracks holdings at the **tax-lot**
level, values them against **live market prices**, finds losing positions worth
selling to cut taxes, checks each against the IRS wash-sale rule, and explains the
plan in natural language. The headline feature added most recently: **live pricing**
— prices stream in real time from Finnhub and the dashboard updates itself.

---

## 2. The processes (this is the key mental model)

The system is **not one program**. It's several OS processes that **do not share
memory**. They can only communicate through two shared stores: **Postgres** (durable
state) and **Redis** (cache + pub/sub messaging). Every time two processes need to
exchange something, it goes through one of those two. Burn this in — most confusion
comes from forgetting that these are separate memory spaces.

| Process | Command | Job |
|---|---|---|
| **Web** | `uvicorn app.main:app --reload` | FastAPI. Serves HTTP endpoints + the browser websocket. |
| **Streamer** | `python -m app.price_stream` | Holds the Finnhub websocket. Owns the in-memory `latest` price dict. |
| **Worker** | `celery -A app.celery_app:celery_app worker --pool=solo` | Runs background jobs (tax recompute, AI explanation). `--pool=solo` is REQUIRED on macOS (fork-safety segfaults otherwise). |
| **Redis** | (service) | Pub/sub channel `"prices"` + caches (`tax_analysis`, `prices_last_refreshed`). |
| **Postgres** | (service, db `harvestiq`) | Durable state: `securities.latest_price`, tax lots, transactions, realized gains. |
| **Browser** | opens `/dashboard` | Renders. Talks HTTP + one websocket to the Web process. |

### The two websockets (do not confuse them)
- **Websocket A: Finnhub → Streamer.** How live prices *arrive* into our system.
- **Websocket B: Web process → Browser** (`/ws/prices`). How the computed summary
  *reaches the screen*.

### The two async patterns in the codebase
- **Compute-and-cache:** an expensive job computes and writes a result to Redis under
  a stable key; cheap reads serve that cache; a trigger recomputes. Used for the tax
  analysis (`run_tax_analysis` writes `tax_analysis`; `GET /tax-analysis` reads it).
- **Read-live-per-request:** an endpoint reads current DB state on every call. Used
  for P&L (`/portfolio-summary`, `/unrealized-gains`).

---

## 3. File / responsibility map

Import direction is **one-way** and must stay that way:
`main.py → celery_app.py → harvest.py → model.py`, plus `main.py → harvest.py` and
`price_stream.py → celery_app.py`. Nothing points back "up." Shared logic lives in
`harvest.py` so both the web app and workers can use it without a circular import.

- **`app/main.py`** — FastAPI endpoints (the web layer). Enqueues jobs, serves reads,
  hosts the browser websocket. Does NOT contain business logic.
- **`app/price_stream.py`** — the standalone streamer. Finnhub websocket + two
  background threads. Imports `SessionLocal`, `SecurityDB`, and from `celery_app`
  (`run_tax_analysis`, `redis_client`). NOT a Celery task — a long-running process.
- **`app/celery_app.py`** — Celery config + task definitions (`run_tax_analysis`,
  `generate_harvest_explanation`) + the LLM prompt builder. Plumbing only.
- **`app/harvest.py`** — pure business logic. `build_harvest_opportunities`,
  `build_portfolio_summary`, `get_ytd_realized_totals`, `marginal_tax_savings`,
  `tax_owed`. No FastAPI, no Celery, no Redis imports.
- **`app/model.py`** — SQLAlchemy models (`TransactionDB`, `SecurityDB`, `TaxLotDB`,
  `RealizedGainDB`).
- **`app/database.py`** — engine + `SessionLocal` + `get_db`.
- **`static/index.html`** — the vanilla HTML/JS dashboard (served at `/dashboard`).

---

## 4. DATA FLOW — live price to the screen (the main event)

Follow a single price from the exchange to the browser. Each numbered step is a
different place the data lives or moves.

```
Finnhub ──WS A──▶ Streamer ──(writes)──▶ Postgres ──(reads)──▶ Web /ws/prices ──WS B──▶ Browser
                     │                                              ▲
                     └──(publishes "updated")──▶ Redis pub/sub ─────┘  (this is only a TRIGGER,
                                                                        it carries NO price data)
```

**Step 1 — Ingest (Streamer, Websocket A).**
`price_stream.on_open` subscribes to every symbol in `SecurityDB`. Finnhub then
pushes trades continuously. `price_stream.on_message` fires per trade and writes
`latest[symbol] = price` into an **in-memory dict**. This runs at the FEED rate —
hundreds/sec. No DB, no Redis, pure memory → it never falls behind the feed.
(`run_forever()` inside the library IS the event loop that calls these callbacks.)

**Step 2 — Flush (Streamer, background thread, once/sec).**
`price_stream.flush_prices` runs on its own daemon thread, looping `sleep(1)`. Each
second it:
  - (a) opens a DB session, writes each changed `latest_price` into **Postgres**, one
    batched `commit()`. ← THIS is the durable "injection" of the new price.
  - (b) snapshots `latest`; **if it changed** since last time, publishes the string
    `"updated"` to the Redis `"prices"` channel (change-gated — silent when nothing
    moved). ← THIS is the notification, not the data.
  Ordering matters: **DB write first, publish second**, so by the time anyone reacts
  to the nudge, Postgres already holds the fresh price.

  *Why a thread and not a Celery task:* the flush must run alongside `run_forever()`
  (which blocks forever), and it must share the `latest` dict living in this process's
  memory. A Celery worker is a separate process and couldn't see `latest`. Threads
  share memory; separate processes don't.

  *Why 1/sec and not per-tick:* committing per tick (hundreds/sec) would make the DB
  the bottleneck and the consumer would fall behind the feed (backpressure). This is
  a **write-back cache**: fast in-memory writes, throttled persistence.

**Step 3 — Bridge (Redis pub/sub).**
Streamer and Web are separate processes → can't call each other. The `"updated"`
message sits in the Redis `"prices"` channel; Redis delivers it to whoever is
subscribed *right now*. This is the only path for the nudge to cross the process
boundary. Pub/sub is **transient**: not stored, no history — if nobody's listening it
vanishes.

**Step 4 — Push (Web process, Websocket B = `/ws/prices`).**
When the browser loads, `index.html` runs `new WebSocket('/ws/prices')`. The FastAPI
handler `main.ws_prices`:
  - `accept()`s the socket,
  - subscribes to the Redis `"prices"` channel via **`redis.asyncio`** (async client,
    so the pub/sub loop doesn't block the event loop),
  - pushes the current summary immediately (COLD START — see §6),
  - then loops `async for message in pubsub.listen()`, and on each `"updated"`,
    **builds a fresh summary and pushes it** down the socket.

**Step 5 — Build the summary (inside the push; reads Postgres + Redis).**
Each push calls `main._summary_json()`, run via `run_in_threadpool(...)` because the
summary code is synchronous/blocking and we must keep it off the async event loop.
That function:
  - opens its own `SessionLocal()` (it's not in an HTTP request, so no `Depends`),
  - calls `main._build_summary(db)` which:
      - `redis_client.get("tax_analysis")` ← READ Redis: cached harvest opportunities
      - `build_portfolio_summary(db, opportunities)` ← the calculation (see below)
      - `redis_client.get("prices_last_refreshed")` ← READ Redis: timestamp
  - `json.dumps(..., default=str)` (Decimals → strings), then `send_text`.

  `harvest.build_portfolio_summary(db, opportunities)` is where the **live price
  enters the math**:
      - reads open lots + securities from **Postgres**,
      - `total_market_value += lot.quantity_remaining * security.latest_price`
        ← the live price is read back out of the Postgres column the flush wrote,
      - derives `harvestable_recommended_savings/loss` by summing the recommended
        cached opportunities (cheap — no recompute).
  This same function is called by BOTH the websocket push and `GET /portfolio-summary`
  → the summary math lives in exactly one place (no duplication).

**Step 6 — Render (Browser).**
`ws.onmessage` → `renderSummary(JSON.parse(e.data))` → updates the DOM (value,
unrealized P&L green/red, savings, budget line). **No refetch** — the data arrived on
the socket.

### CRITICAL point: we still read the DB. "Way 2" moved the read, not removed it.
The pub/sub message carries no price. On each nudge the websocket **reads the current
state fresh from Postgres** (and the Redis cache) and computes the summary right then.
The earlier design ("Way 1") had the *browser* make a separate `GET /portfolio-summary`
after each nudge. "Way 2" (current) has the *server* do that read and push the result,
so **the DB data now rides the socket instead of a browser fetch.** Same DB read,
performed server-side, delivered over the persistent connection.

Why the DB read is unavoidable: (1) the Web process can't see the Streamer's in-memory
`latest` — different process — so Postgres is its only window onto the current price;
(2) the summary is price *joined with holdings* (shares, cost basis), and holdings
live only in Postgres. The price alone isn't the summary.

---

## 5. DATA FLOW — tax savings / harvest opportunities (the slow tier)

The harvest analysis is EXPENSIVE (walks every lot, wash-sale matching), so it's on a
different clock than price.

```
Streamer recompute_tax_loop (every 30s) ──▶ Celery run_tax_analysis ──▶ writes Redis "tax_analysis"
                                                                              │
                             GET /portfolio-summary & /ws/prices push ────────┘ (read the cache)
```

- **`price_stream.recompute_tax_loop`** — second daemon thread, loops `sleep(30)` then
  `run_tax_analysis.delay()` (enqueues on Celery so the heavy work runs on the worker,
  not in the streamer).
- **`celery_app.run_tax_analysis`** — on the worker: calls
  `build_harvest_opportunities(...)` (the expensive compute; this is where
  `estimated_tax_savings` per lot is calculated via `marginal_tax_savings`), then
  `redis_client.set("tax_analysis", json.dumps(result, default=str))`.
- **Readers** — `GET /tax-analysis` serves the cache raw; `_build_summary` reads it to
  produce the savings number in the summary.

### Two clocks inside ONE summary push
| Field | Read from | Recomputed each push? | Actually changes every |
|---|---|---|---|
| `total_market_value`, `total_unrealized_gain_loss` | Postgres `latest_price` | Yes — live calc | ~1s |
| `harvestable_recommended_savings/loss` | Redis `tax_analysis` cache | No — read + sum | ~30s |

So the **savings number is delivered every 1s** (it rides the same push) but only
**changes every 30s** (the worker's recompute cadence). The websocket reads BOTH
Postgres and Redis on EVERY push, regardless of whether either changed — reads happen
at the push cadence (1s), not the change cadence. Cheap because Redis is in-memory.

**Note:** only the savings NUMBER rides the summary. The full harvest opportunities
TABLE is a separate panel (`GET /harvest-opportunities` / `loadOpportunities()`), and
it is NOT wired to live updates yet (see TODO).

---

## 6. Special cases baked into the design

- **Cold start:** a freshly loaded page can't wait for the next tick (a quiet market
  may not publish for minutes). So `ws_prices` pushes the current summary immediately
  on connect (one DB read), before entering the pub/sub loop. Page renders instantly,
  then follows the stream.
- **Change-gate:** if prices don't move (market closed / weekend), `snapshot ==
  last_published` → no publish → no push → browser idle. This is what makes it genuine
  push (silent when quiet) rather than polling in disguise.
- **Reconnect:** browser `ws.onclose` schedules `connectPrices()` again after 2s, so a
  dropped socket re-establishes.

---

## 7. Cadence summary

| Stage | Rate |
|---|---|
| `on_message` fills `latest` | hundreds/sec (feed) |
| `flush_prices` → Postgres write + Redis publish | 1/sec (throttle) |
| `/ws/prices` push → browser | 1/sec (driven by publish) |
| `renderSummary` repaint | 1/sec |
| `recompute_tax_loop` → `run_tax_analysis` | every 30s |

---

## 8. Endpoint inventory (current)

- `GET /dashboard` — serves `static/index.html`.
- `GET /portfolio-summary` — cheap; `_build_summary(db)` (reads Postgres + Redis cache).
- `WS /ws/prices` — pushes the summary on connect + on each price change.
- `GET /unrealized-gains`, `GET /portfolio` — read-live-per-request P&L.
- `GET /harvest-opportunities` — computes `build_harvest_opportunities` LIVE per request
  (expensive; NOT the cache). Used by the dashboard's opportunities table.
- `POST /tax-analysis/run` — enqueues `run_tax_analysis` (the "recompute" trigger).
- `GET /tax-analysis` — serves the cached `tax_analysis` blob.
- `GET /harvest-explanation` — enqueues the Groq AI explanation task, returns a job id.
- `POST /transactions`, `/transactions/market`, `/securities`, etc. — writes/config.
- `GET /jobs/{id}` — poll a background job's status.
- **REMOVED:** `POST /refresh-prices` and the `refresh_prices` task (replaced by the
  streamer). See loose ends below.

---

## 9. STATUS — what's DONE

- [x] Finnhub websocket streamer (`price_stream.py`): live prices → `latest` → Postgres.
- [x] Flush thread: write-back cache, 1s batched DB writes.
- [x] Tax recompute thread: `run_tax_analysis` every 30s → Redis cache.
- [x] Retired the old polling `refresh_prices` task + `POST /refresh-prices` endpoint.
- [x] `build_portfolio_summary` extracted to `harvest.py` (single source of summary math;
      also fixed the per-lot N+1 that used to be in the endpoint).
- [x] `GET /portfolio-summary` made cheap (reads cache instead of recomputing harvest).
- [x] `/ws/prices` websocket: pushes computed summary on each change-gated nudge.
- [x] Frontend: `renderSummary` + websocket client (`connectPrices`) with reconnect.

**Verified:** all Python parses; `app.main` imports clean; `/ws/prices` route registers.
**NOT verified:** the live end-to-end websocket push has NOT been run yet. Code-complete,
not runtime-proven. THIS IS THE #1 NEXT STEP.

---

## 10. TODO — what needs to be done

### A. Verify the live feature (do this first)
Bring up the full stack (Redis, Postgres, worker, streamer, uvicorn), put a crypto
symbol (`BINANCE:BTCUSDT`) in `securities` (stocks won't tick outside market hours),
run `POST /tax-analysis/run` once to seed the cache, open `/dashboard`, and confirm the
summary tiles move on their own. The async pub/sub push is the untested part.

### B. Loose ends from the streaming migration
- **Harvest opportunities TABLE not live** — only the savings number updates. Either
  poll `/tax-analysis` on a timer, or add a second pub/sub channel (`"tax_updated"`
  published by `run_tax_analysis`) so the browser refetches the table on the 30s beat.
- **`prices_last_refreshed` is written by nobody** now (the streamer never sets it), so
  the "Prices as of…" line always shows "not yet refreshed." Fix: have `flush_prices`
  write it each publish, or drop the display.
- **Dead `POST /refresh-prices`** still called by the dashboard "Refresh Prices" button
  (`index.html refreshPrices()`) and by `seed.py`. Remove/repurpose both (fetch 404s
  silently right now, so it's cosmetic).
- **N+1 in `build_harvest_opportunities`** — the wash-sale `recent_buy` query still runs
  per lot. Collapse into one query if scaling.

### C. Domain-logic gaps (the "finance-firm grade" list — known, not solved)
- No auth / multi-user / tenant isolation (biggest blocker to being "real").
- Flat tax rates (no brackets, NIIT, 0% LT band).
- Wash-sale matching is same-symbol only (not "substantially identical"); only looks 30
  days BACK (not the full ±30-day window); no wash-sale basis adjustment.
- FIFO only (no specific-lot / HIFO selection).
- Holding period uses `days > 365` rather than the calendar "more than one year" rule.
- Currency stored but unused in P&L.
- Crypto wallet-based cost-basis accounting not built (needs a schema change + Alembic
  migration; IRS per-wallet rules for crypto).
- Tests run on in-memory SQLite, not Postgres.

### D. Deferred by choice
- **Groq → Anthropic Claude** — description says Claude; code uses Groq (budget). ~2-line
  swap: `client.messages.create(...)`, read `response.content[0].text`.

---

## 11. How to run (local dev)

```bash
source venv/bin/activate            # Python 3.9; system Python lacks deps

# each in its own terminal:
redis-server                        # (or brew services)
# Postgres running, db "harvestiq"
celery -A app.celery_app:celery_app worker --loglevel=info --pool=solo
uvicorn app.main:app --reload
python -m app.price_stream

curl -X POST localhost:8000/tax-analysis/run   # seed the tax cache once
# open http://localhost:8000/dashboard
```

Gotchas: activate the venv first; restart the worker after editing tasks and uvicorn
after editing endpoints (no reliable hot-reload for task code); Celery can't serialize
`Decimal` over Redis (pass rates as strings, `Decimal(...)` back, cache with
`default=str`); run `python3 -m pytest -q` after logic changes.

---

## 12. Concept notes (for future-me)

- **Inversion of control:** `run_forever()` (streamer) and the FastAPI event loop own
  the loop; our callbacks (`on_message`, `ws_prices`) are called BY them. We register
  functions; the library invokes them. "Don't call us, we'll call you."
- **Threads vs processes:** threads share memory (why the flush thread can read
  `latest`); separate processes don't (why Streamer↔Web must go through Redis/Postgres).
- **Durable vs transient stores:** Postgres remembers state (read on demand any time);
  Redis pub/sub is a fire-and-forget notification (delivered to current subscribers,
  then gone). We need both — one for "what is it now," one for "tell me when it changed."
- **Push vs poll:** push (websocket) fires only on change; poll asks on a fixed clock.
  Push earns its complexity mainly when data is quiet sometimes (change-gate → silence).
- **GIL (still owed as a full explanation):** the two threads touching `latest` are safe
  here largely because the operations are simple and Python's GIL serializes bytecode —
  revisit this properly.
```
