import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database.connection as connection


class DashboardDirectEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other test modules may import the production connection first.  Give
        # this web-entry test its own isolated, in-memory application database.
        connection.engine.dispose()
        connection.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        connection.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=connection.engine)
        connection.init_db()
        from main import app
        cls.app = app

    def setUp(self):
        self.client = TestClient(self.app, base_url="https://testserver")

    def test_dashboard_opens_without_login_or_credentials(self):
        root = self.client.get("/", follow_redirects=False)
        dashboard = TestClient(self.app, base_url="https://testserver").get("/dashboard", follow_redirects=False)
        self.assertEqual(root.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Goddess AI", root.text)
        self.assertNotIn("Sign in", root.text)
        self.assertNotIn("Log in", root.text)
        from app.database.models import User
        db = connection.SessionLocal()
        try:
            self.assertIsNone(db.query(User).filter_by(youtube_id="dashboard-direct-entry").first())
        finally:
            db.close()
        self.assertEqual(self.client.get("/login", follow_redirects=False).status_code, 404)
        self.assertEqual(self.client.get("/auth", follow_redirects=False).status_code, 404)

    def test_channel_is_persisted_and_websub_uses_existing_helper(self):
        self.client.get("/")
        channel_id = "UC" + "a" * 22
        saved = self.client.post("/api/websub/channel", data={"youtube_channel_id": f"  {channel_id}  "}, follow_redirects=False)
        self.assertEqual(saved.headers["location"], "/?success=channel_saved")
        from app.database.connection import SessionLocal
        from app.database.models import AuditLog, Streamer
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Streamer).order_by(Streamer.id.asc()).first().youtube_channel_id, channel_id)
            audit = db.query(AuditLog).filter_by(action="YOUTUBE_CHANNEL_SET").one()
            self.assertEqual(audit.details, channel_id)
            self.assertIsNone(audit.user_id)
        finally:
            db.close()
        with patch("main.subscribe_websub", return_value=True) as subscribe:
            enabled = self.client.post("/api/websub/enable", follow_redirects=False)
        self.assertEqual(enabled.headers["location"], "/?success=websub_subscribed")
        subscribe.assert_called_once_with(channel_id, "subscribe")

    def test_manual_channel_validation_and_topic(self):
        from main import YOUTUBE_CHANNEL_ID_RE, websub_topic
        channel_id = "UC" + "a" * 22
        self.assertTrue(YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id))
        self.assertFalse(YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id + " extra"))
        self.assertEqual(websub_topic(channel_id), f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}")

    def test_monitored_channel_registration_is_idempotent(self):
        from app.database.connection import SessionLocal
        from app.database.models import AuditLog, Streamer
        from app.services.youtube.monitored_channels import MONITORED_CHANNEL_IDS, ensure_monitored_channels
        db = SessionLocal()
        try:
            ensure_monitored_channels(db)
            ensure_monitored_channels(db)
            self.assertEqual(
                db.query(Streamer).filter(Streamer.youtube_channel_id.in_(MONITORED_CHANNEL_IDS)).count(),
                len(MONITORED_CHANNEL_IDS),
            )
            logs = db.query(AuditLog).filter(AuditLog.action == "MONITORED_CHANNEL_REGISTERED").all()
            self.assertEqual(len(logs), len(MONITORED_CHANNEL_IDS))
            self.assertTrue(all(log.channel_id in MONITORED_CHANNEL_IDS for log in logs))
            self.assertTrue(all(log.user_id is None for log in logs))
            self.assertTrue(all(log.actor_user_id is None for log in logs))
        finally:
            db.close()

    def test_websub_live_channel_queues_existing_monitor_once(self):
        import main
        from app.bot.youtube_chat import DETECTED_VIDEOS, DISCONNECT_QUEUE
        from app.database.connection import SessionLocal
        from app.database.models import AuditLog, Streamer
        from app.services.youtube.monitored_channels import MONITORED_CHANNEL_IDS, ensure_monitored_channels
        channel_id, video_id = MONITORED_CHANNEL_IDS[0], "abcdefghijk"
        xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f'<entry><yt:videoId>{video_id}</yt:videoId><yt:channelId>{channel_id}</yt:channelId></entry></feed>'
        )
        db = SessionLocal()
        try:
            ensure_monitored_channels(db)
            streamer = db.query(Streamer).filter_by(youtube_channel_id=channel_id).one()
        finally:
            db.close()
        DETECTED_VIDEOS.clear(); DISCONNECT_QUEUE.clear()
        resolved = {"video_id": video_id, "channel_id": channel_id, "chat_id": "live-chat", "channel_name": None, "stream_title": None}
        with patch.object(main.yt_api_manager, "resolve_live_broadcast", new=AsyncMock(return_value=resolved)) as resolve, patch.object(main.yt_api_manager, "invalidate_live_video"):
            self.assertEqual(self.client.post("/api/youtube-webhook", content=xml).status_code, 204)
            self.assertEqual(self.client.post("/api/youtube-webhook", content=xml).status_code, 204)
        self.assertEqual(DETECTED_VIDEOS[video_id], streamer.effective_id)
        self.assertEqual(resolve.await_count, 2)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(AuditLog).filter_by(action="WEBSUB_LIVE_SESSION_QUEUED", details=video_id).count(), 1)
        finally:
            db.close()

    def test_websub_ignores_unmonitored_or_not_live_notifications(self):
        import main
        from app.bot.youtube_chat import DETECTED_VIDEOS, DISCONNECT_QUEUE
        from app.services.youtube.monitored_channels import MONITORED_CHANNEL_IDS
        video_id = "klmnopqrst0"
        xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f'<entry><yt:videoId>{video_id}</yt:videoId><yt:channelId>UCxxxxxxxxxxxxxxxxxxxxxx</yt:channelId></entry></feed>'
        )
        DETECTED_VIDEOS.clear(); DISCONNECT_QUEUE.clear()
        with patch.object(main.yt_api_manager, "resolve_live_broadcast", new=AsyncMock()) as resolve:
            self.assertEqual(self.client.post("/api/youtube-webhook", content=xml).status_code, 204)
        resolve.assert_not_awaited()

        channel_id = MONITORED_CHANNEL_IDS[1]
        xml = xml.replace("UCxxxxxxxxxxxxxxxxxxxxxx", channel_id)
        with patch.object(main.yt_api_manager, "resolve_live_broadcast", new=AsyncMock(return_value=None)), patch.object(main.yt_api_manager, "invalidate_live_video"):
            self.assertEqual(self.client.post("/api/youtube-webhook", content=xml).status_code, 204)
        self.assertNotIn(video_id, DETECTED_VIDEOS)
        self.assertIn(video_id, DISCONNECT_QUEUE)

    def test_startup_seed_uses_and_closes_existing_session_factory(self):
        import main
        async def worker():
            return None
        def discard_task(coroutine):
            coroutine.close()
            return object()
        session = type("Session", (), {"close": lambda self: setattr(self, "closed", True), "closed": False})()
        streamer = type("Streamer", (), {"youtube_channel_id": "UC" + "z" * 22})()
        monitor = type("Monitor", (), {"active_streams": {}, "run": lambda self: worker()})()
        main.running_tasks.clear()
        with patch.object(main, "init_db"), \
             patch.object(main, "SessionLocal", return_value=session) as session_factory, \
             patch.object(main, "ensure_monitored_channels", return_value=[streamer]), \
             patch.object(main, "subscribe_websub"), \
             patch.object(main, "start_scheduler"), \
             patch.object(main, "YouTubeChatMonitor", return_value=monitor), \
             patch.object(main.asyncio, "create_task", side_effect=discard_task), \
             patch.object(main, "start_discord_bot", new=AsyncMock()), \
             patch.object(main, "start_timed_command_loop", new=AsyncMock()), \
             patch.object(main, "websub_renewal_loop", new=AsyncMock()):
            asyncio.run(main.startup_event())
        session_factory.assert_called_once_with()
        self.assertTrue(session.closed)
        self.assertIs(main.app.state.yt_monitor, monitor)
        main.app.state.yt_monitor = None

    def test_startup_event_with_real_database_seeds_and_subscribes(self):
        import main
        from app.services.youtube.monitored_channels import MONITORED_CHANNEL_IDS
        subscribed = []
        def discard_task(coroutine):
            coroutine.close()
            return object()
        main.running_tasks.clear()
        with patch.object(main, "subscribe_websub", side_effect=subscribed.append), \
             patch.object(main, "start_scheduler"), \
             patch.object(main.asyncio, "create_task", side_effect=discard_task), \
             patch.object(main, "start_discord_bot", new=AsyncMock()), \
             patch.object(main, "start_timed_command_loop", new=AsyncMock()), \
             patch.object(main, "websub_renewal_loop", new=AsyncMock()):
            asyncio.run(main.startup_event())
        self.assertEqual(subscribed, list(MONITORED_CHANNEL_IDS))
        main.app.state.yt_monitor = None
