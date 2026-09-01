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
        self.assertIn("20260831_03_legacy_youtube_user_identity", self.versions())
        self.assertIn("20260831_05_audit_streamer_scope", self.versions())
        self.assertIn("20260831_06_streamer_persona_enabled", self.versions())
        self.assertIn("20260831_07_audit_log_schema_complete", self.versions())
        self.assertIn("20260831_08_youtube_daily_usage_window", self.versions())
        self.assertIn("20260831_09_audit_logs_channel_identity", self.versions())
        self.assertIn("20260831_10_audit_logs_nullable_user_id", self.versions())
        self.assertIn("20260831_11_audit_logs_actor_user_id_compat", self.versions())
        self.assertIn("emergency_reason", {column["name"] for column in inspect(self.engine).get_columns("system_state")})
        self.assertIn("youtube_id", {column["name"] for column in inspect(self.engine).get_columns("users")})
        self.assertTrue({"first_seen", "last_seen"}.issubset({column["name"] for column in inspect(self.engine).get_columns("users")}))
        self.assertIn("channel_id", {column["name"] for column in inspect(self.engine).get_columns("users")})
        self.assertIn("youtube_api_window_date", {column["name"] for column in inspect(self.engine).get_columns("system_state")})
        self.assertIn("youtube_user_id", {column["name"] for column in inspect(self.engine).get_columns("users")})
        self.assertIn("actor_user_id", {column["name"] for column in inspect(self.engine).get_columns("audit_logs")})
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

    def test_legacy_audit_table_gains_streamer_scope_without_losing_history(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE streamers (id INTEGER PRIMARY KEY, personality_mode VARCHAR)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, first_seen TIMESTAMP, last_seen TIMESTAMP)"))
            conn.execute(text("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, user_id INTEGER, action VARCHAR NOT NULL, details VARCHAR, timestamp TIMESTAMP)"))
            conn.execute(text("INSERT INTO audit_logs(id, action) VALUES (1, 'LEGACY_EVENT')"))
        run(self.engine, bootstrap=False)
        columns = {column["name"]: column for column in inspect(self.engine).get_columns("audit_logs")}
        self.assertIn("streamer_id", columns)
        self.assertTrue(columns["streamer_id"]["nullable"])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT action FROM audit_logs WHERE id = 1")).scalar_one(), "LEGACY_EVENT")

    def test_incomplete_legacy_audit_table_gets_action_and_write_columns(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE streamers (id INTEGER PRIMARY KEY, personality_mode VARCHAR)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)"))
            conn.execute(text("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, streamer_id INTEGER, timestamp TIMESTAMP)"))
            conn.execute(text("INSERT INTO audit_logs(id, streamer_id) VALUES (1, 1)"))
        run(self.engine, bootstrap=False)
        columns = {column["name"]: column for column in inspect(self.engine).get_columns("audit_logs")}
        self.assertTrue({"streamer_id", "channel_id", "user_id", "actor_user_id", "action", "details", "timestamp"}.issubset(columns))
        self.assertFalse(columns["action"]["nullable"])
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO audit_logs(streamer_id, channel_id, user_id, actor_user_id, action, details) "
                "VALUES (1, 'UCtest', NULL, NULL, 'YOUTUBE_CHANNEL_SET', 'UCtest')"
            ))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT action FROM audit_logs WHERE id = 1")).scalar_one(), "LEGACY_EVENT")
            self.assertEqual(conn.execute(text("SELECT action FROM audit_logs WHERE action = 'YOUTUBE_CHANNEL_SET'")).scalar_one(), "YOUTUBE_CHANNEL_SET")

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
            self.assertEqual(viewer.youtube_user_id, "UC-real-viewer")
        finally:
            db.close()

    def test_legacy_youtube_user_identity_is_backfilled(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, youtube_id VARCHAR, "
                "first_seen TIMESTAMP, last_seen TIMESTAMP)"
            ))
            conn.execute(text("INSERT INTO users(id, username, youtube_id) VALUES (1, 'viewer', 'UC-viewer')"))
        run(self.engine, bootstrap=False)
        with self.engine.connect() as conn:
            value = conn.execute(text("SELECT youtube_user_id FROM users WHERE id = 1")).scalar_one()
        self.assertEqual(value, "UC-viewer")

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

    def test_legacy_audit_table_reconciles_varchar_actor_user_id(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
            conn.execute(text("CREATE TABLE streamers (id INTEGER PRIMARY KEY, personality_mode VARCHAR)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, channel_id VARCHAR, youtube_id VARCHAR, youtube_user_id VARCHAR, first_seen TIMESTAMP, last_seen TIMESTAMP)"))
            conn.execute(text("INSERT INTO users(id, username, channel_id, youtube_id, youtube_user_id) VALUES (1, 'viewer_one', 'UC-1', 'UC-1', 'UC-1')"))
            conn.execute(text("INSERT INTO users(id, username, channel_id, youtube_id, youtube_user_id) VALUES (2, 'viewer_two', 'UC-2', 'UC-2', 'UC-2')"))
            conn.execute(text(
                "CREATE TABLE audit_logs ("
                "id INTEGER PRIMARY KEY, "
                "streamer_id INTEGER, "
                "user_id INTEGER, "
                "actor_user_id VARCHAR, "
                "action VARCHAR NOT NULL, "
                "details VARCHAR, "
                "timestamp TIMESTAMP"
                ")"
            ))
            # 1. Numeric string matching user 1
            conn.execute(text("INSERT INTO audit_logs(id, streamer_id, user_id, actor_user_id, action) VALUES (1, 1, NULL, '1', 'ACTION_1')"))
            # 2. Text channel_id string matching user 2
            conn.execute(text("INSERT INTO audit_logs(id, streamer_id, user_id, actor_user_id, action) VALUES (2, 1, NULL, 'UC-2', 'ACTION_2')"))
            # 3. Non-matching external string (should stay NULL user_id)
            conn.execute(text("INSERT INTO audit_logs(id, streamer_id, user_id, actor_user_id, action) VALUES (3, 1, NULL, 'system_boot', 'ACTION_3')"))
            # 4. Existing integer user_id with NULL actor_user_id
            conn.execute(text("INSERT INTO audit_logs(id, streamer_id, user_id, actor_user_id, action) VALUES (4, 1, 2, NULL, 'ACTION_4')"))
        run(self.engine, bootstrap=False)
        with self.engine.connect() as conn:
            row1 = conn.execute(text("SELECT user_id, actor_user_id FROM audit_logs WHERE id = 1")).one()
            self.assertEqual(row1[0], 1)
            self.assertEqual(row1[1], "1")

            row2 = conn.execute(text("SELECT user_id, actor_user_id FROM audit_logs WHERE id = 2")).one()
            self.assertEqual(row2[0], 2)
            self.assertEqual(row2[1], "UC-2")

            row3 = conn.execute(text("SELECT user_id, actor_user_id FROM audit_logs WHERE id = 3")).one()
            self.assertIsNone(row3[0])
            self.assertEqual(row3[1], "system_boot")

            row4 = conn.execute(text("SELECT user_id, actor_user_id FROM audit_logs WHERE id = 4")).one()
            self.assertEqual(row4[0], 2)
            self.assertEqual(row4[1], "2")

