import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import unittest
from unittest.mock import patch

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
