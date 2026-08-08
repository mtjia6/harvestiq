from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import get_db
from app.model import Base

# A throwaway in-memory SQLite database, shared for the test run.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# Point the app's database dependency at the test DB instead of Postgres.
app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts with clean, empty tables."""
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


# ---------- helpers ----------
def add_security(symbol, asset_class="equity", sector="technology", price=None):
    client.post("/securities", json={
        "symbol": symbol, "name": symbol, "asset_class": asset_class,
        "currency": "USD", "sector": sector,
    })
    if price is not None:
        client.post(f"/securities/{symbol}/price", json={"latest_price": price})


def buy(symbol, qty, price, trade_date, fees=0):
    return client.post("/transactions", json={
        "symbol": symbol, "quantity": qty, "price": price,
        "transaction_type": "BUY", "trade_date": trade_date,
        "broker": "test", "fees": fees, "currency": "USD",
    })


def sell(symbol, qty, price, trade_date, fees=0):
    return client.post("/transactions", json={
        "symbol": symbol, "quantity": qty, "price": price,
        "transaction_type": "SELL", "trade_date": trade_date,
        "broker": "test", "fees": fees, "currency": "USD",
    })


# ---------- tests ----------
def test_unknown_symbol_is_rejected():
    r = buy("FAKE", 10, 100, "2024-01-01")
    assert r.status_code == 400


def test_cost_basis_includes_fees():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2024-01-01", fees=10)
    lots = client.get("/tax-lots").json()
    assert len(lots) == 1
    # (150 * 10 + 10 fee) / 10 = 151.0
    assert float(lots[0]["cost_basis_per_unit"]) == pytest.approx(151.0)


def test_fifo_sells_oldest_lot_first():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2024-01-01")
    buy("AAPL", 10, 180, "2024-03-01")
    sell("AAPL", 15, 200, "2024-06-01")
    lots = {l["opened_date"]: l for l in client.get("/tax-lots").json()}
    assert float(lots["2024-01-01"]["quantity_remaining"]) == 0
    assert lots["2024-01-01"]["status"] == "closed"
    assert float(lots["2024-03-01"]["quantity_remaining"]) == pytest.approx(5)


def test_oversell_is_rejected():
    add_security("AAPL")
    buy("AAPL", 5, 100, "2024-01-01")
    r = sell("AAPL", 10, 120, "2024-02-01")
    assert r.status_code == 400


def test_realized_gain_and_short_term():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2024-01-01")
    sell("AAPL", 10, 200, "2024-06-01")   # held ~5 months
    gains = client.get("/realized-gains").json()
    assert len(gains) == 1
    assert float(gains[0]["gain_loss"]) == pytest.approx(500)
    assert gains[0]["term"] == "short"


def test_wash_sale_detected():
    add_security("AAPL")
    buy("AAPL", 10, 200, "2024-01-01")
    sell("AAPL", 10, 150, "2024-02-01")   # $500 loss
    buy("AAPL", 10, 150, "2024-02-15")    # rebuy 14 days later
    washes = client.get("/wash-sales").json()
    assert len(washes) == 1
    assert washes[0]["wash_sale"] is True


def test_crypto_has_no_wash_sale():
    add_security("BTC-USD", asset_class="crypto", sector="crypto", price=90000)
    today = date.today().isoformat()
    buy("BTC-USD", 1, 100000, today)      # bought today (recent) at a loss
    opps = client.get("/harvest-opportunities").json()
    btc = [o for o in opps if o["symbol"] == "BTC-USD"][0]
    assert btc["wash_sale_risk"] is False
    assert btc["recommended"] is True


def test_harvest_savings_uses_rate():
    add_security("AAPL", price=130)
    buy("AAPL", 10, 150, "2023-01-01")    # long-term, now at a loss
    opps = client.get(
        "/harvest-opportunities?long_term_rate=0.05&short_term_rate=0.15"
    ).json()
    aapl = [o for o in opps if o["symbol"] == "AAPL"][0]
    assert float(aapl["unrealized_loss"]) == pytest.approx(-200)   # 10 * (130 - 150)
    # No realized gains anywhere this year (fresh test DB), so this loss has
    # nothing to net against -> falls back to the ordinary-income rate (short_rate).
    assert float(aapl["estimated_tax_savings"]) == pytest.approx(30)  # 200 * 0.15
    assert aapl["term"] == "long"

# ---------- tax engine (pure functions — no HTTP, no DB) ----------
from decimal import Decimal

from app.harvest import marginal_harvest_value, tax_outcome

SR = Decimal("0.35")  # short-term / ordinary rate
LR = Decimal("0.15")  # long-term rate


def test_waterfall_gains_only():
    out = tax_outcome(Decimal("1000"), Decimal("2000"), SR, LR)
    assert out["liability"] == Decimal("1000") * SR + Decimal("2000") * LR
    assert out["carryforward_st"] == 0 and out["carryforward_lt"] == 0


def test_st_loss_cancels_st_gain_at_short_rate():
    # $4,000 ST gain; harvest a $4,000 ST loss -> saves the full 35% tax.
    savings, carry = marginal_harvest_value(
        Decimal("4000"), "short", Decimal("4000"), Decimal("0"), SR, LR
    )
    assert savings == Decimal("4000") * SR
    assert carry == 0


def test_same_loss_worth_more_against_st_than_lt():
    # $1,000 ST gain and $1,000 LT gain on the books. The same-size loss
    # saves 35% if it's short-term, only 15% if it's long-term — the loss
    # must cancel gains of its own character first.
    st_savings, _ = marginal_harvest_value(
        Decimal("1000"), "short", Decimal("1000"), Decimal("1000"), SR, LR
    )
    lt_savings, _ = marginal_harvest_value(
        Decimal("1000"), "long", Decimal("1000"), Decimal("1000"), SR, LR
    )
    assert st_savings == Decimal("350")
    assert lt_savings == Decimal("150")


def test_cross_netting_st_loss_against_lt_gain():
    # No ST gains; a $2,000 ST loss cross-nets against a $5,000 LT gain,
    # so it saves at the LONG rate (it cancels long-term gain).
    savings, carry = marginal_harvest_value(
        Decimal("2000"), "short", Decimal("0"), Decimal("5000"), SR, LR
    )
    assert savings == Decimal("2000") * LR
    assert carry == 0


def test_3000_cap_with_no_gains():
    # Nothing to net against: a $5,000 harvested loss deducts $3,000 against
    # ordinary income (valued at the ordinary/short rate); $2,000 rolls forward.
    savings, carry = marginal_harvest_value(
        Decimal("5000"), "short", Decimal("0"), Decimal("0"), SR, LR
    )
    assert savings == Decimal("3000") * SR
    assert carry == Decimal("2000")


def test_net_loss_year_extra_harvest_saves_nothing():
    # THE OLD BUG: already $10,000 net ST loss YTD — the $3,000 deduction is
    # maxed out. An extra $5,000 loss saves $0 this year; it is ALL deferral.
    # The old engine reported 5000 * 0.35 = $1,750 of phantom savings here.
    savings, carry = marginal_harvest_value(
        Decimal("5000"), "short", Decimal("-10000"), Decimal("0"), SR, LR
    )
    assert savings == 0
    assert carry == Decimal("5000")


def test_partial_gain_then_cap_then_carryforward():
    # $2,000 ST gain on the books; harvest a $9,000 ST loss.
    # First $2,000 cancels the gain (35%), next $3,000 hits the ordinary
    # cap (35%), the final $4,000 is pure carryforward.
    savings, carry = marginal_harvest_value(
        Decimal("9000"), "short", Decimal("2000"), Decimal("0"), SR, LR
    )
    assert savings == (Decimal("2000") + Decimal("3000")) * SR
    assert carry == Decimal("4000")


# ---------- carryforward settings (API) ----------
def test_tax_settings_roundtrip():
    r = client.get("/tax-settings").json()
    assert float(r["carryforward_st"]) == 0
    client.put("/tax-settings", json={"carryforward_st": "22000", "carryforward_lt": "5000"})
    r = client.get("/tax-settings").json()
    assert float(r["carryforward_st"]) == pytest.approx(22000)
    assert float(r["carryforward_lt"]) == pytest.approx(5000)


def test_carryforward_shelters_gains_so_harvest_saves_nothing():
    # User enters the year with a $22,000 ST carryforward, then realizes a
    # $15,000 gain. The carryforward already shelters the whole gain (and the
    # $3k deduction), so a new harvested loss is worth $0 this year — the old
    # engine, blind to carryforwards, would have priced it at the full rate.
    client.put("/tax-settings", json={"carryforward_st": "22000", "carryforward_lt": "0"})
    add_security("AAPL", price=100)
    add_security("MSFT", price=100)
    buy("AAPL", 100, 50, "2026-01-05")
    sell("AAPL", 100, 200, "2026-02-01")          # +$15,000 realized ST gain
    buy("MSFT", 100, 150, "2026-01-05")            # now down $5,000
    opps = client.get("/harvest-opportunities").json()
    msft = [o for o in opps if o["symbol"] == "MSFT"][0]
    assert float(msft["estimated_tax_savings"]) == pytest.approx(0)
    assert float(msft["carryforward_generated"]) == pytest.approx(5000)


# ---------- harvest plan (fix #2: portfolio-level, sequentially priced) ----------
def test_plan_prices_sequentially_not_in_parallel():
    # The canonical case: $4,000 ST gain on the books, three lots each with a
    # $4,000 ST loss, rates 35/15. Priced independently they'd claim
    # 3 x $1,400 = $4,200. Executed in sequence the truth is:
    #   step 1: cancels the gain            -> $1,400
    #   step 2: $3,000 ordinary cap         -> $1,050 (+$1,000 carryforward)
    #   lot 3:  cap exhausted               -> $0, carryforward-only
    add_security("GAIN", price=90)
    buy("GAIN", 100, 50, "2026-01-05")
    sell("GAIN", 100, 90, "2026-02-01")            # +$4,000 realized ST gain
    for sym in ("LOSA", "LOSB", "LOSC"):
        add_security(sym, price=60)
        buy(sym, 100, 100, "2026-01-05")           # each down $4,000, no recent buy

    plan = client.get(
        "/harvest-plan?short_term_rate=0.35&long_term_rate=0.15"
    ).json()

    assert len(plan["steps"]) == 2
    assert float(plan["steps"][0]["step_savings"]) == pytest.approx(1400)
    assert float(plan["steps"][1]["step_savings"]) == pytest.approx(1050)
    assert float(plan["steps"][1]["step_carryforward"]) == pytest.approx(1000)
    assert float(plan["total_current_year_savings"]) == pytest.approx(2450)
    assert float(plan["total_harvest_loss"]) == pytest.approx(8000)
    assert plan["stop_reason"] == "no_further_current_year_benefit"

    assert len(plan["carryforward_only"]) == 1
    assert float(plan["carryforward_only"][0]["carryforward_if_harvested"]) == pytest.approx(4000)


def test_plan_orders_st_loss_before_lt_loss():
    # $1,000 ST gain and $1,000 LT gain; one ST-loss lot and one LT-loss lot,
    # both $1,000. The ST loss saves 35%, the LT loss 15% -> ST goes first.
    add_security("GAIN", price=90)
    buy("GAIN", 50, 50, "2026-01-05")
    sell("GAIN", 25, 90, "2026-02-01")             # +$1,000 ST gain
    add_security("GAINL", price=90)
    buy("GAINL", 25, 50, "2024-06-01")
    sell("GAINL", 25, 90, "2026-02-01")            # +$1,000 LT gain
    add_security("STLOSS", price=90)
    buy("STLOSS", 10, 190, "2026-01-05")           # -$1,000, short-term
    add_security("LTLOSS", price=90)
    buy("LTLOSS", 10, 190, "2024-06-01")           # -$1,000, long-term

    plan = client.get(
        "/harvest-plan?short_term_rate=0.35&long_term_rate=0.15"
    ).json()

    assert [s["symbol"] for s in plan["steps"][:2]] == ["STLOSS", "LTLOSS"]
    assert float(plan["steps"][0]["step_savings"]) == pytest.approx(350)
    assert float(plan["steps"][1]["step_savings"]) == pytest.approx(150)


def test_plan_excludes_wash_sale_risky_lots():
    add_security("GAIN", price=90)
    buy("GAIN", 100, 50, "2026-01-05")
    sell("GAIN", 100, 90, "2026-02-01")            # gains, so losses have value
    # TWO recent buys: selling either lot leaves the other's shares as
    # replacement shares -> both are genuine wash-sale risks.
    add_security("FRESH", price=60)
    buy("FRESH", 10, 100, date.today().isoformat())
    buy("FRESH", 5, 100, date.today().isoformat())

    plan = client.get("/harvest-plan").json()
    assert all(s["symbol"] != "FRESH" for s in plan["steps"])
    assert {e["symbol"] for e in plan["excluded_wash_sale"]} == {"FRESH"}


# ---------- wash-sale correctness (fix #3) ----------
from datetime import timedelta

def test_full_liquidation_of_recent_buy_is_not_wash_risky():
    # THE OLD FALSE POSITIVE: a lot bought 2 weeks ago that immediately
    # dropped is a harvester's best candidate. Selling ALL of it leaves no
    # replacement shares -> no wash sale -> must be recommended.
    add_security("NVDA", price=85)
    two_weeks_ago = (date.today() - timedelta(days=14)).isoformat()
    buy("NVDA", 10, 100, two_weeks_ago)
    opps = client.get("/harvest-opportunities").json()
    nvda = [o for o in opps if o["symbol"] == "NVDA"][0]
    assert nvda["wash_sale_risk"] is False
    assert nvda["recommended"] is True
    assert nvda["safe_to_rebuy_after"] is not None


def test_remaining_window_shares_still_flag_wash_risk():
    # An OLDER losing lot, plus a fresh buy of the same symbol that the sale
    # would NOT liquidate -> those fresh shares are replacement shares.
    add_security("AAPL", price=80)
    buy("AAPL", 10, 150, "2026-01-05")                       # old losing lot
    buy("AAPL", 5, 82, (date.today() - timedelta(days=10)).isoformat())
    opps = client.get("/harvest-opportunities").json()
    old_lot = [o for o in opps if float(o["unrealized_loss"]) == pytest.approx(-700)][0]
    assert old_lot["wash_sale_risk"] is True
    assert "5 shares" in old_lot["wash_sale_reason"]


def test_buy_side_wash_sale_disallows_and_adjusts_basis():
    # Sell at a loss, rebuy 14 days later: the loss is disallowed and rolls
    # into the new lot's basis; the holding period tacks back.
    add_security("AAPL")
    buy("AAPL", 10, 200, "2026-01-01")
    sell("AAPL", 10, 150, "2026-02-01")            # $500 economic loss
    buy("AAPL", 10, 150, "2026-02-15")             # replacement purchase

    gains = client.get("/realized-gains").json()
    assert float(gains[0]["gain_loss"]) == pytest.approx(0)          # allowed: none
    assert float(gains[0]["disallowed_wash_loss"]) == pytest.approx(500)

    lots = [l for l in client.get("/tax-lots").json() if l["status"] == "open"]
    assert len(lots) == 1
    # $150 purchase + $500/10 disallowed loss = $200/share adjusted basis
    assert float(lots[0]["cost_basis_per_unit"]) == pytest.approx(200)
    # Tacking: opened Feb 15 minus the 31 days the old lot was held = Jan 15
    assert lots[0]["opened_date"] == "2026-01-15"


def test_sell_side_wash_sale_partial_disallowance():
    # 100 shares bought long ago at $100; 50 more bought 9 days before the
    # sale at $90. Selling the OLD 100 at $80 realizes a $2,000 loss, but 50
    # replacement shares exist -> half the loss (50 sh x $20) is disallowed.
    add_security("MSFT", price=80)
    buy("MSFT", 100, 100, "2026-01-05")
    buy("MSFT", 50, 90, "2026-02-20")
    sell("MSFT", 100, 80, "2026-03-01")            # FIFO -> consumes the old lot

    gains = client.get("/realized-gains").json()
    assert float(gains[0]["gain_loss"]) == pytest.approx(-1000)      # allowed half
    assert float(gains[0]["disallowed_wash_loss"]) == pytest.approx(1000)

    lots = [l for l in client.get("/tax-lots").json() if l["status"] == "open"]
    assert len(lots) == 1
    # $90 basis + $1,000 spread over 50 shares = $110/share
    assert float(lots[0]["cost_basis_per_unit"]) == pytest.approx(110)

    washes = client.get("/wash-sales").json()
    assert washes[0]["wash_sale"] is True
    assert float(washes[0]["disallowed_loss"]) == pytest.approx(1000)
    assert float(washes[0]["allowed_loss"]) == pytest.approx(-1000)


# ---------- lot selection (fix #4) ----------
def test_hifo_sells_highest_basis_lot_first():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2026-01-05")
    buy("AAPL", 10, 180, "2026-03-02")
    r = client.post("/transactions", json={
        "symbol": "AAPL", "quantity": 10, "price": 160,
        "transaction_type": "SELL", "trade_date": "2026-06-01",
        "broker": "test", "fees": 0, "currency": "USD",
        "lot_selection": "hifo",
    })
    assert r.status_code == 201
    lots = {l["opened_date"]: l for l in client.get("/tax-lots").json()}
    assert lots["2026-03-02"]["status"] == "closed"                  # $180 lot sold
    assert float(lots["2026-01-05"]["quantity_remaining"]) == pytest.approx(10)
    gains = client.get("/realized-gains").json()
    assert float(gains[0]["gain_loss"]) == pytest.approx(-200)       # (160-180) x 10


def test_specific_lot_sale_targets_exactly_that_lot():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2026-01-05")
    buy("AAPL", 10, 180, "2026-03-02")
    lots = client.get("/tax-lots").json()
    target = [l for l in lots if l["opened_date"] == "2026-03-02"][0]
    r = client.post("/transactions", json={
        "symbol": "AAPL", "quantity": 10, "price": 160,
        "transaction_type": "SELL", "trade_date": "2026-06-01",
        "broker": "test", "fees": 0, "currency": "USD",
        "lot_selection": "specific", "lot_ids": [target["id"]],
    })
    assert r.status_code == 201
    lots = {l["opened_date"]: l for l in client.get("/tax-lots").json()}
    assert lots["2026-03-02"]["status"] == "closed"
    assert float(lots["2026-01-05"]["quantity_remaining"]) == pytest.approx(10)


def test_specific_lot_sale_validates_lot():
    add_security("AAPL")
    buy("AAPL", 10, 150, "2026-01-05")
    base = {
        "symbol": "AAPL", "quantity": 5, "price": 160,
        "transaction_type": "SELL", "trade_date": "2026-06-01",
        "broker": "test", "fees": 0, "currency": "USD",
        "lot_selection": "specific",
    }
    assert client.post("/transactions", json=base).status_code == 400            # no lot_ids
    assert client.post("/transactions", json={**base, "lot_ids": [9999]}).status_code == 400
    lot_id = client.get("/tax-lots").json()[0]["id"]
    r = client.post("/transactions", json={**base, "lot_ids": [lot_id], "broker": "other"})
    assert r.status_code == 400                                                  # wrong broker


def test_holding_period_uses_calendar_year_not_365_days():
    add_security("AAPL")
    # Bought Feb 1, 2025. Sold exactly on the first anniversary: SHORT.
    buy("AAPL", 10, 100, "2025-02-01")
    sell("AAPL", 5, 120, "2026-02-01")
    # Sold the day after the anniversary: LONG.
    sell("AAPL", 5, 120, "2026-02-02")
    gains = sorted(client.get("/realized-gains").json(), key=lambda g: g["sell_date"])
    assert gains[0]["term"] == "short"
    assert gains[1]["term"] == "long"
