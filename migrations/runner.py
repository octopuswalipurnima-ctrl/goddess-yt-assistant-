"""Small, dependency-free, append-only migration runner."""
from sqlalchemy import text
from app.database.connection import engine, init_db

MIGRATIONS = [("20260830_01_emergency_stop", [
    "ALTER TABLE system_state ADD COLUMN emergency_stop BOOLEAN NOT NULL DEFAULT 0",
    "ALTER TABLE system_state ADD COLUMN emergency_reason VARCHAR",
])]

def run() -> None:
    # Bootstrap only creates a fresh baseline; subsequent changes are tracked below.
    init_db()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) PRIMARY KEY)"))
        applied = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}
        for version, statements in MIGRATIONS:
            if version in applied:
                continue
            for statement in statements:
                try:
                    conn.execute(text(statement))
                except Exception as exc:
                    # SQLite and some managed databases report duplicate-column differently.
                    if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower():
                        raise
            conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})

if __name__ == "__main__":
    run()
