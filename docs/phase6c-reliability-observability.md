# Phase 6C: Production Reliability & Observability

## Overview

AegisForge Phase 6C makes the distributed worker system operationally reliable and observable. This phase does **not** add new AI capabilities — it focuses exclusively on failure handling, observability, and production validation.

## Key Design Decisions

- **At-least-once semantics**: The distributed queue provides at-least-once-style execution. External side effects **must** be idempotent. AegisForge does not guarantee exactly-once execution.
- **No new infrastructure**: Redis and PostgreSQL remain the sole external dependencies. No Kafka, Celery, or Kubernetes were introduced.
- **Bounded everything**: All retries, backoffs, recovery attempts, and timeouts are bounded. No infinite loops.

---

## Available Metrics

All metrics use **bounded labels only**. No user IDs, request IDs, job IDs, or arbitrary tool names appear as Prometheus labels.

### Queue Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `queue_depth` | Gauge | — | Current number of pending jobs |
| `queue_jobs_total` | Counter | `status` (queued/running/completed/failed/retried/cancelled) | Total jobs by lifecycle status |
| `queue_jobs_dequeued_total` | Counter | — | Total jobs dequeued from the queue |
| `queue_jobs_claimed_total` | Counter | — | Total jobs successfully claimed by workers |
| `queue_jobs_abandoned_total` | Counter | — | Jobs that reached terminal failure (max retries exhausted) |
| `queue_wait_time_seconds` | Histogram | — | Time a job spends waiting before being dequeued |
| `job_duration_seconds` | Histogram | `status` (completed/failed) | Job processing duration |

### Worker Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `active_workers` | Gauge | — | Number of active workers with recent heartbeats |
| `worker_events_total` | Counter | `status` (claimed/completed/failed/retried) | Worker lifecycle events |
| `worker_registrations_total` | Counter | — | Total worker registrations |
| `worker_deregistrations_total` | Counter | — | Total worker deregistrations |
| `worker_heartbeat_failures_total` | Counter | — | Total worker heartbeat failures |
| `jobs_recovered_total` | Counter | — | Total jobs recovered from crashed workers |

### Claims Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `active_claims` | Gauge | — | Number of currently active (unexpired) claims |
| `claim_expirations_total` | Counter | — | Claims that expired (possible worker crash) |
| `claim_extensions_total` | Counter | — | Visibility-timeout extensions via heartbeat |

### Stuck Job Detection

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `stuck_jobs_detected_total` | Counter | — | Jobs detected as stuck by the watchdog |
| `stuck_jobs_recovered_total` | Counter | — | Stuck jobs that were recovered |

---

## Health & Readiness Endpoints

### `/api/v1/health` (Liveness)

Always returns **200 OK** when the process is alive. Does **not** depend on any external dependencies. If the process is running, liveness returns 200 regardless of Redis/Postgres status.

```json
{"status": "ok"}
```

### `/api/v1/ready` (Readiness)

Returns **200** when all critical dependencies are reachable, **503** otherwise. Checks:

- PostgreSQL connectivity (`SELECT 1`)
- Redis connectivity (`PING`)

Results are cached for 5 seconds to avoid hammering dependencies.

```json
{"status": "ready", "checks": {"database": "ok", "redis": "ok"}}
```

### `/api/v1/workers` (Operational)

Returns the list of active workers and their heartbeat freshness. This is an operational visibility endpoint, not a health probe.

```json
{
  "status": "ok",
  "active_workers": 2,
  "workers": [
    {"worker_id": "worker-a1b2c3d4", "last_heartbeat_age_seconds": 5.2, "healthy": true}
  ]
}
```

---

## Worker Lifecycle

1. **Registration**: On startup, the worker registers in the Redis worker registry (`aegisforge:workers` sorted set).
2. **Heartbeat**: Every 30 seconds (configurable), the worker refreshes its registry heartbeat and any active job claim.
3. **Job Processing**: Dequeue → Claim → Execute → Complete/Fail → Release claim.
4. **Recovery Scanning**: Every 60 seconds (configurable), the worker scans for expired claims and re-enqueues them.
5. **Stuck Job Detection**: Runs alongside recovery scanning with configurable thresholds.
6. **Graceful Shutdown**: On SIGTERM/SIGINT, the worker releases all claims and unregisters from the registry.

---

## Recovery Behavior

### Crash Recovery

When a worker crashes:
1. Its claim key in Redis expires after the visibility timeout (default: 300s).
2. The recovery scanner detects the expired claim in `aegisforge:active_claims`.
3. The job is re-enqueued with an incremented `retry_count`.
4. Another worker picks up the re-enqueued job.

### Stuck Job Detection

The stuck job detector uses multiple signals:

| Signal | Default Threshold | Description |
|--------|------------------|-------------|
| `max_job_age_seconds` | 3600 (1h) | Maximum age for a queued job |
| `max_claim_age_seconds` | 600 (10m) | Maximum age of a claim before it's stale |
| `max_execution_time_seconds` | 1800 (30m) | Maximum total execution time |
| `max_recoveries` | 5 | Maximum recovery attempts before terminal failure |

**Configuration** (via environment variables or `Settings`):

```
STUCK_JOB_DETECTION_ENABLED=true
STUCK_JOB_MAX_AGE_SECONDS=3600
STUCK_JOB_MAX_CLAIM_AGE_SECONDS=600
STUCK_JOB_MAX_EXECUTION_TIME_SECONDS=1800
STUCK_JOB_MAX_RECOVERIES=5
```

---

## Retry Semantics

- **Bounded retries**: Each job has a `max_retries` count (default: 3).
- **Terminal failure**: When `retry_count >= max_retries`, the job reaches `FAILED` status permanently.
- **No infinite loops**: All retry mechanisms are bounded.
- **Permanent failure detection**: Jobs failing with "permission denied", "unauthorized", "invalid input", or "not found" errors are not retried.
- **Bounded backoff**: `BoundedBackoff` class provides exponential backoff with jitter (0.1s initial, 30s max).

---

## Dashboards

Four Grafana dashboards are provisioned automatically:

### 1. AegisForge System (`aegisforge-system`)
- API request rate and latency
- LLM latency and failures
- Workflow success rate
- Tool execution count
- Retry count

### 2. AegisForge Queue (`aegisforge-queue`)
- Queue depth
- Jobs submitted/dequeued/claimed
- Queue wait time (p50, p95)
- Job lifecycle (completed/failed/retried)
- Job execution duration
- Retries, recoveries, and abandonments
- Active claims, claim expirations
- Stuck jobs detected/recovered

### 3. AegisForge Workers (`aegisforge-workers`)
- Active workers (with threshold coloring)
- Worker registrations/deregistrations
- Worker heartbeat failures
- Worker events (claimed/completed/failed)
- Jobs per worker, worker availability
- Queue depth vs active workers
- Recovery rate

### 4. AegisForge Infrastructure (`aegisforge-infrastructure`)
- API request rate and latency (p50/p95/p99)
- HTTP error rate (4xx/5xx)
- API success rate
- Active workers
- Workflow success rate
- LLM latency
- RAG retrieval latency
- Approval requests
- Tool executions

---

## Alert Rules

Prometheus alert rules are defined in `observability/alert-rules.yml`. These are designed to be consumed by a standard Alertmanager deployment.

### Worker Alerts
- **NoHealthyWorkers**: `active_workers == 0` for 1 minute (critical)
- **WorkerHeartbeatFailures**: heartbeat failures > 0.1/s for 5 minutes (warning)
- **WorkerRegistrationStorm**: registrations > 0.5/s for 5 minutes (warning)

### Queue Alerts
- **QueueGrowthSustained**: queue depth predicted to grow 50% in 10 minutes (warning)
- **HighJobFailureRate**: > 30% job failure rate for 5 minutes (warning)
- **ExcessiveJobAge**: p95 queue wait > 5 minutes for 10 minutes (warning)
- **RepeatedJobRecovery**: recovery rate > 0.05/s for 10 minutes (warning)
- **HighTerminalFailureRate**: abandonments > 0.01/s for 5 minutes (warning)

### Infrastructure Alerts
- **RedisUnavailable**: Redis unreachable for 2 minutes (critical)
- **PostgreSQLUnavailable**: /ready returning 503 for 2 minutes (critical)
- **HighAPIErrorRate**: > 5% 5xx error rate for 5 minutes (warning)
- **StuckJobsDetected**: stuck jobs detected for 5 minutes (warning)

---

## Running Reliability Integration Tests

```bash
# Requires Redis running on localhost:6379
# Start Redis via Docker:
docker run -d --name redis-test -p 6379:6379 redis:7-alpine

# Run all Phase 6C reliability tests:
pytest tests/test_reliability.py tests/test_stuck_job_detection.py \
       tests/test_health_readiness.py tests/test_performance_baseline.py \
       tests/test_distributed_worker.py -v

# Run full test suite:
pytest tests/ -v
```

Tests that require Redis are automatically skipped if Redis is not available.

---

## Reproducing the Performance Baseline

```bash
# Requires Redis on localhost:6379
pytest tests/test_performance_baseline.py -v -s
```

### Baseline Environment
- **OS**: Windows (tests also run on Linux/macOS)
- **Python**: 3.12
- **Redis**: 7.x (local)
- **Workers**: Thread-based concurrency (not multiprocessing)
- **Handler**: Deterministic (no I/O, no LLM calls)

### Measured Baselines
- **Submission throughput**: ~592 jobs/sec (500 jobs in 0.84s)
- **Dequeue+execute throughput**: ~163 jobs/sec (500 jobs in 3.06s)
- **Queue latency**: avg=2.03ms, p95=16.00ms
- **Execution throughput**: ~158 jobs/sec (200 jobs in 1.27s)
- **Concurrent scaling (4 workers)**: ~426 jobs/sec (300 jobs)
- **Recovery throughput**: ~455 jobs/sec (50 jobs in 0.11s)
- **In-memory baseline**: ~87,080 jobs/sec

### Known Limitations
- Measures queue overhead, not actual workflow execution time
- Single Redis instance, no network latency simulation
- Deterministic handler (no I/O, no LLM calls)
- Thread-based concurrency (not multiprocessing)

---

## Reliability Limitations

1. **At-least-once semantics**: Under narrow failure races (e.g., worker completes job at the exact moment its claim expires), a job may be executed twice. External side effects **must** be idempotent.

2. **No exactly-once delivery**: The Redis-backed queue provides atomic claiming but cannot prevent all duplicate executions during crash recovery.

3. **Visibility timeout tradeoff**: A shorter visibility timeout catches crashes faster but increases the risk of duplicate execution for legitimately long-running jobs.

4. **Single Redis instance**: The current architecture uses a single Redis instance. Redis Sentinel or Cluster would be needed for Redis HA.

5. **Worker crash causes execution interruption, not job loss**: When a worker crashes mid-execution, the claimed job is automatically recovered after the visibility timeout expires. The job is re-enqueued for another worker. However, any computation performed by the crashed worker before the crash is lost — the job restarts from its last checkpoint (if checkpointing is enabled) or from the beginning. This is execution interruption, not job loss. Workflow checkpoints are durable (DB-backed in production) and allow resumption from the last completed node.

6. **Stuck job detection is conservative**: By default, jobs must be queued for 1 hour or have a stale claim for 10 minutes before detection. This avoids false positives but means genuinely stuck jobs may take time to detect.

---

## Security Verification

- **Tenant isolation**: Job `organization_id` is preserved through the entire lifecycle. Jobs from different tenants never leak.
- **No secrets in metrics**: Prometheus labels contain only bounded, low-cardinality values (status codes, agent types). No user IDs, request IDs, or payloads.
- **No secrets in logs**: Worker logs contain job IDs and status but never job payloads or credentials.
- **No secrets in dashboards**: Dashboard queries reference only metric names and label values, never raw data.
- **Authentication intact**: Worker APIs use the same auth middleware as API routes.
- **Worker APIs respect permissions**: Workers operate through the same `JobManager` and `JobWorker` classes used by the API, with no bypass paths.
