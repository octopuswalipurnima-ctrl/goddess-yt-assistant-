"""Small deterministic helpers for YouTube chat mentions."""
import re


_DUPLICATE_MENTION = re.compile(r"@{2,}([A-Za-z0-9_.-]+)")


def format_youtube_mention(handle: str) -> str:
    """Return a canonical single-@ mention from a platform-supplied handle."""
    return "@" + (handle or "").strip().lstrip("@").strip()


def normalize_youtube_mentions(text: str) -> str:
    """Collapse duplicate handle prefixes without touching ordinary email text."""
    return _DUPLICATE_MENTION.sub(r"@\1", text or "")
