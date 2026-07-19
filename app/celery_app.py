from celery import Celery
from app.database import SessionLocal
from app.model import SecurityDB
import yfinance as yf
from decimal import Decimal
from datetime import datetime, timezone
import redis
import json
import requests
import os
from groq import Groq
from app.harvest import build_harvest_opportunities
from dotenv import load_dotenv

load_dotenv()

celery_app = Celery(
    "harvestiq",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
)

# Shared store both the worker and the web app can read/write.
redis_client = redis.Redis.from_url("redis://localhost:6379/0")


@celery_app.task
def add(x: int, y: int):
    return x + y


@celery_app.task
def refresh_prices():
    db = SessionLocal()
    try:
        securities = db.query(SecurityDB).all()
        updated = []
        for security in securities:
            ticker = yf.Ticker(security.symbol)
            data = ticker.history(period="1d")

            if data.empty:
                continue

            latest_close = float(data["Close"].iloc[-1])
            security.latest_price = Decimal(str(round(latest_close, 2)))
            updated.append({
                "symbol": security.symbol,
                "latest_price": float(security.latest_price),
            })
        db.commit()
        redis_client.set(
            "prices_last_refreshed",
            datetime.now(timezone.utc).isoformat(),
        )
        run_tax_analysis.delay()
        return {"updated": updated}
    finally:
        db.close()

def build_explanation_prompt(db, opportunities):
    def replacements_for(symbol):
        sec = db.query(SecurityDB).filter(SecurityDB.symbol == symbol).first()
        if sec is None or sec.sector is None:
            return []
        alts = (
            db.query(SecurityDB)
            .filter(SecurityDB.sector == sec.sector)
            .filter(SecurityDB.symbol != symbol)
            .all()
        )
        return [a.symbol for a in alts]

    lines = []
    for o in opportunities:
        reps = replacements_for(o["symbol"])
        if o["recommended"]:
            status = "recommended (no wash-sale risk)"
        else:
            status = f"WASH-SALE RISK: {o.get('wash_sale_reason') or 'a purchase within the last 30 days'}"
        rep_txt = f"; possible replacements: {', '.join(reps)}" if reps else ""
        lines.append(
            f"- {o['symbol']}: {o['quantity_remaining']} units, unrealized loss {o['unrealized_loss']}, "
            f"{o['term']}-term, estimated tax savings {o['estimated_tax_savings']}, {status}{rep_txt}"
        )
    summary = "\n".join(lines)

    return (
        "You are a concise Wall Street-level financial assistant explaining tax-loss harvesting to a beginner. "
        "Here are the harvest opportunities in the portfolio:\n\n"
        f"{summary}\n\n"
        "For each position, state the estimated tax saving and whether to act now. "
        "If a position is a WASH-SALE RISK, explain the specific stated reason and that selling now would "
        "DISALLOW the loss, so they should wait until the 30-day window passes or harvest a different position; "
        "recommend the listed replacement tickers if provided. "
        "Do NOT use vague phrasing like 'or will buy one within 30 days' — use the actual reason given. "
        "Be specific and concise. End with exactly one line: 'This is not personalized tax advice.'"
    )

@celery_app.task
def generate_harvest_explanation():
    db = SessionLocal()
    try:
        raw = redis_client.get("tax_analysis")
        if raw is None:
            return {"explanation": "No analysis available yet — run POST /tax-analysis/run first."}
        opportunities = json.loads(raw)["opportunities"]
        if not opportunities:
            return {"explanation": "No harvesting opportunities right now — none of your open lots are at a loss."}
        prompt = build_explanation_prompt(db, opportunities)
        client = Groq()
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"explanation": response.choices[0].message.content}
    finally:
        db.close()

@celery_app.task
def run_tax_analysis(short_term_rate="0.40", long_term_rate="0.20"):
    db = SessionLocal()
    try:
        opportunities = build_harvest_opportunities(db, Decimal(short_term_rate), Decimal(long_term_rate))
        generated_at = datetime.now(timezone.utc).isoformat()

        result = {"opportunities": opportunities, "generated_at": generated_at}
        redis_client.set("tax_analysis", json.dumps(result, default=str))
        return {"count": len(opportunities)}
    finally:
        db.close()

