import os
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

class Config:
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database.db")
    
    @classmethod
    def validate(cls):
        missing = []
        if not cls.YOUTUBE_API_KEY: missing.append("YOUTUBE_API_KEY")
        if not cls.GEMINI_API_KEY: missing.append("GEMINI_API_KEY")
        if not cls.DISCORD_BOT_TOKEN: missing.append("DISCORD_BOT_TOKEN")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

Config.validate()