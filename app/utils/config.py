import os
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

class Config:
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database.db")
    
    @classmethod
    def validate(cls):
        missing = []
        if not cls.YOUTUBE_CLIENT_ID: missing.append("YOUTUBE_CLIENT_ID")
        if not cls.YOUTUBE_CLIENT_SECRET: missing.append("YOUTUBE_CLIENT_SECRET")
        if not cls.YOUTUBE_REFRESH_TOKEN: missing.append("YOUTUBE_REFRESH_TOKEN")
        if not cls.GEMINI_API_KEY: missing.append("GEMINI_API_KEY")
        if not cls.DISCORD_BOT_TOKEN: missing.append("DISCORD_BOT_TOKEN")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

Config.validate()