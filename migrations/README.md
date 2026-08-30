# Migrations

Run `python -m migrations.runner` once per deployment before starting the web process. Migrations are append-only and recorded in `schema_migrations`; do not rely on `create_all` for production schema changes.
