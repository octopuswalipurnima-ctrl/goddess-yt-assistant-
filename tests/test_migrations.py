import unittest

from sqlalchemy import create_engine, inspect, text

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
        self.assertIn("emergency_reason", {column["name"] for column in inspect(self.engine).get_columns("system_state")})
        run(self.engine)
        self.assertEqual(self.versions().count("20260830_01_emergency_stop"), 1)

    def test_existing_and_partially_changed_system_state_complete(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_state (id INTEGER PRIMARY KEY, emergency_stop BOOLEAN NOT NULL DEFAULT 0)"))
        run(self.engine, bootstrap=False)
        columns = {column["name"] for column in inspect(self.engine).get_columns("system_state")}
        self.assertIn("emergency_stop", columns)
        self.assertIn("emergency_reason", columns)

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
