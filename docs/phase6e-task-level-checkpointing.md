# Phase 6E: Task-Level Durable Checkpointing

## Overview

AegisForge Phase 6E closes the last durability gap in workflow recovery: **intra-wave task loss**.

Phase 6C/6D checkpointed workflow state at **wave boundaries** — after all tasks in a scheduling wave finished. That is correct for serial dependency chains (each task is its own wave), but under real concurrency a wave contains several tasks running at once. If a worker crashed mid-wave — or one long-running task held the wave open — every task that had *already completed inside that wave* was absent from the latest durable checkpoint and was **re-executed** after recovery, wasting LLM tokens and tool execution time.

After Phase 6E, the scheduler emits a durable checkpoint **the moment each task reaches a terminal state**. A recovered workflow now resumes from the last completed *task*, not the last completed *wave*.

## The Problem (before 6E)

```
Wave 1: [A, B, C] running concurrently (D depends on all three)
  t=10s  A completes          ← not yet durable
  t=20s  B completes          ← not yet durable
  t=25s  💥 WORKER CRASH (C still running)
Recovery: latest checkpoint predates wave 1 → A and B re-executed
```

## The Solution (after 6E)

```
Wave 1: [A, B, C] running concurrently (D depends on all three)
  t=10s  A completes → durable task checkpoint written
  t=20s  B completes → durable task checkpoint written
  t=25s  💥 WORKER CRASH (C still running)
Recovery: latest checkpoint has A/B COMPLETED, C/D PENDING
          → A and B skipped, C re-executed, D continues  ✓
```

## How It Works

1. **Per-task checkpoint emission** (`MultiAgentExecutor._execute_wave`):
   - Each submitted task future gets a done-callback (`_on_task_terminal`).
   - The moment a task's worker thread finishes, the callback emits a
     checkpoint snapshot containing **all** tasks' current records.
2. **Snapshot safety (no torn reads)**:
   - Records of tasks whose futures have completed are dumped live — their
     worker threads have returned, so the record is fully written
     (happens-before via future completion).
   - Records of tasks whose futures have NOT completed are captured as fresh
     **PENDING stubs** instead of live records. A mid-finalization record
     could otherwise be observed as COMPLETED before its output fields were
     written, which would make a resumed run skip a task whose output was
     never checkpointed. Stubbing as PENDING is always safe: the task is
     re-executed (at-least-once) instead of wrongly skipped.
   - Tasks outside the current wave (terminal records restored from a
     previous checkpoint) are dumped live — only the main thread mutates
     them, and it does not mutate them during a wave.
3. **Resume** (unchanged Phase 6D mechanics): recovered job →
   `resume_from_checkpoint=True` → checkpoint loaded →
   `preexisting_records` seeded → scheduler executes only PENDING tasks.
4. **Monotonic checkpoint ordering** (`WorkflowCheckpointer.save_after_node`):
   per-task checkpoints are emitted concurrently, so the checkpointer now
   serializes saves and guarantees strictly increasing `created_at` in save
   order.  Without this, two checkpoints written within the same clock
   microsecond could tie on `ORDER BY created_at DESC LIMIT 1`, making a
   STALE snapshot win and a resumed workflow lose completed tasks.
   Regression-tested with concurrent readers/writers on both the in-memory
   and real-PostgreSQL stores.
4. **PostgreSQL persistence** (unchanged Phase 6C mechanics): the checkpoint
   callback writes through `WorkflowCheckpointer` → `DbCheckpointStore`,
   so per-task checkpoints survive worker/process restarts exactly like
   wave-boundary checkpoints did.

## Code Changes

**`src/aegisforge/workflows/scheduler.py`**
- `_execute_wave`: registers a done-callback per task future (mapping is
  registered *before* the callback so the callback can always resolve which
  task completed); forwards `errors` into the wave for snapshot context.
- `_save_task_checkpoint` (new): builds the safe snapshot and emits it via
  the existing checkpoint callback; records the per-task metric.
- `execute`: counts tasks restored from a checkpoint and not re-executed.

**`src/aegisforge/observability/metrics.py`**
- Two new bounded counters (see Metrics below).

## Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `workflow_task_checkpoints_total` | Counter | `status` (completed/failed/timeout/denied) | Per-task durable checkpoints emitted as tasks reach terminal state |
| `workflow_tasks_skipped_on_resume_total` | Counter | `status` (completed/failed/timeout/denied) | Tasks restored from a durable checkpoint and not re-executed |

Both labels are bounded — no task IDs, no user data, no high cardinality.

## Alert Rules

| Alert | Condition | Severity |
|-------|-----------|----------|
| HighTaskResumeSkipRate | `rate(workflow_tasks_skipped_on_resume_total{status="completed"}[10m]) > 0.5` for 10m | warning |

Sustained high skip rates mean the same work is being redone constantly —
typically repeated mid-wave worker crashes or infrastructure instability.

## Testing

```bash
# Requires PostgreSQL running (docker compose up -d postgres)
pytest tests/test_task_level_checkpointing.py -v
```

### Test Coverage

| Test | What It Verifies |
|------|-----------------|
| `test_per_task_checkpoint_emitted_after_each_task_completes` | A checkpoint is emitted per task completion (task granularity) |
| `test_mid_wave_crash_preserves_completed_tasks` | Crash mid-wave → A/B NOT re-executed, C/D continue, dependency inputs resolve |
| `test_resume_dependency_chain_skips_completed_upstream` | Dependency chains resume at the first pending task, in order |
| `test_failed_task_not_reexecuted_on_resume` | Failed tasks are terminal; REQUIRE_ALL_DEPENDENCIES policy applies on resume |
| `test_metric_functions_exist` / `test_metrics_in_prometheus_output` | Bounded metrics exist and appear in /metrics |
| `test_db_checkpoint_persists_across_store_instances` | Real PostgreSQL: independent engine/store sees committed checkpoints |
| `test_checkpoint_persists_across_process_boundary` | Real PostgreSQL: checkpoint written by a **separate OS process** (subprocess) is loadable |
| `test_db_checkpoint_tenant_isolation` | Resume loads only the correct tenant's checkpoint |
| `test_worker_crash_recovery_task_level_resume_against_postgres` | End-to-end: mid-wave crash → durable PG checkpoint → new runtime resumes with skips |
| `test_in_memory_concurrent_reader_never_regresses` / `test_postgres_concurrent_reader_never_regresses` | Concurrent reader never observes an older checkpoint than one it already saw |
| `test_in_memory_concurrent_writers_last_save_wins` / `test_postgres_concurrent_writers_last_save_wins` | After concurrent writers, the last completed save is the one a fresh reader sees (no stale-snapshot win) |

## Security Verification

- **Tenant isolation**: per-task checkpoints carry `organization_id` through
  the same `WorkflowCheckpointer` path; the DB store loads strictly by
  `workflow_id`. Proven by `test_db_checkpoint_tenant_isolation`.
- **No secrets in checkpoints**: snapshots pass through the existing
  `_sanitize_state()` sanitization (no API keys, passwords, tokens).
- **Bounded metrics only**: `status` labels are a fixed 4-value enum.
- **Same auth/worker chain**: per-task checkpoints reuse the checkpoint
  callback wiring already used by the worker handler — no bypass paths.

## Semantics Guarantees

- **At-least-once execution is preserved and explicit.** A task that was
  mid-flight at crash time is re-executed; a task whose completion raced a
  checkpoint write may also be re-executed (conservative stub rule). No
  exactly-once claim is made anywhere.
- **Dependency ordering is unchanged**: the topological wave scheduler is
  untouched; per-task checkpointing only affects what is *durable*, not what
  runs when.
- **Per-task retry semantics are unchanged**: a terminally failed task
  restored from a checkpoint is never re-executed; dependents follow their
  failure policies exactly as before.

## Limitations

1. **Checkpoint write volume**: one DB write per task completion (in addition
   to wave-boundary checkpoints). For plans with many fast tasks this
   increases checkpoint-store writes; the writes are small, indexed, and
   cleaned up with the workflow's checkpoints.
2. **Concurrent per-task checkpoint writes**: writes from the same workflow
   are serialized by the checkpointer with strictly monotonic timestamps (so
   the latest save always wins).  Saves from *different* processes for the
   same workflow could still theoretically tie on clock microsecond
   granularity; in practice a single worker owns a workflow at a time.
   Worst case under any tie: the missed task is re-executed
   (at-least-once). Never a wrong skip.
3. **In-flight tasks are still lost**: only *completed* tasks are durable.
   The task executing at the moment of the crash restarts from scratch.
4. **Single PostgreSQL instance**: checkpoint durability matches the
   database's own durability; no additional HA layer is introduced.
