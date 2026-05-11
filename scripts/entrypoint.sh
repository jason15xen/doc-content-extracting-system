#!/usr/bin/env bash
set -euo pipefail

# WORKER_MODE=coordinator → run the sharding coordinator (no SQLite, no
# alembic). Anything else (default) → run the worker FastAPI app as before.
if [ "${WORKER_MODE:-worker}" = "coordinator" ]; then
    exec uvicorn app.coordinator.main:app --host 0.0.0.0 --port 8889
fi

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8889
