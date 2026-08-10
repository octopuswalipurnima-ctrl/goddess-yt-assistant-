import asyncio
import time

class TokenBucketRateLimiter:
    def __init__(self, capacity: int, refill_rate_per_second: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate_per_second
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int = 1):
        """Waits until enough API quota/tokens are available to proceed."""
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= amount:
                    self.tokens -= amount
                    return True
                
                # If we are out of tokens, calculate exactly how long to wait
                deficit = amount - self.tokens
                wait_time = deficit / self.refill_rate
                await asyncio.sleep(wait_time)

    def _refill(self):
        """Quietly generates new tokens in the background based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        added_tokens = elapsed * self.refill_rate
        if added_tokens > 0:
            self.tokens = min(self.capacity, self.tokens + added_tokens)
            self.last_refill = now