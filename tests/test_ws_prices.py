"""Behavioural tests for the /ws/prices pipeline (real Redis, in-memory SQLite).

Run with a local redis on :6379 — the same one Celery already uses.
"""
import json
import time

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main
from app.database import get_db
from app.model import Base

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


r = redis.Redis.from_url("redis://localhost:6379/0")

# Count how many times the (expensive) summary rebuild actually runs.
computes = []


def fake_summary_json():
    computes.append(time.time())
    return json.dumps({"tick": len(computes)})


@pytest.fixture(autouse=True)
def patch_summary(monkeypatch):
    computes.clear()
    main.price_clients.clear()
    # Scope the override to this module's tests so test_engine.py keeps its own DB.
    main.app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main, "_summary_json", fake_summary_json)
    monkeypatch.setattr(main, "MIN_PUSH_INTERVAL", 0.2)
    yield
    main.app.dependency_overrides.pop(get_db, None)


def publish_and_settle(n=1, gap=0.0, settle=0.6):
    for _ in range(n):
        r.publish("prices", "updated")
        if gap:
            time.sleep(gap)
    time.sleep(settle)


def test_pushes_on_price_change():
    with TestClient(main.app) as client:               # `with` runs the lifespan
        with client.websocket_connect("/ws/prices") as ws:
            assert json.loads(ws.receive_text())["tick"] == 1   # cold-start push
            publish_and_settle()
            assert json.loads(ws.receive_text())["tick"] == 2   # broadcast push


def test_one_compute_serves_many_clients():
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/prices") as a, \
             client.websocket_connect("/ws/prices") as b:
            a.receive_text(); b.receive_text()         # two cold-start pushes
            before = len(computes)
            publish_and_settle()
            # ONE rebuild fanned out to both clients, not one rebuild per client.
            assert len(computes) - before == 1
            assert a.receive_text() == b.receive_text()


def test_bursts_are_coalesced():
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/prices") as ws:
            ws.receive_text()
            before = len(computes)
            publish_and_settle(n=20, gap=0.005, settle=0.5)
            # 20 publishes inside one window must not cost 20 rebuilds.
            assert 1 <= len(computes) - before <= 2


def test_no_clients_means_no_db_work():
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/prices") as ws:
            ws.receive_text()
        time.sleep(0.4)                                # let the close land
        assert main.price_clients == set()
        before = len(computes)
        publish_and_settle(n=3, gap=0.05)
        assert len(computes) == before                 # nobody watching: no rebuild


def test_disconnect_is_detected_and_client_released():
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/prices") as ws:
            ws.receive_text()
            assert len(main.price_clients) == 1
        # The old handler never awaited receive(), so this set stayed populated
        # forever and every future tick paid for a socket nobody was reading.
        deadline = time.time() + 3
        while main.price_clients and time.time() < deadline:
            time.sleep(0.05)
        assert main.price_clients == set()
