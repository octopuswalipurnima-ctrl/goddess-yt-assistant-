"""Small, append-only migration runner with per-migration transactions."""
import logging
import re
from typing import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.database.connection import Base, engine, init_db

logger = logging.getLogger("goddess_stream_manager")

# This migration was released but never marked applied: the previous runner
# created these model columns during bootstrap, then tried to add them again.
MIGRATIONS = [("20260830_01_emergency_stop", [
    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emergency_stop BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emergency_reason VARCHAR",
]), ("20260830_02_user_youtube_identity", [
    # Older deployments created `users` before the bot started keeping the
    # stable YouTube author identifier.  Keep this nullable: historic rows do
    # not necessarily have a recoverable YouTube ID.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS youtube_id VARCHAR",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_youtube_id ON users (youtube_id)",
]), ("20260830_03_user_timestamps", [
    # Legacy user rows predate the audit timestamps declared by User.  The
    # default preserves the model's server-side first_seen behavior.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP WITH TIME ZONE",
]), ("20260831_01_user_channel_identity", [
    # Production's legacy schema requires `channel_id`; map it explicitly so
    # ORM inserts for real YouTube viewers satisfy that constraint.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS channel_id VARCHAR",
    "UPDATE users SET channel_id = youtube_id WHERE channel_id IS NULL AND youtube_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_channel_id ON users (channel_id)",
]), ("20260831_02_direct_dashboard_audit_actor", [
    # Dashboard direct-entry has no authenticated web user.  Preserve audit
    # records while allowing their actor to be absent rather than synthetic.
    "ALTER TABLE audit_logs ALTER COLUMN user_id DROP NOT NULL",
])]


class MigrationError(RuntimeError):
    """Identifies the original failed migration statement without database secrets."""


SQLITE_ADD_COLUMN_IF_NOT_EXISTS = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.IGNORECASE)


def _statement_for_dialect(conn, statement: str) -> str | None:
    """SQLite lacks ALTER TABLE ADD COLUMN IF NOT EXISTS; preserve PostgreSQL SQL."""
    if conn.dialect.name != "sqlite":
        return statement
    if statement == "ALTER TABLE audit_logs ALTER COLUMN user_id DROP NOT NULL":
        # SQLite does not support ALTER COLUMN and has no equivalent needed for
        # the test schema, where this column is created nullable by the model.
        return None
    match = SQLITE_ADD_COLUMN_IF_NOT_EXISTS.match(statement)
    if not match:
        return statement
    table_name, column_name = match.groups()
    if column_name.lower() in {column["name"].lower() for column in inspect(conn).get_columns(table_name)}:
        return None
    return statement.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")


def _bootstrap(target_engine: Engine) -> None:
    # Register mapped tables before create_all when the runner is invoked
    # directly (rather than through the application import path).
    from app.database import models  # noqa: F401
    if target_engine is engine:
        init_db()
    else:
        Base.metadata.create_all(bind=target_engine)


def _apply_migrations(target_engine: Engine, migrations: Iterable[tuple[str, list[str]]]) -> None:
    # Version tracking is committed independently from each migration body.
    with target_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) PRIMARY KEY)"))

    for version, statements in migrations:
        with target_engine.connect() as conn:
            applied = conn.execute(text("SELECT 1 FROM schema_migrations WHERE version = :version"), {"version": version}).first()
        if applied:
            continue

        # A failed statement rolls back this entire migration and prevents its
        # version from being recorded. Later migrations are never attempted.
        try:
            with target_engine.begin() as conn:
                for statement_index, statement in enumerate(statements, start=1):
                    try:
                        executable_statement = _statement_for_dialect(conn, statement)
                        if executable_statement:
                            conn.execute(text(executable_statement))
                    except Exception as exc:
                        logger.error("Migration %s statement %d failed: %s", version, statement_index, statement)
                        raise MigrationError(f"Migration {version} statement {statement_index} failed") from exc
                conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})
        except MigrationError:
            raise


def run(target_engine: Engine | None = None, *, bootstrap: bool = True, migrations=None) -> None:
    """Run migrations; injectable arguments are used only by offline tests."""
    target_engine = target_engine or engine
    if bootstrap:
        _bootstrap(target_engine)
    _apply_migrations(target_engine, MIGRATIONS if migrations is None else migrations)


if __name__ == "__main__":
    run()
