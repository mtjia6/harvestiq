# HarvestIQ — Build Roadmap & Data Model

A dependency-ordered plan for turning the current scaffold into a real
tax-loss harvesting analytics platform. Issues are reordered by what
*depends on what*, not by the order they were filed.

---

## Where we are today

- FastAPI app with one flat table (`TransactionDB`).
- Endpoints: `POST/GET /transactions`, a naive `/portfolio` (sums BUY − SELL
  quantity), and a half-written `/tax-lots`.
- Postgres via SQLAlchemy, schema created with `create_all` (no migrations).

Only issue #6 is partially done. Everything else is unstarted.

---

## Foundational fixes (do these first)

These aren't feature issues — they're corrections that everything else
depends on. Getting them wrong means rewrites later.

1. **Dates as real dates.** `trade_date` is currently a `String`. FIFO
   ordering, short- vs long-term holding periods, and the 30-day wash-sale
   window all need real `Date` values.
2. **Money as Decimal, not Float.** `price` and `fees` as `Float` cause
   rounding errors in financial math. Use `Numeric`/`Decimal`.
3. **Fractional quantity.** `quantity` is an `Integer`, which makes crypto
   (#22) impossible. Use `Numeric` so 0.5 BTC is representable.
4. **Migrations (Alembic).** `create_all` can't evolve a schema safely.
   Add Alembic before the schema grows.

---

## Dependency-ordered build plan

### Phase 1 — Trustworthy data
- **#13 Transaction Validation** — reject bad transactions (unknown type,
  non-positive quantity, unknown symbol, overselling) before they persist.
- **#17 Security Master** — canonical reference for every tradable asset
  (symbol, name, asset class, currency). Symbol identity underlies cost
  basis, wash-sale "substantially identical" tests, splits, and renames.

### Phase 2 — The ledger
- **#7 Tax Lot Tracking** — the real foundation. A *lot* is a quantity
  bought at a price on a date. Portfolio state should be **derived from
  lots**, replacing the net-quantity `/portfolio` shortcut.
- **#8 FIFO Cost Basis** — consume lots oldest-first on a SELL.

### Phase 3 — Gains & losses
- **#14 Realized Gain/Loss** — proceeds − cost basis on closed lots;
  classify short vs long term by holding period.
- **#9 Market Data Provider** — live/last prices, needed for unrealized.
- **#10 Unrealized Gain/Loss** — current value − cost basis on open lots.

### Phase 4 — Tax logic
- **#15 Wash Sale Detection** — flag losses where a substantially identical
  security was bought within ±30 days.
- **#11 Harvest Opportunity Detection** — rank unrealized losses by
  estimated tax savings, excluding wash-sale-tainted lots.
- **#19 Replacement Security Recommendation** — suggest non-identical
  replacements to preserve exposure without triggering a wash sale.

### Phase 5 — Intelligence & scale
- **#12 Claude API** — natural-language explanations over real numbers.
- **#16 Broker Import Pipeline** — bulk ingest real transactions (depends on
  validation + security master).
- **#22 Multi-Asset (crypto)** — extend the model to crypto with
  wallet-based accounting. Plan the schema for this *now* (fractional qty).

### Phase 6 — Supporting
- **#20 README** — write once the shape of the app is stable.
- **#21 Improve UI** — after the API surface settles.

---

## Target data model (direction, not final)

```
security_master
  id            PK
  symbol        unique, canonical
  name
  asset_class   'equity' | 'crypto'
  currency

transactions            (raw events, immutable record of what happened)
  id            PK
  security_id   FK -> security_master
  quantity      Numeric   (fractional allowed)
  price         Numeric
  fees          Numeric
  type          'BUY' | 'SELL'
  trade_date    Date
  broker
  currency

tax_lots                 (derived: open positions with remaining quantity)
  id                PK
  security_id       FK -> security_master
  open_txn_id       FK -> transactions
  quantity_opened   Numeric
  quantity_remaining Numeric
  cost_basis        Numeric   (per unit or total — pick one, be consistent)
  opened_date       Date
  status            'open' | 'closed'
```

Lots are the source of truth for positions; the raw `transactions` table is
the immutable history that lots are built from.

---

## Working rhythm

Small steps, each ending in a git commit. One concept at a time.
