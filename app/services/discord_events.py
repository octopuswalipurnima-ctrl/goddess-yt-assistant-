"""Best-effort Discord event delivery. Never awaited by YouTube processing."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("goddess_stream_manager")

@dataclass(frozen=True)
class DiscordEvent:
    title: str
    body: str
    channel_id: Optional[str] = None


class DiscordEventLogger:
    def __init__(self, max_queue: int = 200):
        self.queue: asyncio.Queue[DiscordEvent] = asyncio.Queue(maxsize=max_queue)
        self.client = None
        self._worker: Optional[asyncio.Task] = None

    def configure(self, client) -> None:
        self.client = client
        if not self._worker or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="discord-event-logger")

    def emit(self, title: str, body: str, channel_id: Optional[str] = None) -> None:
        """Non-blocking and intentionally lossy under sustained Discord outage."""
        try:
            self.queue.put_nowait(DiscordEvent(title[:256], body[:1800], channel_id))
        except asyncio.QueueFull:
            logger.warning("Discord event queue full; dropping event")

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                if not self.client or not self.client.is_ready():
                    continue
                channel_id = event.channel_id
                if not channel_id:
                    from app.utils.config import Config
                    channel_id = Config.DISCORD_LOG_CHANNEL_ID
                if not channel_id:
                    continue
                channel = self.client.get_channel(int(channel_id)) or await self.client.fetch_channel(int(channel_id))
                await channel.send(f"**{event.title}**\n{event.body}", allowed_mentions=None)
                await asyncio.sleep(0.75)  # stays below Discord's normal per-channel send rate
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Discord event delivery failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            try: await self._worker
            except asyncio.CancelledError: pass


discord_events = DiscordEventLogger()
