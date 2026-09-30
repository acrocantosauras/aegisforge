# Phase 6D: Durable Workflow Resume After Worker Crash

> **Phase 6E update:** Task-level durable checkpointing is now implemented on top of
> this phase — see [Phase 6E](phase6e-task-level-checkpointing.md). Phase 6D's
> wave-boundary resume remains the baseline; 6E narrows the crash window from
> "everything since the last wave" to "everything since the last task completion".

## Overview

AegisForge Phase 6D closes the most critical durability gap in the distributed worker system: **workflow resume from durable checkpoints after worker crash**.

Before Phase 6D, when a worker crashed mid-workflow and the job was recovered by Phase 6C's crash recovery mechanism, the new worker restarted the entire workflow from scratch — losing all work completed before the failure. This wasted LLM tokens, tool execution time, and could produce inconsistent results.

After Phase 6D, recovered workflows **resume from the last completed node** using DB-backed checkpoints, skipping already-completed tasks.

## Architecture

### The Problem

```
Worker A: validate → plan → execute(task1) → execute(task2) 💥 CRASH
                                    ↑ checkpoint saved
Worker B (recovery): validate → plan → execute(task1) → execute(task2)
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                     BEFORE Phase 6D: restarts from scratch
```

### The Solution

```
Worker A: validate → plan → execute(task1) → execute(task2) 💥 CRASH
                                    ↑ checkpoint saved
Worker B (recovery): load checkpoint → execute(task2) ← resumes here
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                     AFTER Phase 6D: continues from last checkpoint
```

### How It Works

1. **Checkpointing** (already existed in Phase 6C):
   - After each workflow node completes, the `WorkflowCheckpointer` saves state to PostgreSQL
   - State includes: plan, task records, agent results, errors, retry counts
   - Checkpoints are sanitized (no secrets, no API keys)

2. **Crash Recovery** (already existed in Phase 6C):
   - Worker crash → claim expires → `recover_expired_claims()` re-enqueues the job
   - Job `retry_count` is incremented

3. **Resume** (NEW in Phase 6D):
   - When the worker handler sees `retry_count > 0`, it passes `resume_from_checkpoint=True` to `execute_workflow`
   - `execute_workflow` loads the latest checkpoint state
   - `MultiAgentExecutor` receives `preexisting_records` from the checkpoint
   - Already-completed tasks are skipped; only pending tasks are executed
   - The workflow continues from where it left off

### Code Changes

**Worker handler** (`src/aegisforge/worker.py`):
```python
is_retry_resume = job.retry_count > 0
if is_retry_resume:
    logger.info("Job %s is retry #%d — resuming from checkpoint", ...)
    record_workflow_checkpoint_resume()

final_state = execute_workflow(
    ...,
    resume_from_checkpoint=is_retry_resume,
    ...
)
```

**MultiAgentExecutor** (`src/aegisforge/workflows/scheduler.py`):
- Already handles `preexisting_records` parameter
- Seeds `TaskExecutionRecord` from checkpoint for completed tasks
- Only processes tasks whose status is `PENDING`

## Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `workflow_checkpoint_resumes_total` | Counter | Total workflows resumed from durable checkpoints |

## Alert Rules

| Alert | Condition | Severity |
|-------|-----------|----------|
| ExcessiveCheckpointResumes | `rate(workflow_checkpoint_resumes_total[5m]) > 0.1` for 10m | warning |

High resume rates may indicate frequent worker crashes or a systemic infrastructure issue.

## Testing

```bash
# Run Phase 6D tests
pytest tests/test_durable_workflow_resume.py -v

# Run all tests
pytest tests/ -q
```

### Test Coverage

| Test | What It Verifies |
|------|-----------------|
| `test_retry_count_zero_no_resume` | First-time jobs don't trigger resume |
| `test_retry_count_positive_enables_resume` | Retry jobs trigger resume |
| `test_crash_recovery_sets_retry_count` | Crash recovery increments retry_count |
| `test_save_and_load_checkpoint` | Checkpoint persistence round-trips |
| `test_load_resume_state_returns_none_when_empty` | No checkpoint → fresh start |
| `test_checkpoint_preserves_task_records` | Task records survive checkpoint |
| `test_completed_tasks_not_reexecuted` | Completed tasks are skipped on resume |
| `test_all_completed_no_tasks_run` | All-completed plans complete immediately |
| `test_dependency_chain_resume` | Dependency chains resume correctly |
| `test_metric_function_exists` | Prometheus metric function exists |
| `test_metric_in_prometheus_output` | Metric appears in /metrics output |
| `test_crash_recovery_resume_flow` | Full end-to-end crash → recovery → resume |
| `test_resume_preserves_tenant_isolation` | Resume only loads correct org's checkpoint |

## Limitations

1. **In-flight work is lost**: If the worker crashes while executing a task, that task's partial computation is lost. The task will be re-executed from scratch on resume. *(Phase 6E narrows this: tasks that had already completed when the crash occurred are now preserved even mid-wave — only the task executing at the moment of the crash is lost.)*

2. **No exactly-once**: Under narrow failure races, a task could be executed twice. External side effects must remain idempotent.

3. **Checkpoint staleness**: If the checkpoint store (PostgreSQL) is unavailable when the crash occurs, checkpoints may be lost. The workflow would restart from scratch.

4. **Single-task workflows**: Legacy single-task workflows (Phase 0-4) also benefit from checkpoint resume, but their checkpoint granularity is coarser.

## Security Verification

- **Tenant isolation**: Checkpoint `organization_id` is preserved through resume. Workflows from different tenants never share checkpoints.
- **No secrets in checkpoints**: The `_sanitize_state()` function removes API keys, passwords, and tokens before checkpointing.
- **Same auth chain**: Resume uses the same worker handler and job queue as initial execution — no bypass paths.
