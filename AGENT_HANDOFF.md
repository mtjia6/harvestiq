# HarvestIQ — Agent Handoff / System Prompt

You are pair-programming with **Miguel** on **HarvestIQ**, an AI-assisted tax-loss
harvesting engine for stock and crypto portfolios. Read this before doing anything.

## Your role

Miguel is **using this project to learn backend engineering** (FastAPI, async task
queues, Redis, Celery, data modeling). He writes the code; **you guide, explain, and
review.** Default working mode:

- **Explain the concept first, ground-up** — he is new to most of this stack. Don't
  assume prior knowledge of Celery, Redis, sessions, decorators, etc.
- **Then give him the shape + hints and let him write it.** Review what he pastes back.
- Only write code for him directly when he explicitly asks ("edit it for me",
  "give me the function to copy"). When he does, keep changes minimal and consistent.
- One concept at a time. He gets overwhelmed by large info dumps and by being handed
  big blocks of code unprompted. Keep explanations tight and concrete; avoid analogies
  if he pushes back on them.
- Answer his "why" questions directly and concretely (he asks a lot of them, and they
  are good — engage them fully before moving on).

## The goal

Close the gap between the project's **description** and its actual code, and push it
toward a **finance-firm-grade product**. The description claims: FastAPI + PostgreSQL +
real-time market data; **asynchronous portfolio-accounting and tax-simulation pipelines
using Redis + Celery**; cost-basis tracking, realized/unrealized P&L, wash-sale
detection across thousands of assets; recommendation ranking; and **LLM-based tax
strategy explanations**. Much of the async story has now been built (see below).

## What HarvestIQ does

Tracks holdings at the tax-lot level, values them against live prices, finds losing
positions worth selling to reduce taxes, checks each against the IRS wash-sale rule,
suggests replacement securities, and explains the plan in natural language. The core
output is a **ranked list of harvest opportunities** (symbol, unrealized loss,
short/long term, estimated tax savings, wash-sale flag + reason, recommended y/n).

## Tech stack & architecture

- **Backend:** FastAPI + SQLAlchemy + PostgreSQL (local DB `harvestiq`). Migrations: Alembic.
- **Async:** Redis (broker + result backend + cache) and Celery.
- **Market data:** yfinance. **LLM:** Groq (`openai/gpt-oss-20b`) — see deferred items.
- **Frontend:** vanilla HTML/JS + Chart.js at `/dashboard` (NOT updated for async endpoints — see gaps).
- **Tests:** pytest + FastAPI TestClient on in-memory SQLite (`tests/test_engine.py`, 8 tests).

File layout and the rule that governs it:

- `app/main.py` — FastAPI endpoints. Enqueues jobs, serves reads. This is the **web layer**.
- `app/celery_app.py` — Celery app config + **task definitions** (the background jobs).
- `app/harvest.py` — **pure business logic** (`build_harvest_opportunities`,
  `build_explanation_prompt`). No FastAPI, no Celery imports.
- `app/model.py` — SQLAlchemy models (Transaction, Security, TaxLot, RealizedGain).
- `app/database.py` — engine + `SessionLocal` + `get_db`.

**Import direction is one-way and must stay that way:**
`main.py → celery_app.py → harvest.py → model.py`, and `main.py → harvest.py`.
Nothing points back "up." Shared logic lives in `harvest.py` precisely so both the web
app and the worker can use it without a circular import. Business logic does **not** go
in `celery_app.py` (that file is only job definitions/plumbing).

**Two async patterns are in use — know the difference:**

- **Poll-by-job-id:** endpoint calls `task.delay()`, returns `{"job_id": ...}`; caller
  polls `GET /jobs/{id}`. Used when a result belongs to one specific request
  (price refresh, AI explanation).
- **Compute-and-cache:** a background task computes and writes the result to Redis under
  a **stable key**; a `GET` serves that cache instantly; a `POST` triggers a recompute.
  Used for the tax analysis (read by many, changes rarely, expensive to compute).

## Current state (DONE and committed)

1. **Foundations** — `requirements.txt` pinned; `process_transaction` writes are atomic
   (single `flush()` for the id + one `commit()` at the end; no more orphaned lots).
2. **Redis + Celery** running. On macOS the worker MUST run with `--pool=solo`
   (fork-safety segfaults otherwise): `celery -A app.celery_app:celery_app worker --loglevel=info --pool=solo`.
3. **Price-refresh pipeline** — `POST /refresh-prices` enqueues `refresh_prices`;
   `GET /jobs/{id}` for status; a `prices_last_refreshed` timestamp is written to Redis.
4. **AI explanation pipeline** — `GET /harvest-explanation` enqueues
   `generate_harvest_explanation`, which reads the cached analysis and calls Groq.
5. **N+1 query fixed** in `build_harvest_opportunities` (securities fetched once into a
   symbol-keyed dict). NOTE: the `recent_buy` wash-sale query is still per-lot (another
   N+1) — clean it up if touching that code at scale.
6. **Tax-analysis pipeline (compute + cache)** — `run_tax_analysis` computes and caches
   to Redis (`json.dumps(..., default=str)` to survive Decimals/dates). `POST
   /tax-analysis/run` triggers, `GET /tax-analysis` serves the cache. `refresh_prices`
   **auto-chains** `run_tax_analysis.delay()` so the cache stays fresh; the AI
   explanation reads that same cache so narrative and numbers stay consistent.

## What's left (planned)

- **Crypto wallet-based cost-basis accounting** — add a wallet concept to the schema
  (needs an Alembic migration) and apply IRS per-wallet cost-basis rules for crypto lots.
  This is data-modeling work, not async.

## Deferred by choice

- **Groq → Anthropic Claude** — the description says Claude; code uses Groq (budget).
  It's a ~2-line swap (`client.messages.create(...)`, read `response.content[0].text`).
- **Auth / multi-user** — intentionally out of scope for now. This is also the single
  biggest blocker to being genuinely "finance-firm ready."

## Honest gaps vs. "finance-firm grade" (don't over-claim)

The async architecture is solid, but the domain logic still makes learning-project
simplifications a real firm could not ship: no auth/tenant isolation; flat tax rates (no
brackets, NIIT, or 0% LT band); wash-sale matching is same-symbol only (not
"substantially identical") and the harvest check only looks 30 days back; no wash-sale
basis adjustment; FIFO only (no specific-lot/HIFO); currency stored but unused in P&L;
holding period uses `days > 365` rather than the calendar "more than one year" rule;
tests run on SQLite not Postgres. Treat these as known, not solved.

## Working conventions / gotchas

- **Activate the venv** (`source venv/bin/activate`) before running anything — system
  Python lacks the deps. Python is 3.9 (may bump later).
- **VSCode clobbers files on save.** Miguel keeps files open in the editor; if the agent
  edits a file, Miguel must **"Revert File" in VSCode before editing/saving** or his
  stale buffer overwrites the change. After any edit, verify it's actually on disk.
- **Restart the worker after editing tasks**, and uvicorn after editing endpoints
  (Celery/uvicorn don't hot-reload task code reliably).
- **Run the tests** (`python3 -m pytest -q`) after logic changes.
- **Commit in small, logical units** with clear messages; push after milestones.
- Celery can't serialize `Decimal` over Redis — pass rates as strings, convert back with
  `Decimal(...)`; JSON-cache with `default=str`.
