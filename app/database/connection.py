from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# 1. Create the Database Engine
SQLALCHEMY_DATABASE_URL = "sqlite:///./goddess.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})

# 2. Create the Session Factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 3. Define the Base class (models.py imports this!)
Base = declarative_base()

def init_db():
    # --- THE FIX: Break the Circular Import ---
    # We import the models down here INSIDE the function. 
    # This guarantees connection.py is fully loaded before it asks for models.py
    from app.database.models import Streamer, User, XP, Coin, ChatLog, DiscordLink, SystemState
    
    # Generate all the new SaaS tables
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()