import os
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

class Config:
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
    
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database.db")
    
    # ---------------------------------------------------------
    # MULTI-PROJECT API KEY LOADERS
    # ---------------------------------------------------------
    GEMINI_API_KEYS = []
    YOUTUBE_API_KEYS = []

    @classmethod
    def load_multi_keys(cls):
        # Load up to 10 Gemini Keys
        for i in range(1, 11):
            key = os.getenv(f"GEMINI_API_KEY_{i}")
            if key: 
                cls.GEMINI_API_KEYS.append(key)
                
        # Fallback to standard key if numbered ones aren't found
        if not cls.GEMINI_API_KEYS and os.getenv("GEMINI_API_KEY"):
            cls.GEMINI_API_KEYS.append(os.getenv("GEMINI_API_KEY"))

        # Load up to 10 YouTube Data API Keys
        for i in range(1, 11):
            key = os.getenv(f"YOUTUBE_API_KEY_{i}")
            if key: 
                cls.YOUTUBE_API_KEYS.append(key)
                
        # Fallback to standard key if numbered ones aren't found
        if not cls.YOUTUBE_API_KEYS and os.getenv("YOUTUBE_API_KEY"):
            cls.YOUTUBE_API_KEYS.append(os.getenv("YOUTUBE_API_KEY"))

    @classmethod
    def validate(cls):
        cls.load_multi_keys()
        missing = []
        
        if not cls.YOUTUBE_CLIENT_ID: missing.append("YOUTUBE_CLIENT_ID")
        if not cls.YOUTUBE_CLIENT_SECRET: missing.append("YOUTUBE_CLIENT_SECRET")
        if not cls.YOUTUBE_REFRESH_TOKEN: missing.append("YOUTUBE_REFRESH_TOKEN")
        if not cls.GEMINI_API_KEYS: missing.append("GEMINI_API_KEY_1 (At least one required)")
        if not cls.DISCORD_BOT_TOKEN: missing.append("DISCORD_BOT_TOKEN")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

Config.validate()