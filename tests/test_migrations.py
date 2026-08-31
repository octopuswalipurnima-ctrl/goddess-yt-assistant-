import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from migrations.runner import MigrationError, run


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

    def tearDown(self):
        self.engine.dispose()

    def versions(self):
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))]

    def test_fresh_database_and_rerun_are_safe(self):
        run(self.engine)
        self.assertIn("20260830_01_emergency_stop", self.versions())
        self.assertIn("20260830_02_user_youtube_identity", self.versions())
        self.assertIn("20260830_03_user_timestamps", self.versions())
        self.assertIn("20260831_01_user_channel_identity", self.versions())
        self.assertIn("20260831_02_direct_dashboard_audit_actor", self.versions())
        self.assertIn("emergency_reason", {column["name"] for column in inspect(self.engine).get_columns("system_state")})
        self.assertIn("youtube_id", {column["name"] for column in inspect(self.engine).get_columns("users")})
        self.assertTrue({"first_seen", "last_seen"}.issubset({column["name"] for column in inspect(self.engine).get_columns("users")}))
        self.assertIn("channel_id", {column["name"] for column in inspect(self.engine).get_columns("users")})
        run(self.engine)
        self.assertEqual(self.versions().count("20260830_01_emergency_stop"), 1)
        self.assertEqual(self.versions().count("20260830_02_user_youtube_identity"), 1)
        self.assertEqual(self.versions().count("20260830_03_user_timestamps"), 1)

    def test_existing_and_partially_changed_system_state_complete(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)"))
        run(self.engine, bootstrap=False)
        columns = {column["name"] for column in inspect(self.engine).get_columns("system_state")}
        self.assertIn("emergency_stop", columns)
        self.assertIn("emergency_reason", columns)

    def test_direct_dashboard_migration_adds_missing_audit_actor_column(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)"))
            conn.execute(text("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, streamer_id INTEGER NOT NULL, action VARCHAR NOT NULL)"))
        run(self.engine, bootstrap=False)
        columns = {column["name"]: column for column in inspect(self.engine).get_columns("audit_logs")}
        self.assertIn("user_id", columns)
        self.assertTrue(columns["user_id"]["nullable"])

    def test_legacy_users_table_gains_youtube_identity(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)"))
        run(self.engine, bootstrap=False)
        columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        self.assertTrue({"youtube_id", "first_seen", "last_seen"}.issubset(columns))
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id, username, youtube_id) VALUES (1, 'one', 'UC-one')"))
            with self.assertRaises(Exception):
                conn.execute(text("INSERT INTO users(id, username, youtube_id) VALUES (2, 'two', 'UC-one')"))

    def test_real_youtube_user_populates_required_legacy_channel_id(self):
        run(self.engine)
        from app.database.models import User
        db = sessionmaker(bind=self.engine)()
        try:
            viewer = User(youtube_id="UC-real-viewer", username="Viewer")
            db.add(viewer); db.commit()
            self.assertEqual(viewer.channel_id, "UC-real-viewer")
        finally:
            db.close()

    def test_failed_migration_rolls_back_and_stops(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE migration_probe (value INTEGER)"))
        broken = [("test_broken", ["INSERT INTO migration_probe(value) VALUES (1)", "INVALID MIGRATION SQL", "INSERT INTO migration_probe(value) VALUES (2)"])]
        with self.assertLogs("goddess_stream_manager", level="ERROR") as logs:
            with self.assertRaisesRegex(MigrationError, "test_broken statement 2"):
                run(self.engine, bootstrap=False, migrations=broken)
        self.assertIn("INVALID MIGRATION SQL", "\n".join(logs.output))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM migration_probe")).scalar_one(), 0)
        self.assertNotIn("test_broken", self.versions())
