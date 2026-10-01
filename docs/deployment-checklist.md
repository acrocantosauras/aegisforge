# AegisForge — Deployment Readiness Checklist (Phase 9)

Evidence types: **LIVE-PROD** (validated against the local production-equivalent
compose stack, Phase 9), **UNIT/INTEGRATION** (pytest), **CONFIG** (inspected /
statically validated), **NOT DONE** (requires external resources).

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Secrets externalized (none committed) | ✅ VERIFIED | CONFIG: `.env*` gitignored; compose requires vars; no hardcoded secrets found |
| 2 | SECRET_KEY required in production | ✅ VERIFIED | UNIT (fail-fast tests) + LIVE-PROD (stack ran with real key) |
| 3 | Redis authenticated | ✅ VERIFIED | LIVE-PROD: `NOAUTH`/`WRONGPASS` without password; prod stack uses strong password |
| 4 | PostgreSQL configured | ✅ VERIFIED | LIVE-PROD: healthy, strong password, not publicly published |
| 5 | pgvector available | ✅ VERIFIED | LIVE-PROD: pgvector image; migrations created vector schema |
| 6 | Migrations verified | ✅ VERIFIED | LIVE-PROD: `api-migrate` ran `alembic upgrade head` once; API started after |
| 7 | API production image | ✅ VERIFIED | LIVE-PROD: built clean, non-root, healthy; CI smoke job added |
| 8 | Worker production image | ✅ VERIFIED | LIVE-PROD: workers healthy, registered, executed workflows |
| 9 | Frontend production build | ✅ VERIFIED | LIVE-PROD: standalone Next.js image, HTTP 200, non-root, no dev server |
| 10 | Frontend API URL configurable | ✅ VERIFIED | LIVE-PROD: `NEXT_PUBLIC_API_URL` baked into bundle (grep-verified) |
| 11 | CORS configured | ✅ VERIFIED | CONFIG: explicit origin list required in prod; no wildcard |
| 12 | Readiness checks real dependencies | ✅ VERIFIED | LIVE-PROD: `/api/v1/ready` → `{"database":"ok","redis":"ok"}` |
| 13 | Liveness endpoint | ✅ VERIFIED | LIVE-PROD: `/api/v1/health` 200 |
| 14 | Authentication enforced | ✅ VERIFIED | LIVE-PROD (adversarial): anon/garbage/tampered tokens → 401 |
| 15 | Tenant isolation / IDOR | ✅ VERIFIED | LIVE-PROD (adversarial): cross-user access → 404, 38/38 suite |
| 16 | Queue execution | ✅ VERIFIED | LIVE-PROD: async submission → Redis → workers → persisted result |
| 17 | Workflow execution | ✅ VERIFIED | LIVE-PROD: planner→research→RAG→analysis→synthesis→evaluation completed |
| 18 | Recovery / retry semantics | ✅ VERIFIED | UNIT/INTEGRATION (Phase 6/8 suites); not re-faulted on prod stack |
| 19 | Rate limiting enabled | ✅ VERIFIED | CONFIG: Redis-backed limiter on by default in prod env |
| 20 | Observability stack | ✅ VERIFIED | LIVE-PROD: otel-collector/Prometheus/Grafana running internal-only; Prometheus scrapes API (`up=1`); `active_workers=2`, `queue_depth`, `active_claims` scraped after the API-side gauge-refresh fix (Phase 9); Grafana `/api/health` ok |
| 21 | HTTPS (TLS certificates) | ❌ NOT DONE | Requires real host + DNS + open 80/443; Caddy config prepared |
| 22 | Backups configured | ⚠️ DOCUMENTED | Provider snapshots / `pg_dump` documented in `docs/phase9-deployment.md`; not executed |
| 23 | Real deployment on public host | ❌ NOT DONE | No provider credentials in this environment — manual steps in docs §5 |
| 24 | End-to-end on deployed URL | ❌ NOT DONE | Follows from #23; full E2E passed on production-equivalent stack |

**Summary:** 20 verified, 1 documented, 3 blocked on external resources
(host + DNS). Nothing on this list is marked done without evidence.
