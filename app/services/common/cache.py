import time

class MemoryCache:
    def __init__(self):
        self._cache = {}

    def set(self, key: str, value, ttl_seconds: int = 300):
        """Saves a value to memory for a specific amount of time."""
        expire_at = time.time() + ttl_seconds
        self._cache[key] = {"value": value, "expire_at": expire_at}

    def get(self, key: str):
        """Retrieves a value if it exists and hasn't expired."""
        item = self._cache.get(key)
        if not item:
            return None
            
        if time.time() > item["expire_at"]:
            del self._cache[key]
            return None
            
        return item["value"]

    def delete(self, key: str):
        """Forces an item to be deleted from memory."""
        if key in self._cache:
            del self._cache[key]

    def clear(self):
        """Wipes the entire cache."""
        self._cache.clear()

# Global instance to be shared across the bot
global_cache = MemoryCache()