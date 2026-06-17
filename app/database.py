from sqlalchemy import create_engine

DATABASE_URL = "postgresql://localhost/harvestiq"

engine = create_engine(DATABASE_URL)

print("Connected to PostgreSQL")