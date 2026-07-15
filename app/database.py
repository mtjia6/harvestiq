import os

from sqlalchemy import create_engine
from app.model import Base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/harvestiq")

# Sized deliberately against Starlette's threadpool. Summary builds run through
# run_in_threadpool, and the SQLAlchemy default (5 connections + 10 overflow) is
# small enough that a handful of concurrent threads produce "QueuePool limit
# reached" long before any real load. pool_pre_ping drops connections Postgres
# closed under us; pool_recycle keeps long-lived worker connections from staling.
_pool_kwargs = {}
if not DATABASE_URL.startswith("sqlite"):
    # SQLite's default pool classes reject these, and a file/memory DB has no
    # connection cost worth pooling. Only size the pool for a real server.
    _pool_kwargs = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", "10")),
        "pool_recycle": 1800,
    }

engine = create_engine(DATABASE_URL, pool_pre_ping=True, **_pool_kwargs)
SessionLocal = sessionmaker(bind=engine)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
