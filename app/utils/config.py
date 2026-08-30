"""Environment-only configuration. Secrets are never persisted by the app."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goddess.db")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
    SESSION_SECRET = os.getenv("SESSION_SECRET")
    GEMINI_PRIMARY_MODEL = os.getenv("GEMINI_PRIMARY_MODEL", "gemini-2.5-flash")
    GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
    GEMINI_API_KEYS: list[str] = []
    YOUTUBE_API_KEYS: list[str] = []

    @classmethod
    def load_multi_keys(cls):
        cls.GEMINI_API_KEYS = [os.getenv(f"GEMINI_API_KEY_{i}") for i in range(1, 11) if os.getenv(f"GEMINI_API_KEY_{i}")]
        cls.YOUTUBE_API_KEYS = [os.getenv(f"YOUTUBE_API_KEY_{i}") for i in range(1, 11) if os.getenv(f"YOUTUBE_API_KEY_{i}")]
        if not cls.GEMINI_API_KEYS and os.getenv("GEMINI_API_KEY"):
            cls.GEMINI_API_KEYS = [os.getenv("GEMINI_API_KEY")]
        if not cls.YOUTUBE_API_KEYS and os.getenv("YOUTUBE_API_KEY"):
            cls.YOUTUBE_API_KEYS = [os.getenv("YOUTUBE_API_KEY")]

    @classmethod
    def missing_core(cls) -> list[str]:
        cls.load_multi_keys()
        return [name for name, value in (("YOUTUBE_CLIENT_ID", cls.YOUTUBE_CLIENT_ID), ("YOUTUBE_CLIENT_SECRET", cls.YOUTUBE_CLIENT_SECRET), ("YOUTUBE_REFRESH_TOKEN", cls.YOUTUBE_REFRESH_TOKEN)) if not value]

    @classmethod
    def production_warnings(cls) -> list[str]:
        cls.load_multi_keys()
        warnings = []
        if not cls.SESSION_SECRET: warnings.append("SESSION_SECRET is not set; dashboard sessions use a generated unsafe fallback.")
        if not cls.GEMINI_API_KEYS: warnings.append("No Gemini key configured; AI moderation/co-host are disabled.")
        if not cls.DISCORD_BOT_TOKEN: warnings.append("No Discord token configured; Discord event delivery is disabled.")
        return warnings


Config.load_multi_keys()
