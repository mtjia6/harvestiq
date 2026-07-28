# One-terminal dev startup: `honcho start` (pip install honcho).
# Redis and Postgres are expected to be running as services already
# (brew services start redis / postgresql) — they're not listed here so a
# port-in-use exit doesn't take down the app processes with it.
web: uvicorn app.main:app --reload
worker: celery -A app.celery_app:celery_app worker --loglevel=info --pool=solo
streamer: python -m app.price_stream
