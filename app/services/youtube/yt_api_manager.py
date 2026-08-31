import asyncio
import logging
import time
from typing import Optional, Dict, Any, Tuple
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from app.utils.config import Config
from app.services.common.cache import global_cache
from app.services.common.rate_limiter import TokenBucketRateLimiter
from app.services.common.queue_manager import APIQueueManager, Priority

logger = logging.getLogger("goddess_stream_manager")

class YouTubeAPIManager:
    def __init__(self):
        self.credentials = Credentials(
            token=None,
            refresh_token=Config.YOUTUBE_REFRESH_TOKEN,
            client_id=Config.YOUTUBE_CLIENT_ID,
            client_secret=Config.YOUTUBE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        # Token bucket: Allows up to 10 bursts, refills at 1 token per second
        self.rate_limiter = TokenBucketRateLimiter(capacity=10, refill_rate_per_second=1.0)
        # Priority Queue Manager
        self.queue_manager = APIQueueManager(max_concurrent=3)
        # Stable lookups can be requested by more than one stream worker at
        # startup.  Share one in-flight result instead of spending quota twice.
        self._inflight_requests: Dict[str, asyncio.Task] = {}
        self._quota_exhausted_until = 0.0
        self._last_quota_log_at = 0.0

    def _quota_available(self) -> bool:
        return time.time() >= self._quota_exhausted_until

    def _mark_quota_exhausted(self, error: Exception) -> bool:
        """Open a quiet circuit breaker only for YouTube's quota response."""
        if "quotaexceeded" not in str(error).lower():
            return False
        # Daily quota does not recover quickly. Avoid retry storms while still
        # allowing a later process restart or the next quota window to recover.
        self._quota_exhausted_until = max(self._quota_exhausted_until, time.time() + 6 * 60 * 60)
        if time.time() - self._last_quota_log_at >= 300:
            self._last_quota_log_at = time.time()
            logger.warning("[YT API MANAGER] YouTube quota exhausted; nonessential requests are paused.")
        return True

    async def _deduplicated(self, key: str, operation):
        """Reuse a concurrent stable request and always release its key."""
        task = self._inflight_requests.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            self._inflight_requests[key] = task
        try:
            return await task
        finally:
            if self._inflight_requests.get(key) is task and task.done():
                self._inflight_requests.pop(key, None)

    def _get_service(self):
        """Constructs an un-cached, thread-safe Google API Service Client."""
        return build('youtube', 'v3', credentials=self.credentials, cache_discovery=False)

    # ---------------------------------------------------------
    # 1. STREAM & CHAT ID DETECTION (CACHED)
    # ---------------------------------------------------------
    async def get_chat_from_video(self, video_id: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetches channelId and activeLiveChatId for a video.
        CACHED: Results are stored in memory for 2 hours to avoid wasting quota on duplicates.
        """
        cache_key = f"yt_video_chat:{video_id}"
        cached_result = global_cache.get(cache_key)
        if cached_result:
            return cached_result["channel_id"], cached_result["chat_id"]
        if not self._quota_available():
            return None, None

        async def _raw_fetch():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            res = service.videos().list(part="snippet,liveStreamingDetails", id=video_id).execute()
            items = res.get("items", [])
            if not items:
                return None, None
            
            item = items[0]
            channel_id = item["snippet"]["channelId"]
            chat_id = item.get("liveStreamingDetails", {}).get("activeLiveChatId")
            return channel_id, chat_id, {
                "channel_name": item["snippet"].get("channelTitle"),
                "stream_title": item["snippet"].get("title"),
            }

        async def _fetch_and_cache():
            channel_id, chat_id, metadata = await self.queue_manager.execute(Priority.NORMAL, _raw_fetch)
            if channel_id and chat_id:
                # Cache valid stream IDs for 2 hours (7200 seconds)
                global_cache.set(cache_key, {"channel_id": channel_id, "chat_id": chat_id, **metadata}, ttl_seconds=7200)
            return channel_id, chat_id

        try:
            return await self._deduplicated(cache_key, _fetch_and_cache)
        except Exception as e:
            self._mark_quota_exhausted(e)
            logger.error(f"[YT API MANAGER] Error fetching stream info for video {video_id}: {e}")
            return None, None

    def stream_metadata(self, video_id: str) -> Dict[str, Any]:
        """Return metadata captured with the current video, never another stream."""
        cached = global_cache.get(f"yt_video_chat:{video_id}") or {}
        return {
            "channel_name": cached.get("channel_name"),
            "stream_title": cached.get("stream_title"),
        }

    async def get_bot_identity(self) -> Dict[str, Optional[str]]:
        """Read the authenticated YouTube account once and cache it briefly."""
        cache_key = "yt_authenticated_bot_identity"
        cached = global_cache.get(cache_key)
        if cached:
            return cached
        if not self._quota_available():
            return {"bot_name": None, "bot_handle": None}

        async def _raw_fetch():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            items = service.channels().list(part="snippet", mine=True).execute().get("items", [])
            snippet = items[0].get("snippet", {}) if items else {}
            return {"bot_name": snippet.get("title"), "bot_handle": snippet.get("customUrl")}

        try:
            identity = await self._deduplicated(cache_key, lambda: self.queue_manager.execute(Priority.LOW, _raw_fetch))
        except Exception as exc:
            self._mark_quota_exhausted(exc)
            logger.warning("[YT IDENTITY] Unable to load bot identity type=%s", type(exc).__name__)
            identity = {"bot_name": None, "bot_handle": None}
        global_cache.set(cache_key, identity, ttl_seconds=600)
        return identity

    # ---------------------------------------------------------
    # 2. CHAT POLLED READING (NORMAL PRIORITY)
    # ---------------------------------------------------------
    async def get_live_chat_messages(self, live_chat_id: str, page_token: Optional[str] = None) -> Dict[str, Any]:
        """
        Reads messages from YouTube Live Chat using the queue and rate limiter.
        """
        if not self._quota_available():
            return {"items": [], "pollingIntervalMillis": 60000, "quota_exhausted": True}

        async def _raw_list():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            return service.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=page_token
            ).execute()

        try:
            return await self.queue_manager.execute(Priority.NORMAL, _raw_list)
        except Exception as e:
            self._mark_quota_exhausted(e)
            logger.error(f"[YT API MANAGER] Chat list error on {live_chat_id}: {e}")
            raise e

    # ---------------------------------------------------------
    # 3. SEND CHAT MESSAGE (HIGH PRIORITY - 200 UNITS)
    # ---------------------------------------------------------
    async def send_chat_message(self, live_chat_id: str, text: str) -> Optional[Dict[str, Any]]:
        """
        Posts a text message into YouTube live chat. High Priority queue item.
        """
        if not live_chat_id or not text:
            return None
        if not self._quota_available():
            return None

        async def _raw_send():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            return service.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": text}
                    }
                }
            ).execute()

        try:
            return await self.queue_manager.execute(Priority.HIGH, _raw_send)
        except Exception as e:
            self._mark_quota_exhausted(e)
            logger.error(f"[YT API MANAGER] Error sending message to {live_chat_id}: {e}")
            return None

    # ---------------------------------------------------------
    # 4. MODERATION ACTIONS (HIGH PRIORITY - 200 UNITS)
    # ---------------------------------------------------------
    async def delete_chat_message(self, message_id: str) -> bool:
        """Deletes a message from live chat."""
        if not message_id:
            return False
        if not self._quota_available():
            return False

        async def _raw_delete():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            service.liveChatMessages().delete(id=message_id).execute()
            return True

        try:
            return await self.queue_manager.execute(Priority.HIGH, _raw_delete)
        except Exception as e:
            self._mark_quota_exhausted(e)
            logger.error(f"[YT API MANAGER] Error deleting message {message_id}: {e}")
            return False

    async def ban_or_timeout_user(
        self, 
        live_chat_id: str, 
        channel_id: str, 
        duration_seconds: int = 300, 
        is_permanent: bool = False
    ) -> bool:
        """Issues a timeout or permanent ban to a user."""
        if not live_chat_id or not channel_id:
            return False
        if not self._quota_available():
            return False

        ban_type = "permanent" if is_permanent else "temporary"
        body_data = {
            "snippet": {
                "liveChatId": live_chat_id,
                "type": ban_type,
                "bannedUserDetails": {"channelId": channel_id}
            }
        }
        if not is_permanent:
            body_data["snippet"]["temporaryBanDurationMinutes"] = max(1, int(duration_seconds / 60))

        async def _raw_ban():
            await self.rate_limiter.acquire(1)
            service = self._get_service()
            service.liveChatBans().insert(part="snippet", body=body_data).execute()
            return True

        try:
            return await self.queue_manager.execute(Priority.HIGH, _raw_ban)
        except Exception as e:
            self._mark_quota_exhausted(e)
            logger.error(f"[YT API MANAGER] Error issuing {ban_type} ban to {channel_id}: {e}")
            return False

# Global instance
yt_api_manager = YouTubeAPIManager()
