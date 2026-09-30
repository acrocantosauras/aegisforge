# Phase 6G — Reliability, Correctness & Circuit-Breaking Hardening

6G is a **hardening sprint**: no new product features. It audits the Phase 6
reliability surface (workers, recovery, checkpoints, planner, security) and
closes the P0/P1 gaps found. Every behavior change ships with regression
tests; every guarantee below is backed by a test that would fail if the
guarantee regressed.

## Guarantees and non-guarantees (read this first)

| Property | Status | Meaning |
|---|---|---|
| Job durability | **YES** (Redis-backed) | A job accepted into the queue is represented in Redis job data; a queued or claimed-but-unfinished job is always recoverable via visibility-timeout recovery. |
| Workflow durability | **YES** (PostgreSQL-backed) | Workflow-level state (checkpoints, final results) survives process and Redis restarts. |
| Task durability | **YES** (PostgreSQL-backed) | Each completed task persists a checkpoint before the next task starts; completed tasks are skipped on resume. |
| At-least-once execution | **YES** | Jobs and tasks may execute more than once after crashes/recoveries. External side effects MUST be idempotent. |
| Exactly-once execution | **NO** | Not implemented. Duplicate execution after worker crash/restart is possible and accepted. |
| Zero data loss under DB failure | **NO** | If PostgreSQL loses data, checkpoints/results are lost with it. |
| Distributed global tool-health / circuit state | **NO** | Tool health and circuit state are **process-local** (deliberately). A restart resets them; each process has its own view. |
| Automatic perfect recovery | **NO** | Recovery is bounded (retry budgets) and conservative; permission/configuration failures are never auto-retried. |
| Guaranteed LLM correctness | **NO** | The LLM planner's output is validated and falls back to the deterministic planner. |

## Circuit breaker (WS1)

`src/aegisforge/tools/circuit_breaker.py`. Per-tool breaker keyed by tool
name (operator-controlled key space, never user input). States:

```
CLOSED ──(threshold consecutive qualifying failures)──▶ OPEN
OPEN    ──(cooldown elapses)──▶ HALF_OPEN (single probe admitted)
HALF_OPEN ──(probe success)──▶ CLOSED
HALF_OPEN ──(probe failure)──▶ OPEN
```

- **Integrated at the `ToolRegistry.execute()` choke point**, AFTER the
  unknown-tool / disabled / permission checks. Policy denials happen first;
  circuit state can only *reject*, never *admit* an execution that policy
  would deny (deny-first ordering, regression-tested).
- **Qualifying failures**: real execution outcomes — FAILED, TIMEOUT.
  **Non-qualifying**: DENIED (permission/policy), disabled tools, unknown
  tools. These never create circuit state.
- **HALF_OPEN probe exclusivity**: exactly one probe in flight; a second
  concurrent `allow()` is refused until the probe resolves.
- **Fast-fail**: OPEN circuits return a structured
  `FAILED (CIRCUIT_OPEN: ...)` result without invoking the tool.
- **Bounded state**: per-tool deques with window pruning; state map holds
  only tools that transitioned (a never-failed tool has no entry).
- **Configuration** via `Settings` (`circuit_failure_threshold`,
  `circuit_cooldown_seconds`, etc.) with safe defaults.
- **Process-local**: circuit state is per-process and resets on restart.
  Documented deliberately — no unsafe distributed breaker was added.

Metrics: `tool_circuit_state_changes_total{from_state,to_state}`,
`tool_circuit_fast_fails_total`, `tool_circuit_probes_total{outcome}` —
bounded labels only.

## Worker reliability (WS2)

Claim/heartbeat/release are now **single Redis Lua operations** with
ownership checks:

- **Claim** (`CLAIM_LUA`): atomic SET-NX claim + terminal-state guard
  (cjson-parsed status, not substring matching) + capacity check +
  active-claims zset registration.
- **Heartbeat** (`HEARTBEAT_CLAIM_LUA`): compare-and-expire — a worker
  cannot extend another worker's claim.
- **Release** (`RELEASE_CLAIM_LUA`): compare-and-delete — a stale worker
  cannot delete another worker's claim (double-completion race closed).
- **Recovery** (`recover_expired_claims`): scan-cycle lock prevents two
  workers recovering the same expired job; terminal guard prevents
  re-enqueueing finished jobs; state persists BEFORE enqueue.
- **Retry** (`requeue_for_retry`): updated state (RETRYING + retry_count)
  persists BEFORE the queue push — a fast worker can no longer dequeue a
  retry copy with stale FAILED state and drop it.
- **Idempotency** (`submit_job`): SET-NX reservation before job creation
  closes the check-then-create TOCTOU. Verified under 20-thread contention
  (exactly one logical job, one queue entry).
- **BaseException hardening**: a `BaseException` (KeyboardInterrupt, hard
  shutdown) escaping the handler persists terminal FAILED state *before*
  propagating — previously the job was stranded in RUNNING with the claim
  released, unreachable by every recovery path.

## Queue durability (WS3 — RPOP→claim loss window)

The pre-6G sequence `RPOP` → `claim_job()` had a genuine loss window: if the
worker died (or Redis failed) after the pop but before a successful claim,
the popped copy was gone from the queue and registered nowhere recoverable.

Fix: **atomic dequeue+claim** (`DEQUEUE_CLAIM_LUA`) — pop, claim, capacity
check, and zset registration happen in ONE Redis operation. A job returned
to the worker is always claimed, so the visibility-timeout recovery path
covers a worker that dies before executing. Stale copies (terminal or
already-claimed) encountered during the scan are discarded; the authoritative
state lives in the job-data key. The plain-RPOP path remains as fallback for
callers without a worker identity. Regression test: worker "crashes" right
after atomic dequeue → `recover_expired_claims` finds the job in the
active-claims zset → redelivered to another worker.

## Worker capacity model (WS3)

Application-level, bounded:

- Workers advertise `capacity` via `set_worker_capacity` (hash, refreshed by
  heartbeats, removed on graceful shutdown).
- The atomic claim refuses work when a worker already holds `capacity`
  active jobs (HINCRBY inside the claim script — no over-admission).
- Gauges: `worker_capacity`, `worker_active_jobs`, `worker_available_slots`.
- `cleanup_dead_worker_state` sweeps registry/capacity/active-job entries
  for workers whose heartbeat TTL expired; wired into the heartbeat loop.
  Crashed workers no longer accumulate forever.

## Checkpoint reliability (WS4)

- **Tenant guard**: `load_resume_state` refuses checkpoints whose
  `organization_id` does not match the requesting context (empty org still
  resumes for single-tenant deployments).
- **Malformed data**: corrupt JSON / non-dict state no longer crashes
  resume — treated as empty state (at-least-once re-execution) instead.
- **Sanitization** unchanged: secrets stripped before persistence.
- **Concurrent writes**: last-writer-wins with version ordering protected
  (existing 6E behavior, still tested).

## Planner / health integration (WS5)

- Deterministic selection uses `select_tool_with_circuit`:
  `score = (4 − health_rank) + circuit_penalty` (closed=0, half_open=1,
  open=4) — a strictly-better candidate replaces the requested tool;
  unknown-health candidates never win without circuit evidence; ties keep
  the requested tool. **Permission checking is applied exactly as before;
  circuit state cannot bypass it.**
- LLM planner context includes bounded circuit states (same block as tool
  health: no errors, payloads, or tenant data).
- **Health concepts remain distinct** (see below).

### Tool health vs MCP server health vs circuit state

| Concept | Scope | Answers | Feeds |
|---|---|---|---|
| Tool Health (`ToolHealthTracker`) | per tool | "What is this tool's recent availability history?" | planner context, state-change metric |
| MCP Server Health (`MCPServerCatalog`) | per server | "Is the MCP server connected/healthy?" | operator dashboards, lifecycle |
| Circuit (`ToolCircuitBreaker`) | per tool | "Should the next execution be admitted?" | `ToolRegistry.execute()` admission |

They legitimately disagree: a tool can have an OPEN circuit with a healthy
server (repeated tool-level failures), or a closed circuit with an unhealthy
server (no recorded failures yet — admission allowed, call likely fails).
Nothing derives circuit state from server health or vice versa.

## Failure taxonomy (WS6)

`MultiAgentExecutor._classify_failure` → bounded classes:
`timeout | transient | unavailable | model | permission | configuration |
dependency | unknown`. `CIRCUIT_OPEN` fast-fails classify as `unavailable`.

Recovery decisions (`_suggest_recovery`):

| Class | Action |
|---|---|
| permission | **escalate** — never retried (deterministic policy denial) |
| configuration | skip — retrying cannot succeed without config change |
| dependency | skip |
| timeout | retry with extended timeout, within bounded budget |
| transient / unavailable / model | retry within bounded budget |
| unknown | retry cautiously within bounded budget |
| budget exhausted | abort |

No infinite retries; budget enforced per task and per job (`max_retries`).
Metric: `failure_classifications_total{failure_class,recovery_action}`.

## Observability (WS7/WS12)

Full lifecycle instrumented: request → workflow → task → agent → tool →
MCP → retry → checkpoint → recovery → queue/job/worker/claim events →
final result. Phase 6G additions: circuit state changes / fast-fails /
probes, worker capacity gauges, checkpoint save latency (workflow +
task-level kinds), failure classifications. All labels are bounded enums
(states, outcomes, classes, kinds) — no job/task/workflow/tenant IDs, no
payloads, no error text in labels.

## Security / tenant isolation (WS8)

- Redis URLs are sanitized before logging (`_sanitize_redis_url`); the raw
  URL (which may embed `user:password@host`) never reaches logs.
- Checkpoint tenant guard (above) closes cross-tenant resume.
- Job payloads flow dequeue→claim→execute with `organization_id` intact
  (regression-tested with interleaved tenants).
- Idempotency keys are dedup-only: a duplicate submission returns the
  ORIGINAL job; the binding cannot be hijacked to create a second job.
- Circuit/health snapshots contain only tool names and bounded numeric
  evidence — no payloads, errors, or tenant data (regression-tested).
- Deny-first ordering: disabled/unknown/permission-denied outcomes are
  decided before circuit admission (regression-tested adversarially).
- Claim operations are ownership-checked: a foreign worker cannot
  heartbeat/release another worker's claim.

## Performance baseline (WS10)

Measured on this dev machine (Windows, localhost Redis 7, SQLite in-memory,
single process, deterministic no-op handlers, ~300-byte JSON payloads).
**These are localhost microbenchmarks — NOT production-representative.**

| Measurement | Result | Backend |
|---|---|---|
| Job submission | ~562 jobs/sec | Redis |
| Dequeue+execute | ~187 jobs/sec | Redis |
| Queue latency | avg 2.0 ms, p95 16 ms | Redis |
| Concurrent (4 workers) | ~417 jobs/sec | Redis |
| Recovery throughput | ~400 jobs/sec | Redis |
| Atomic pop+claim (6G) | ~1816 claims/sec, avg 0.55 ms, p95 0.70 ms | Redis |
| Heartbeat (Lua CAS) (6G) | avg 0.83 ms | Redis |
| Checkpoint save / load (6G) | avg 2.4 ms / 1.2 ms | SQLite (pg proxy) |
| Circuit CLOSED admission vs OPEN fast-fail (6G) | fast-fail measurably cheaper than a full execution | in-process |
| In-memory queue | ~53k jobs/sec (architectural lower bound only) | in-process |

Reproduce: `pytest tests/test_performance_baseline.py -v -s`

## Redis / PostgreSQL failure behavior

- **Redis down**: queue operations fail-safe (dequeue returns None, workers
  idle and retry; enqueue raises to the caller). Jobs already durable in
  Redis survive a Redis restart (RDB/AOS depending on config) and their
  claims expire into recovery. While Redis is down, no queue progress is
  possible — this is a hard operational dependency.
- **PostgreSQL down**: workflow/checkpoint persistence fails; executions
  fail and follow the bounded retry taxonomy. Queue operation continues
  (Redis-only), but workflow durability is unavailable — jobs execute
  at-least-once without checkpoint protection.
- **Worker crash**: claim expires via visibility timeout → recovery
  re-enqueues (bounded by retry budget) → at-least-once redelivery.
- **MCP server down**: connection failures classify as transient/unavailable
  → bounded retry / alternate tool; the tool circuit breaker stops the
  stampede after repeated failures.

## Known limitations (explicit)

1. Tool health + circuit state are process-local (no safe distributed
   mechanism in the current architecture; adding one is a future decision).
2. Plans are not dynamically re-planned mid-execution after health changes;
   health/circuit context affects selection at planning time and admission
   at execution time.
3. At-least-once means duplicate external side effects are possible; the
   platform does not deduplicate tool calls.
4. Recovery counters are TTL-bounded (24h) but per-job counts within that
   window share one hash — cardinality bounded by distinct job ids in 24h.
5. Single-writer-per-workflow remains assumed for checkpoint ordering.
6. `submitted_at` wall-clock assumes reasonably synchronized hosts (NTP);
   large skew skews queue-wait metrics only, never correctness.
7. Windows/macOS dev timing: circuit cooldown tests use generous margins
   to stay deterministic under scheduler jitter.

## Test coverage map (6G)

| Suite | Covers |
|---|---|
| `test_circuit_breaker.py` | state machine, probe exclusivity, cooldown, ranking |
| `test_worker_hardening.py` | claim/heartbeat/release races, terminal guard, capacity, recovery lock |
| `test_phase6g_closeout.py` | cjson terminal guard (poisoned payload), atomic dequeue durability, idempotency concurrency, worker-state boundedness, context isolation |
| `test_checkpoint_resilience.py` | tenant guard, malformed/stale state, concurrent writes, taxonomy |
| `test_tenant_security.py` | payload integrity, idempotency hijack, secret hygiene, aggregate-state leakage, deny-first, claim ownership |
| `test_failure_injection.py` | Redis outage, worker crash, tool timeout/unavailable, permission denial, cancellation, stale claims, circuit modes, checkpoint failure |
| `test_health_concept_separation.py` | tool health vs MCP health vs circuit independence |
| `test_performance_baseline.py` | the numbers above |
