# Phase 7 — Production Sprint: Verification & Hardening

Phase 6 ended verified (746 unit tests, 32/32 integration tests, Ruff/mypy clean).
Phase 7 re-verified every production surface against the **current working tree**
and closed the gaps found. No architecture was replaced; no tests were weakened.

## What was verified (evidence, not claims)

### Docker production stack (WS-A)

Rebuilt from the working tree (`docker compose build`, not stale images) and
recreated. All services healthy:

| Service | Status |
|---|---|
| postgres (pgvector/pg16) | healthy |
| redis (7-alpine) | healthy, **password-authenticated** |
| api | healthy |
| worker-1 / worker-2 | healthy |
| frontend (profile `full`) | builds and serves production (`next build && next start`, not dev) |

- `/api/v1/health` → 200 `{"status":"ok"}` (liveness, no external deps)
- `/api/v1/ready` → 200 with `database: ok`, `redis: ok`
- `/api/v1/workers` → live worker registry with heartbeat freshness
- `/metrics` → Prometheus exposition including Phase 6E–6G metrics

### Database (WS-B)

- Fresh-migration path proven in CI (`fresh-migration` job) and live here:
  17 tables, `vector` extension present, `alembic_version = 20260904_phase42`.
- Checkpoint tables (`workflow_checkpoints`), job tables (`execution_jobs`),
  approvals, audit events all present.

### Live end-to-end (WS-E)

Executed **through the real running Docker stack** (no mocks):

```
register → login (JWT) → create request → execute →
Redis queue → separate worker container → LangGraph multi-agent run →
deterministic LLM → checkpoint saves → persisted result → API response 200
```

- Request row reached `completed` in PostgreSQL.
- `workflow_runs_total{status="completed"} 1.0` observed on `/metrics`.
- `checkpoint_save_latency_seconds_count{kind="workflow"} 6.0` — durability
  overhead is real and measured.
- Re-verified end-to-end **after** Redis auth was enabled (the E2E exercises
  the authenticated REDIS_URL path).
- `tests/integration/test_worker_mcp_e2e.py` additionally proves
  API → Redis → **separate worker process** → real stdio MCP server →
  persisted result (3 passed).

### Observability (WS-I)

Cross-checked every metric referenced in `observability/alert-rules.yml` and
all four Grafana dashboards against `src/aegisforge/observability/metrics.py`:

- **0 missing metrics.** Every dashboard panel and alert rule references a
  metric that is actually defined (script-checked, not eyeballed).
- Grafana datasource points at `http://prometheus:9090` (correct service name).
- All metric labels remain bounded enums — no IDs, no payloads.

## Hardening changes (bugs fixed)

### P1 — Redis was unauthenticated on a published port

`docker-compose.yml` ran `redis-server` with **no `requirepass`** while
publishing `6379` to the host. Any process on the host (or a misconfigured
bridge peer) could read the job queue, job payloads, and claims, and enqueue
arbitrary jobs.

Fix:
- Redis now starts with `--requirepass ${REDIS_PASSWORD:-…}`.
- Health check authenticates (`redis-cli -a …`).
- API and both workers connect via `redis://:<password>@redis:6379/0` and
  worker healthchecks read `REDIS_URL` from the environment instead of a
  hardcoded unauthenticated URL.
- `.env.example` documents `REDIS_PASSWORD` (must be overridden in production).

Verified: unauthenticated `PING` → `NOAUTH`; authenticated → `PONG`; API
`/ready` green; workers healthy; live E2E re-run green.

### P1 — Redis-dependent unit tests silently skipped with authed Redis

Eight test suites hardcoded `redis://localhost:6379/0`. With the hardened
Redis they would **silently skip** (exactly the failure mode CI must not
have). All suites now honor `AEGISFORGE_TEST_REDIS_URL` (falling back to the
old default), via a shared `_test_redis_url()` helper:

- `test_distributed_worker.py`, `test_failure_injection.py`,
  `test_phase6g_closeout.py`, `test_reliability.py`,
  `test_stuck_job_detection.py`, `test_tenant_security.py`,
  `test_worker_hardening.py`, `test_performance_baseline.py`

Full suite verified both ways: 746 passed with the authed URL **and** 658
passed / 120 skipped without Redis (all skips are Redis-dependent suites).

### P1 — Missing Redis guard on a tenant-security test

`TestClaimOwnershipBoundary` (claim ownership boundary — a security test)
was missing `@requires_redis`, so under an unreachable/authenticated Redis it
**errored instead of skipping** in environments without an unauthenticated
local Redis. Guard added.

### CI (WS-G)

- `docker-build` job previously had **no Redis service**, so the container's
  `/ready` check could never validate Redis wiring. Added the Redis service
  container and a `/ready` assertion to the docker smoke test, so a broken
  Redis URL/auth configuration in the image fails CI.

### Compose hygiene

- Removed obsolete `version: '3.9'` attribute (deprecation warning).
- Worker healthchecks now use the real `REDIS_URL` env var.

## Security audit summary (WS-H)

- Hardcoded secret scan across `src/`: **clean** (no API keys, tokens, or
  passwords in source). Dev defaults exist only in `config.py` / compose
  interpolation defaults and are documented as must-override.
- Redis: now password-protected in compose (above).
- Rate limiting: audited — Redis-backed fixed-window, keyed by user id
  (bearer token decoded, not stored) or client IP, never by user content;
  bounded in-memory fallback; documented fail-open policy on limiter outage.
- CORS: wildcard origin forces `allow_credentials=False` (no unsafe combo).
- Logs: Redis URLs sanitized before logging (`_sanitize_redis_url`); MCP stdio
  debug output is opt-in via env flag.
- No debug/breakpoint code in `src/`.

## Final validation results

| Check | Result |
|---|---|
| `pytest -q` (with authed Redis) | **746 passed**, 32 skipped |
| `pytest -q` (no Redis) | 658 passed, 120 skipped (Redis suites skip by design) |
| `AEGISFORGE_INTEGRATION_TESTS=true pytest tests/integration -q` | **32 passed** |
| `tests/integration/test_worker_mcp_e2e.py` (separate worker, real MCP) | **3 passed** |
| `ruff check .` | clean |
| `mypy src` | clean (82 files) |
| Frontend lint / type-check / test / build | clean, 35 tests passed |
| `docker compose build` + up | 5/5 services healthy |
| Live E2E through the stack | completed, persisted, observable |

## Remaining known gaps (documented, not silently accepted)

- **P2**: `secret_key` still defaults to a dev value when `SECRET_KEY` is
  unset in production; production deployments must set it (`.env.example`
  documents this). A fail-fast validator is a follow-up.
- **P2**: Tool health / circuit state remain process-local by design (see
  Phase 6G known limitations).
- **P2**: Grafana dashboards are provisioned by file mount; the
  `observability` compose profile was verified for config wiring, not
  interactively clicked through.

## Git state

All changes are **uncommitted working-tree changes**. Nothing staged,
nothing committed, nothing pushed.
