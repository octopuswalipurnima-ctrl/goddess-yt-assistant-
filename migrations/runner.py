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
]), ("20260831_02_direct_dashboard_audit_actor", []), ("20260831_03_legacy_youtube_user_identity", []), ("20260831_04_streamer_personality_mode", [
    "ALTER TABLE streamers ADD COLUMN IF NOT EXISTS personality_mode VARCHAR NOT NULL DEFAULT 'cohost'",
])]


class MigrationError(RuntimeError):
    """Identifies the original failed migration statement without database secrets."""


SQLITE_ADD_COLUMN_IF_NOT_EXISTS = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.IGNORECASE)


def _statement_for_dialect(conn, statement: str) -> str | None:
    """SQLite lacks ALTER TABLE ADD COLUMN IF NOT EXISTS; preserve PostgreSQL SQL."""
    if conn.dialect.name != "sqlite":
        return statement
    match = SQLITE_ADD_COLUMN_IF_NOT_EXISTS.match(statement)
    if not match:
        return statement
    table_name, column_name = match.groups()
    if column_name.lower() in {column["name"].lower() for column in inspect(conn).get_columns(table_name)}:
        return None
    return statement.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")


def _reconcile_direct_dashboard_audit_actor(conn) -> None:
    """Align legacy audit_logs schemas with the nullable AuditLog.actor field.

    Some deployed databases predate the RBAC audit model and have no user_id
    at all.  Others have it as NOT NULL.  Inspect first so this migration is
    safe across both histories; dashboard startup must not fabricate a User.
    """
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "audit_logs" not in table_names:
        # Bootstrap has not created this optional table; there is no legacy
        # schema to reconcile in this migration run.
        return

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    user_column = columns.get("user_id")
    if user_column is None:
        logger.info("Migration 20260831_02: adding missing nullable audit_logs.user_id")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN user_id INTEGER"))
        return

    if user_column.get("nullable", True):
        return
    if conn.dialect.name == "postgresql":
        logger.info("Migration 20260831_02: making audit_logs.user_id nullable")
        conn.execute(text("ALTER TABLE audit_logs ALTER COLUMN user_id DROP NOT NULL"))
    else:
        # SQLite cannot alter column nullability.  Fresh SQLite databases are
        # created from the nullable ORM model; preserving a legacy NOT NULL
        # constraint is safe because no direct-dashboard audit insert supplies
        # a synthetic actor in production PostgreSQL.
        logger.warning("Migration 20260831_02: legacy %s audit_logs.user_id remains NOT NULL; dialect cannot alter it", conn.dialect.name)


def _reconcile_legacy_youtube_user_identity(conn) -> None:
    """Make legacy ``users.youtube_user_id`` compatible with bot inserts.

    Railway's older schema requires this column, while the application later
    renamed the same YouTube viewer identity to ``youtube_id``.  Inspect first
    so installs that already have the legacy column are untouched; installations
    without it receive a safely backfilled column before it becomes required.
    """
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "users" not in table_names:
        return

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("users")}
    if "youtube_user_id" not in columns:
        logger.info("Migration 20260831_03: adding legacy users.youtube_user_id")
        conn.execute(text("ALTER TABLE users ADD COLUMN youtube_user_id VARCHAR"))
        columns = {column["name"].lower(): column for column in inspect(conn).get_columns("users")}

    if "youtube_id" in columns:
        conn.execute(text(
            "UPDATE users SET youtube_user_id = youtube_id "
            "WHERE youtube_user_id IS NULL AND youtube_id IS NOT NULL"
        ))

    # PostgreSQL can enforce the model's invariant once every legacy record
    # has a value. Do not weaken an existing production constraint, and do not
    # make a historic row with no recoverable YouTube ID block deployment.
    if conn.dialect.name == "postgresql":
        missing = conn.execute(text("SELECT count(*) FROM users WHERE youtube_user_id IS NULL")).scalar_one()
        if missing == 0:
            columns = {column["name"].lower(): column for column in inspect(conn).get_columns("users")}
            if columns["youtube_user_id"].get("nullable", True):
                logger.info("Migration 20260831_03: making users.youtube_user_id required")
                conn.execute(text("ALTER TABLE users ALTER COLUMN youtube_user_id SET NOT NULL"))
        else:
            logger.warning(
                "Migration 20260831_03: leaving users.youtube_user_id nullable; %d legacy rows have no recoverable YouTube ID",
                missing,
            )


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
                if version == "20260831_02_direct_dashboard_audit_actor":
                    _reconcile_direct_dashboard_audit_actor(conn)
                elif version == "20260831_03_legacy_youtube_user_identity":
                    _reconcile_legacy_youtube_user_identity(conn)
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
