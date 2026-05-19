#!/bin/sh
set -e

# Run Alembic migrations before starting the application.
# Safe to run on every startup — Alembic is idempotent (skips already-applied revisions).
echo "[entrypoint] Running database migrations..."
alembic upgrade head
echo "[entrypoint] Migrations complete."

# Hand off to the CLI command passed as CMD (default: serve)
exec portfolio-agent "$@"
