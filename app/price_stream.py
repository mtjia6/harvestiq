import websocket
import json
import os
import time
import threading
from decimal import Decimal

from app.database import SessionLocal
from app.model import SecurityDB
from dotenv import load_dotenv
from app.celery_app import run_tax_analysis

latest = {}
load_dotenv()

def on_open(ws): #queries SecurityDB for every symbol, and transmit one subscribe message per symbol to finnhub. In other words, we tell finnhub at the beginning of initiation which symbols we want to look at (subscribed to)
    db = SessionLocal()
    try:
        securities = db.query(SecurityDB).all()
        for security in securities:
            ws.send(json.dumps({"type": "subscribe", "symbol": security.symbol}))

    finally:
        db.close()

def on_message(ws, message): #stores the newest price everytime a message arrives on the socket in the array latest.
    data = json.loads(message)
    if data.get("type") != "trade":
        return
    for trade in data["data"]:
        latest[trade["s"]] = trade["p"]
    print(latest)

def on_error(ws, error):
    print(error)

def on_close(ws, close_status_code, close_msg):
    print("closed")

def flush_prices(): #reads of the array "latest" and store it to the db. we cshould not do storing of db in on_message because on_message runs very quickly. It receives data and stores it in the array every milisecond, if we have a commit there, we would stall it or delay it from getting the newwest data of the next milisecond.
    while True:
        time.sleep(1)
        db = SessionLocal()
        try:
            securities = db.query(SecurityDB).all()
            for security in securities:
                if security.symbol in latest:
                    security.latest_price = Decimal(str(latest[security.symbol]))
            db.commit()
        finally:
            db.close()

def recompute_tax_loop():
    while True:
        time.sleep(30)
        run_tax_analysis.delay()

def main():
    url = f"wss://ws.finnhub.io?token={os.getenv('FINNHUB_API_KEY')}"

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=flush_prices, daemon=True).start()
    threading.Thread(target=recompute_tax_loop, daemon=True).start()
    ws.run_forever()

if __name__ == "__main__":
    main()

