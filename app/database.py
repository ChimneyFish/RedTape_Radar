import os
from sqlalchemy import create_backend, create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

DATABASE_URL = "sqlite:///./redtape_radar.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    # Creates the .db file and tables if they don't exist yet
    Base.metadata.create_all(bind=engine)

def get_db():
    """Dependency helper to yield database sessions to FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()