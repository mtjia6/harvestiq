from sqlalchemy import create_engine
from app.model import Base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "postgresql://localhost/harvestiq"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()

print("Connected to PostgreSQL")

