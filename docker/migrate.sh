#!/bin/sh
# Single migration runner for container startup (Phase 9).
# The API service runs this BEFORE uvicorn starts; workers wait on API
# health, so migrations run exactly once per deployment, never concurrently
# from multiple workers.
set -eu

echo "[migrate] waiting for database to accept connections..."
python - <<'PYEOF'
import os
import sys
import time

from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, pool_pre_ping=True)
for attempt in range(60):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        break
    except Exception:
        if attempt == 59:
            print("[migrate] database never became reachable", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
engine.dispose()
PYEOF

echo "[migrate] running alembic upgrade head"
alembic upgrade head
echo "[migrate] done"

exec "$@"
