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
    assert float(aapl["estimated_tax_savings"]) == pytest.approx(10)  # 200 * 0.05
    assert aapl["term"] == "long"