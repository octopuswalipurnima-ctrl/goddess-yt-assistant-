from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.utils.config import Config
from app.database.models import Base

# Create the SQLite engine
engine = create_engine(Config.DATABASE_URL, connect_args={"check_same_thread": False})

# Create a session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """Builds all the tables if they don't exist yet."""
    Base.metadata.create_all(bind=engine)
    print("Database initialized successfully.")

def get_db():
    """Opens a database session and closes it safely when done."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        