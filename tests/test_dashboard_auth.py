import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import bcrypt
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database.connection import init_db
from app.utils.config import Config


class DashboardPasswordAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Config.ADMIN_USERNAME = "operator"
        Config.ADMIN_PASSWORD_HASH = bcrypt.hashpw(b"correct password", bcrypt.gensalt()).decode()
        Config.SESSION_SECRET = "test-session-secret"
        Config.SESSION_HTTPS_ONLY = True
        init_db()
        from main import app
        cls.app = app

    def setUp(self):
        self.client = TestClient(self.app, base_url="https://testserver")

    def test_login_logout_and_dashboard_protection(self):
        anonymous = TestClient(self.client.app, base_url="https://testserver")
        self.assertEqual(anonymous.get("/", follow_redirects=False).headers["location"], "/login")
        self.assertEqual(self.client.get("/login").status_code, 200)
        failed = self.client.post("/login", data={"username": "operator", "password": "wrong"}, follow_redirects=False)
        self.assertEqual(failed.headers["location"], "/login?error=invalid_credentials")
        self.assertNotIn("correct password", failed.text)
        ok = self.client.post("/login", data={"username": "operator", "password": "correct password"}, follow_redirects=False)
        self.assertEqual(ok.headers["location"], "/dashboard")
        self.assertEqual(self.client.get("/dashboard", follow_redirects=True).status_code, 200)
        self.assertEqual(self.client.get("/logout", follow_redirects=False).headers["location"], "/login")

    def test_channel_is_persisted_and_websub_uses_existing_helper(self):
        self.client.post("/login", data={"username": "operator", "password": "correct password"})
        channel_id = "UC" + "a" * 22
        saved = self.client.post("/api/websub/channel", data={"youtube_channel_id": f"  {channel_id}  "}, follow_redirects=False)
        self.assertEqual(saved.headers["location"], "/?success=channel_saved")
        from app.database.connection import SessionLocal
        from app.database.models import Streamer
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Streamer).order_by(Streamer.id.asc()).first().youtube_channel_id, channel_id)
        finally:
            db.close()
        with patch("main.subscribe_websub", return_value=True) as subscribe:
            enabled = self.client.post("/api/websub/enable", follow_redirects=False)
        self.assertEqual(enabled.headers["location"], "/?success=websub_subscribed")
        subscribe.assert_called_once_with(channel_id, "subscribe")
        with patch("main.subscribe_websub", return_value=True) as unsubscribe:
            disabled = self.client.post("/api/websub/disable", follow_redirects=False)
        self.assertEqual(disabled.headers["location"], "/?success=websub_unsubscribed")
        unsubscribe.assert_called_once_with(channel_id, "unsubscribe")

    def test_manual_channel_validation_and_topic(self):
        from main import YOUTUBE_CHANNEL_ID_RE, websub_topic
        channel_id = "UC" + "a" * 22
        self.assertTrue(YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id))
        self.assertFalse(YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id + " extra"))
        self.assertEqual(websub_topic(channel_id), f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}")

    def test_google_dashboard_callback_is_removed(self):
        self.assertEqual(self.client.get("/auth", follow_redirects=False).status_code, 404)
