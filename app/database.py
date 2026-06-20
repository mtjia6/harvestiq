from sqlalchemy import create_engine
from app.model import Base

DATABASE_URL = "postgresql://localhost/harvestiq"

engine = create_engine(DATABASE_URL)

Base.metadata.create_all(engine)

print("Connected to PostgreSQL")