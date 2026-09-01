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
]), ("20260831_02_direct_dashboard_audit_actor", []), ("20260831_03_legacy_youtube_user_identity", []), ("20260831_04_streamer_personality_mode", []), ("20260831_05_audit_streamer_scope", []), ("20260831_06_streamer_persona_enabled", []), ("20260831_07_audit_log_schema_complete", []), ("20260831_08_youtube_daily_usage_window", []), ("20260831_09_audit_logs_channel_identity", []), ("20260831_10_audit_logs_nullable_user_id", []), ("20260831_11_audit_logs_actor_user_id_compat", [])]


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
    or actor_user_id at all.  Others have them as NOT NULL.  Inspect first so
    this migration is safe across both histories; dashboard startup must not
    fabricate a User.
    """
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "audit_logs" not in table_names:
        # Bootstrap has not created this optional table; there is no legacy
        # schema to reconcile in this migration run.
        return

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    if "user_id" not in columns:
        logger.info("Migration 20260831_02: adding missing nullable audit_logs.user_id")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN user_id INTEGER"))
        columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}

    if "actor_user_id" not in columns:
        logger.info("Migration 20260831_02: adding missing nullable audit_logs.actor_user_id")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN actor_user_id INTEGER"))
        columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}

    if conn.dialect.name == "postgresql":
        logger.info("Migration 20260831_02: ensuring audit_logs actor columns are nullable")
        conn.execute(text("ALTER TABLE audit_logs ALTER COLUMN user_id DROP NOT NULL"))
        conn.execute(text("ALTER TABLE audit_logs ALTER COLUMN actor_user_id DROP NOT NULL"))

    # Synchronize historic values between user_id and actor_user_id
    if "user_id" in columns and "actor_user_id" in columns:
        conn.execute(text(
            "UPDATE audit_logs SET user_id = actor_user_id "
            "WHERE user_id IS NULL AND actor_user_id IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE audit_logs SET actor_user_id = user_id "
            "WHERE actor_user_id IS NULL AND user_id IS NOT NULL"
        ))


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


def _reconcile_audit_streamer_scope(conn) -> None:
    """Restore the stream foreign key expected by the AuditLog model.

    Older Railway databases have audit rows but predate stream-scoped audit
    events. Existing rows have no trustworthy streamer source, so the column
    remains nullable for history while all new ORM writes supply it.
    """
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "audit_logs" not in table_names:
        return

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    if "streamer_id" not in columns:
        logger.info("Migration 20260831_05: adding nullable audit_logs.streamer_id")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN streamer_id INTEGER"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_streamer_id ON audit_logs (streamer_id)"))

    # PostgreSQL can attach the model's real relationship without rejecting
    # historic rows. NOT VALID enforces the key for every future audit write;
    # old NULL/unverifiable rows remain readable and untouched.
    if conn.dialect.name == "postgresql" and "streamers" in table_names:
        exists = conn.execute(text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'fk_audit_logs_streamer_id_streamers'"
        )).first()
        if not exists:
            logger.info("Migration 20260831_05: adding audit_logs.streamer_id foreign key")
            conn.execute(text(
                "ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_streamer_id_streamers "
                "FOREIGN KEY (streamer_id) REFERENCES streamers(id) NOT VALID"
            ))


def _reconcile_streamer_personality_mode(conn) -> None:
    """Add the optional per-stream mode only when the streamer table exists."""
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "streamers" not in table_names:
        return
    columns = {column["name"].lower() for column in inspect(conn).get_columns("streamers")}
    if "personality_mode" not in columns:
        conn.execute(text("ALTER TABLE streamers ADD COLUMN personality_mode VARCHAR NOT NULL DEFAULT 'cohost'"))


def _reconcile_streamer_persona_enabled(conn) -> None:
    """Add the optional persona switch without changing any existing mode."""
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "streamers" not in table_names:
        return
    columns = {column["name"].lower() for column in inspect(conn).get_columns("streamers")}
    if "persona_enabled" not in columns:
        conn.execute(text("ALTER TABLE streamers ADD COLUMN persona_enabled BOOLEAN NOT NULL DEFAULT FALSE"))


def _reconcile_audit_log_schema_complete(conn) -> None:
    """Bring pre-RBAC audit tables up to the mapped AuditLog write contract.

    Railway can have an old ``audit_logs`` table even when earlier migration
    versions were recorded by a deployment that did not contain every audit
    column.  Inspecting each column makes this safe to re-run and preserves
    historical rows.  ``LEGACY_EVENT`` is an explicit marker for rows created
    before action names existed; all new ORM writes supply their real action.
    """
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "audit_logs" not in table_names:
        return

    # Re-run the prior compatible reconcilers so this migration also repairs a
    # database whose historical migration-version record was incomplete.
    _reconcile_direct_dashboard_audit_actor(conn)
    _reconcile_audit_streamer_scope(conn)

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    if "channel_id" not in columns:
        logger.info("Migration 20260831_07: adding required audit_logs.channel_id")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN channel_id VARCHAR"))
    if "streamers" in table_names:
        streamer_cols = {column["name"].lower() for column in inspect(conn).get_columns("streamers")}
        if "youtube_channel_id" in streamer_cols:
            conn.execute(text(
                "UPDATE audit_logs SET channel_id = ("
                "    SELECT streamers.youtube_channel_id FROM streamers WHERE streamers.id = audit_logs.streamer_id"
                ") WHERE audit_logs.channel_id IS NULL AND audit_logs.streamer_id IS NOT NULL"
            ))

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    if "action" not in columns:
        logger.info("Migration 20260831_07: adding required audit_logs.action")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN action VARCHAR NOT NULL DEFAULT 'LEGACY_EVENT'"))
    elif conn.dialect.name == "postgresql" and columns["action"].get("nullable", True):
        # Existing NULLs have no recoverable action, but must be named before
        # the model's non-null invariant can be enforced.
        conn.execute(text("UPDATE audit_logs SET action = 'LEGACY_EVENT' WHERE action IS NULL"))
        logger.info("Migration 20260831_07: making audit_logs.action required")
        conn.execute(text("ALTER TABLE audit_logs ALTER COLUMN action SET NOT NULL"))

    columns = {column["name"].lower(): column for column in inspect(conn).get_columns("audit_logs")}
    if "details" not in columns:
        logger.info("Migration 20260831_07: adding nullable audit_logs.details")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN details VARCHAR"))
    if "timestamp" not in columns:
        logger.info("Migration 20260831_07: adding audit_logs.timestamp default")
        conn.execute(text("ALTER TABLE audit_logs ADD COLUMN timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"))


def _reconcile_youtube_daily_usage_window(conn) -> None:
    """Make the existing YouTube cap resettable instead of a lifetime stop."""
    table_names = {name.lower() for name in inspect(conn).get_table_names()}
    if "system_state" not in table_names:
        return
    columns = {column["name"].lower() for column in inspect(conn).get_columns("system_state")}
    if "youtube_api_window_date" not in columns:
        logger.info("Migration 20260831_08: adding system_state.youtube_api_window_date")
        conn.execute(text("ALTER TABLE system_state ADD COLUMN youtube_api_window_date DATE"))


def _bootstrap(target_engine: Engine) -> None:
    # Register mapped tables before create_all when the runner is invoked
    # directly (rather than through the application import path).
    from app.database import models  # noqa: F401
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
                elif version == "20260831_04_streamer_personality_mode":
                    _reconcile_streamer_personality_mode(conn)
                elif version == "20260831_05_audit_streamer_scope":
                    _reconcile_audit_streamer_scope(conn)
                elif version == "20260831_06_streamer_persona_enabled":
                    _reconcile_streamer_persona_enabled(conn)
                elif version == "20260831_07_audit_log_schema_complete":
                    _reconcile_audit_log_schema_complete(conn)
                elif version == "20260831_08_youtube_daily_usage_window":
                    _reconcile_youtube_daily_usage_window(conn)
                elif version == "20260831_09_audit_logs_channel_identity":
                    _reconcile_audit_log_schema_complete(conn)
                elif version == "20260831_10_audit_logs_nullable_user_id":
                    _reconcile_direct_dashboard_audit_actor(conn)
                    _reconcile_audit_log_schema_complete(conn)
                elif version == "20260831_11_audit_logs_actor_user_id_compat":
                    _reconcile_direct_dashboard_audit_actor(conn)
                    _reconcile_audit_log_schema_complete(conn)
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
