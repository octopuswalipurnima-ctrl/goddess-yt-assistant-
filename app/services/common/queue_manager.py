import asyncio
import time
from enum import IntEnum

class Priority(IntEnum):
    HIGH = 1    # Critical: Mod Actions, Timeouts, Bans, Deletions
    NORMAL = 2  # Standard: Reading live chat messages
    LOW = 3     # Passive: AI Generation, Giveaway Reminders

class APIQueueManager:
    def __init__(self, max_concurrent: int = 5):
        self.queue = asyncio.PriorityQueue()
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def execute(self, priority: Priority, func, *args, **kwargs):
        """Places a task in the priority queue and awaits its completion."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        
        # Tuple format: (Priority Level, Timestamp (for tie-breakers), Future, Function, Args, Kwargs)
        item = (priority.value, time.time(), future, func, args, kwargs)
        await self.queue.put(item)
        
        # Spin up a worker to process this task
        asyncio.create_task(self._process_next())
        
        # Return the result back to whoever asked for it
        return await future

    async def _process_next(self):
        """Worker that grabs the highest priority item and executes it safely."""
        async with self.semaphore:
            try:
                priority_val, _, future, func, args, kwargs = await self.queue.get()
                if not future.cancelled():
                    try:
                        result = await func(*args, **kwargs)
                        future.set_result(result)
                    except Exception as e:
                        future.set_exception(e)
            finally:
                self.queue.task_done()