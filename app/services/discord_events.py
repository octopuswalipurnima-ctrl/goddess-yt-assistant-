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
    streamer_id: Optional[int] = None


class DiscordEventLogger:
    def __init__(self, max_queue: int = 200):
        self.queue: asyncio.Queue[DiscordEvent] = asyncio.Queue(maxsize=max_queue)
        self.client = None
        self._worker: Optional[asyncio.Task] = None

    def configure(self, client) -> None:
        self.client = client
        if not self._worker or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="discord-event-logger")

    def emit(self, title: str, body: str, channel_id: Optional[str] = None, streamer_id: Optional[int] = None) -> None:
        """Non-blocking and intentionally lossy under sustained Discord outage."""
        try:
            self.queue.put_nowait(DiscordEvent(title[:256], body[:1800], channel_id, streamer_id))
        except asyncio.QueueFull:
            logger.warning("Discord event queue full; dropping event")

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                if not self.client or not self.client.is_ready():
                    continue
                channel_id = event.channel_id
                if not channel_id and event.streamer_id:
                    channel_id = await asyncio.to_thread(self._streamer_channel_id, event.streamer_id)
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

    @staticmethod
    def _streamer_channel_id(streamer_id: int) -> Optional[str]:
        from app.database.connection import SessionLocal
        from app.database.models import Streamer
        db = SessionLocal()
        try:
            streamer = db.query(Streamer).filter_by(id=streamer_id).first()
            return streamer.discord_log_channel_id if streamer else None
        finally:
            db.close()

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            try: await self._worker
            except asyncio.CancelledError: pass


discord_events = DiscordEventLogger()
