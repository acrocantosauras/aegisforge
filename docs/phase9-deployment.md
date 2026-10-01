# Phase 9 — Production Deployment

Status: **DEPLOYMENT-READY — external host + DNS required for real HTTPS.**
The production stack was validated end-to-end locally (real workflow execution,
adversarial security suite 38/38, readiness checks). What remains is
provisioning an actual host and DNS names — no code changes are needed.

## 1. Architecture

Smallest architecture that supports the existing system (no Kubernetes, no
new middleware):

```
Browser ──HTTPS──> Caddy (80/443, automatic Let's Encrypt)
                    ├── APP_DOMAIN  ──> frontend  (Next.js standalone image)
                    └── API_DOMAIN  ──> api       (FastAPI, uvicorn)
                                          ├── Postgres 16 + pgvector (internal only)
                                          └── Redis 7, requirepass   (internal only)
worker-1, worker-2  ── Redis queue + Postgres (internal only)
api-migrate         ── runs `alembic upgrade head` once, then exits
```

Startup order is enforced by compose dependencies:
`postgres/redis healthy → api-migrate → api healthy → workers → frontend → caddy`.
Exactly one service runs migrations; workers never migrate.

Postgres and Redis have **no published ports** in production: they are only
reachable from the internal compose network. The API is exposed only through
Caddy (or the `API_PORT` host override for testing).

## 2. Required services

You may run everything on one host with the bundled services, or swap in
managed equivalents via `DATABASE_URL` / `REDIS_URL`:

| Component | Bundled | Managed alternative (documented option) |
|---|---|---|
| Postgres + pgvector | `pgvector/pgvector:pg16` | Any managed Postgres with `CREATE EXTENSION vector` (e.g. Tiger Cloud, RDS). Tested protocol: standard Postgres wire. |
| Redis (auth) | `redis:7-alpine` + requirepass | Any authenticated Redis-compatible service (e.g. Redis Cloud, Dragonfly Cloud, GCP Memorystore). Set `REDIS_URL`. |
| Compute | Single VM with Docker | Any Docker-capable VM (e.g. Akamai/Linode 2–4 GB). |
| HTTPS | Caddy (built in) | Load balancer of your provider, if preferred. |

Selection reasoning: one VM + Docker Compose is the smallest reliable unit for
a FastAPI + workers + Postgres + Redis + Next.js stack; managed DB/Redis move
backups and durability to the provider. Kubernetes is not justified at this
scale and is explicitly out of scope.

## 3. Environment variables

Copy `.env.example` → `.env.prod` (or `.env`) and set **all** required values.
Required for `docker-compose.prod.yml`:

- `SECRET_KEY` — ≥ 32 chars; the app refuses to start in production otherwise
- `POSTGRES_PASSWORD` (skip when using managed `DATABASE_URL`)
- `REDIS_PASSWORD` (skip when using managed `REDIS_URL`)
- `CORS_ORIGINS` — the browser frontend origin, e.g. `https://app.example.com`
- `API_DOMAIN`, `APP_DOMAIN`, `ACME_EMAIL` — Caddy HTTPS
- `NEXT_PUBLIC_API_URL` — browser-reachable API origin, baked into the
  frontend bundle at **build** time

Optional overrides: `API_PORT`, `FRONTEND_PORT` (host-side port mappings for
coexistence/testing), `RATE_LIMIT_ENABLED` (default true),
`LLM_*`, `EMBEDDING_*`, `ENVIRONMENT=production`, `DEBUG=false`.

No secrets are committed anywhere; `.env` / `.env.prod` are gitignored.

## 4. Local production-equivalent startup

Validated locally on alternate ports with an isolated compose project:

```sh
cp .env.example .env.prod     # fill real values
ENV_FILE=.env.prod PROJECT=aegisforge-prod ./scripts/deploy.sh up
# (for local-only validation without DNS, set API_PORT/FRONTEND_PORT and
#  start services explicitly, without caddy)
```

Commands: `check` (validate env), `up`, `down`, `status`, `logs`.
`OBS=1` attaches the observability overlay (otel-collector, Prometheus,
Grafana — internal only; access via SSH tunnel or authenticated proxy).

## 5. Production deployment (the one remaining manual step)

1. Provision a Docker-capable VM (Ubuntu 22.04+).
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Clone this repository on the VM.
4. `cp .env.example .env` and set real values (use
   `python -c "import secrets; print(secrets.token_urlsafe(48))"` for
   `SECRET_KEY`, strong random DB/Redis passwords).
5. Create DNS A records: `API_DOMAIN` and `APP_DOMAIN` → the VM's public IP.
6. `./scripts/deploy.sh up` (add `OBS=1` for observability).
7. Verify: `curl https://$API_DOMAIN/api/v1/ready` →
   `{"status":"ready","checks":{"database":"ok","redis":"ok"}}`.

Caddy obtains Let's Encrypt certificates automatically once DNS resolves.
These steps require an actual host + DNS and were **not executed** — no
provider credentials were available in this environment.

## 6. Database setup

- Schema is created by Alembic (`alembic upgrade head` in `api-migrate`).
- pgvector: the bundled image ships the extension; managed users must enable
  the `vector` extension before migrating.
- Fresh database: nothing to do manually; migrations are idempotent.
- Backups: use provider snapshots (managed) or `pg_dump` cron on the VM —
  documented as a user responsibility; not configured here.
- Rollback: not supported by the migration chain (forward-only); restore a
  backup instead.

## 7. Redis setup

- Must require authentication (`requirepass` / provider ACL). Unauthenticated
  Redis is treated as a P1 exposure.
- Used for: job queue, idempotency keys, worker heartbeat/registry, recovery
  claims, rate limiting. Persistence: AOF enabled in the bundled config.
- Redis unavailability: API readiness reports `redis: down` and workers
  healthcheck-fail; queued jobs remain in Redis (AOF) and are recovered by
  the stuck-job scanner on restart.

## 8. Worker setup

- `worker-1` / `worker-2` (scale by duplicating services): long-running
  processes, heartbeat + capacity registration, graceful SIGTERM shutdown
  with claim cleanup, at-least-once execution.
- Scaling guidance: one worker service per 1–2 vCPU. Workers must share the
  same `REDIS_URL` / `DATABASE_URL` as the API.

## 9. Frontend setup

- `Dockerfile.frontend` builds a deterministic standalone Next.js image.
- `NEXT_PUBLIC_API_URL` is a build argument — it must be the browser-reachable
  API origin and is baked at build time (verified in the built bundle).
- Non-root runtime user; `node server.js`; no dev server.

## 10. Migrations

- Single runner (`api-migrate`) executes `alembic upgrade head` exactly once
  per deployment; the API starts only after it completes successfully.
- Verify head locally: `alembic heads` (single head enforced).

## 11. Health checks

- `/api/v1/health` — liveness (200 if the process serves).
- `/api/v1/ready` — readiness; returns 503 unless **database** and **redis**
  are reachable (`{"checks":{"database":"ok","redis":"ok"}}`).
- Workers: Redis ping healthcheck; API `/system` shows registered/healthy
  workers (authenticated).
- `/metrics`: the API refreshes `active_workers`, `active_claims`, and
  `queue_depth` gauges from authoritative Redis state on every scrape.
  Workers set these gauges only in their own processes, which Prometheus
  cannot see; the API-side refresh makes the shipped alert rules
  (`NoHealthyWorkers`, `QueueGrowthSustained`, etc.) evaluable. Verified
  live: Prometheus reports `active_workers=2` with two workers running.
  A Redis outage never breaks the scrape (last values are kept).

## 12. Security requirements

- `ENVIRONMENT=production`, `DEBUG=false`; SECRET_KEY fail-fast enforced.
- Redis: password required; not published publicly.
- Postgres: not published publicly; strong password.
- CORS: explicit frontend origin only (no wildcard).
- Rate limiting: enabled (Redis-backed).
- HTTPS: Caddy + Let's Encrypt; ACME email required.
- Verified against the local production stack: adversarial suite 38/38
  (IDOR, cross-user 404s, token tampering, malformed IDs, no secret leakage
  in `/metrics`).
- Observability validated live: Prometheus + Grafana + otel-collector run
  internal-only in the overlay; scrape targets UP; worker/queue gauges
  scraped by Prometheus.

## 13. Troubleshooting

- `api-migrate` failed: `docker compose logs api-migrate` — usually DB not
  reachable or wrong password.
- Redis `WRONGPASS` in healthcheck: ensure the same `REDIS_PASSWORD` is used
  by the redis service and `REDIS_URL`.
- Frontend calls wrong API: `NEXT_PUBLIC_API_URL` is baked at build time —
  change it and rebuild the frontend image.
- Port conflicts with a dev stack: set `API_PORT` / `FRONTEND_PORT` and use
  `PROJECT=<name>` (compose `-p`) for an isolated project.
- Certificates not issued: DNS must point at the host and ports 80/443 open.

## 14. Known limitations

- At-least-once execution semantics (not exactly-once).
- Tool health / circuit-breaker state is process-local per worker.
- Migrations are forward-only (no automated down-migrations).
- Default CORS/rate-limit values must be reviewed per deployment.
- The bundled single-node Postgres/Redis are not HA; use managed services
  for durability guarantees.
