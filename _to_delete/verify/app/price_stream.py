import websocket
import json
import os
import time
import threading
from decimal import Decimal

from app.database import SessionLocal
from app.model import SecurityDB
from dotenv import load_dotenv
from app.celery_app import run_tax_analysis, redis_client

latest = {}
load_dotenv()

# Symbols we have told Finnhub we care about on the CURRENT connection. Cleared
# on every open/close because subscriptions live on the server side of that
# socket and do not survive a reconnect.
_subscribed = set()
_active_ws = None

# How often to pick up securities added since the stream started (seconds).
RESUBSCRIBE_INTERVAL = 60


def _all_symbols():
    db = SessionLocal()
    try:
        return [s.symbol for s in db.query(SecurityDB).all()]
    finally:
        db.close()


def _subscribe_new(ws, symbols): #sends a subscribe frame for anything not already subscribed on this connection. Idempotent, so it is safe to call on a timer.
    for symbol in symbols:
        if symbol in _subscribed:
            continue
        try:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
            _subscribed.add(symbol)
        except Exception as exc:
            # Socket died mid-loop; on_close will reset state and run_forever
            # will reconnect, at which point on_open re-subscribes everything.
            print("subscribe failed for %s: %r" % (symbol, exc))
            return


def on_open(ws): #queries SecurityDB for every symbol, and transmit one subscribe message per symbol to finnhub. In other words, we tell finnhub at the beginning of initiation which symbols we want to look at (subscribed to)
    global _active_ws
    _active_ws = ws
    _subscribed.clear()
    _subscribe_new(ws, _all_symbols())
    print("connected; subscribed to %d symbols" % len(_subscribed))

def on_message(ws, message): #stores the newest price everytime a message arrives on the socket in the array latest.
    data = json.loads(message)
    if data.get("type") != "trade":
        return
    for trade in data["data"]:
        latest[trade["s"]] = trade["p"]

def on_error(ws, error):
    print(error)

def on_close(ws, close_status_code, close_msg):
    global _active_ws
    _active_ws = None
    _subscribed.clear()
    print("closed (%s, %s) - run_forever will reconnect" % (close_status_code, close_msg))

def flush_prices(): #reads of the array "latest" and store it to the db. we cshould not do storing of db in on_message because on_message runs very quickly. It receives data and stores it in the array every milisecond, if we have a commit there, we would stall it or delay it from getting the newwest data of the next milisecond.
    written = {}
    tick = 0
    while True:
        time.sleep(1)
        tick += 1

        # Only touch rows whose price actually moved since our last write. The
        # old version reloaded and committed every security every second, which
        # is write amplification for nothing - most symbols are unchanged.
        snapshot = dict(latest)
        changed = {sym: px for sym, px in snapshot.items() if written.get(sym) != px}

        if changed:
            db = SessionLocal()
            try:
                securities = (
                    db.query(SecurityDB)
                    .filter(SecurityDB.symbol.in_(list(changed)))
                    .all()
                )
                for security in securities:
                    security.latest_price = Decimal(str(changed[security.symbol]))
                db.commit()
            finally:
                db.close()

            written.update(changed)
            # Notify subscribers (the FastAPI broadcaster) that prices changed,
            # but only when they actually did - silence when the feed is quiet.
            # The broadcaster rate-limits downstream, so this stays a cheap ping.
            redis_client.publish("prices", "updated")

        # Securities created after the stream started would otherwise never be
        # subscribed until a restart. Poll on a slow timer, not every tick.
        if tick % RESUBSCRIBE_INTERVAL == 0 and _active_ws is not None:
            _subscribe_new(_active_ws, _all_symbols())

def recompute_tax_loop():
    while True:
        time.sleep(30)
        run_tax_analysis.delay()

def main():
    url = f"wss://ws.finnhub.io?token={os.getenv('FINNHUB_API_KEY')}"

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=flush_prices, daemon=True).start()
    threading.Thread(target=recompute_tax_loop, daemon=True).start()
    # reconnect: retry every 5s instead of exiting the process on a dropped feed.
    # ping/pong: notice a half-open connection instead of silently going stale.
    ws.run_forever(reconnect=5, ping_interval=20, ping_timeout=10)

if __name__ == "__main__":
    main()
