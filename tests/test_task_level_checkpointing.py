"""Phase 6E: Task-level durable checkpointing.

Phase 6C/6D checkpointed workflow state at WAVE boundaries.  When a worker
crashed mid-wave (or one long-running task held a wave open), tasks that had
already completed inside the wave were lost and re-executed on resume.

Phase 6E closes that gap: the scheduler emits a durable checkpoint the moment
each task reaches a terminal state.  These tests prove:

1. A per-task checkpoint is emitted as each task completes
2. A mid-wave crash preserves completed tasks (A/B are NOT re-executed)
3. Pending tasks continue normally and dependency ordering holds
4. Failed tasks follow existing retry semantics (terminal, not re-executed)
5. Real PostgreSQL checkpoints persist across store instances AND OS processes
6. Tenant isolation is preserved in the DB-backed checkpoint store
7. Bounded Prometheus metrics exist for task checkpoints and resume skips
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    RequestStatus,
    TaskExecutionRecord,
    TaskFailurePolicy,
)
from aegisforge.workflows.checkpoint import (
    DbCheckpointStore,
    InMemoryCheckpointStore,
    WorkflowCheckpointer,
)
from aegisforge.workflows.scheduler import (
    ExecutionConfig,
    MultiAgentExecutor,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get(
    "AEGISFORGE_TEST_DATABASE_URL",
    "postgresql+psycopg://aegisforge:aegisforge@localhost:5432/aegisforge",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    """Check if a real PostgreSQL instance is reachable."""
    try:
        from sqlalchemy import text

        from aegisforge.db.session import get_engine

        engine = get_engine(PG_URL)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="Task-level checkpointing persistence tests require real PostgreSQL",
)


class TrackingAgent:
    """Agent that records which task it executed and with what input."""

    def __init__(
        self,
        agent_type: AgentType,
        executed: list[str],
        received: dict[str, dict[str, Any]],
        block_event: threading.Event | None = None,
        block_task_id: str = "",
    ) -> None:
        self.name = "tracking"
        self.agent_type = agent_type
        self._executed = executed
        self._received = received
        self._block_event = block_event
        self._block_task_id = block_task_id

    def execute(self, input_data: dict[str, Any], context: Any) -> AgentResult:
        self._executed.append(context.task_id)
        self._received[context.task_id] = dict(input_data)
        if self._block_event is not None and context.task_id == self._block_task_id:
            self._block_event.wait(timeout=40)
        return AgentResult(
            agent_name="tracking",
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Completed {context.task_id}",
            result={"answer": f"answer-{context.task_id}"},
        )


class TrackingFactory:
    """Agent factory that tracks executions (for resume/no-re-execution checks)."""

    def __init__(
        self,
        executed: list[str],
        received: dict[str, dict[str, Any]],
        block_event: threading.Event | None = None,
        block_task_id: str = "",
    ) -> None:
        self._executed = executed
        self._received = received
        self._block_event = block_event
        self._block_task_id = block_task_id

    @property
    def registry(self) -> Any:
        from aegisforge.tools.registry import ToolRegistry

        return ToolRegistry()

    def build(self, task: ExecutionPlanTask, run_id: str) -> TrackingAgent:
        return TrackingAgent(
            task.assigned_agent_type,
            self._executed,
            self._received,
            block_event=self._block_event,
            block_task_id=self._block_task_id,
        )


class SnapshotCollector:
    """Thread-safe checkpoint callback capturing every emitted snapshot."""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, snapshot: dict[str, Any]) -> None:
        # Deep-copy so later in-place mutations of records never leak in.
        frozen = json.loads(json.dumps(snapshot))
        with self._lock:
            self.snapshots.append(frozen)

    def find_snapshot(
        self,
        completed: list[str],
        pending: list[str],
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Poll until a snapshot shows ``completed`` tasks done and ``pending`` tasks not."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for snap in self.snapshots:
                    records = snap.get("task_records", {})
                    if all(
                        records.get(tid, {}).get("status") == "completed"
                        for tid in completed
                    ) and all(
                        records.get(tid, {}).get("status") == "pending"
                        for tid in pending
                    ):
                        return snap
            time.sleep(0.05)
        return None


def _make_plan(
    tasks: list[ExecutionPlanTask],
    plan_id: str | None = None,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id or f"plan-{uuid.uuid4().hex[:10]}",
        request_id="req-6e",
        tasks=tasks,
    )


def _task(
    task_id: str,
    *,
    dependencies: list[str] | None = None,
    input_references: dict[str, str] | None = None,
    failure_policy: TaskFailurePolicy = TaskFailurePolicy.CONTINUE_WITH_PARTIAL_RESULTS,
    max_retries: int = 0,
) -> ExecutionPlanTask:
    return ExecutionPlanTask(
        task_id=task_id,
        description=f"Task {task_id}",
        assigned_agent_type=AgentType.RESEARCH,
        dependencies=dependencies or [],
        input_references=input_references or {},
        timeout_seconds=10.0,
        max_retries=max_retries,
        failure_policy=failure_policy,
    )


def _new_executor(
    executed: list[str],
    received: dict[str, dict[str, Any]],
    collector: SnapshotCollector | None = None,
    block_event: threading.Event | None = None,
    block_task_id: str = "",
) -> MultiAgentExecutor:
    return MultiAgentExecutor(
        config=ExecutionConfig(
            max_concurrency=4,
            default_task_timeout_seconds=10.0,
        ),
        agent_factory=TrackingFactory(
            executed,
            received,
            block_event=block_event,
            block_task_id=block_task_id,
        ),
        checkpoint_callback=collector,
    )


# ---------------------------------------------------------------------------
# Test: per-task checkpoints are emitted as tasks complete
# ---------------------------------------------------------------------------


class TestPerTaskCheckpointEmission:
    def test_per_task_checkpoint_emitted_after_each_task_completes(self) -> None:
        """Each task completion emits a durable checkpoint (task granularity)."""
        plan = _make_plan([_task("t1"), _task("t2"), _task("t3")])
        collector = SnapshotCollector()
        executed: list[str] = []
        received: dict[str, dict[str, Any]] = {}

        executor = _new_executor(executed, received, collector=collector)
        outcome = executor.execute(
            plan,
            request_id="req-6e",
            workflow_id="wf-6e-per-task",
            organization_id="org-test",
        )

        assert outcome.status == RequestStatus.COMPLETED
        # At least one per-task checkpoint per completed task (3 tasks), plus
        # the wave-boundary checkpoint(s) that already existed in Phase 6C.
        assert len(collector.snapshots) >= 3
        # The last snapshot must show all three tasks completed.
        final = collector.snapshots[-1]["task_records"]
        for tid in ("t1", "t2", "t3"):
            assert final[tid]["status"] == "completed"
            assert final[tid]["output"]["answer"] == f"answer-{tid}"

    def test_mid_wave_crash_preserves_completed_tasks(self) -> None:
        """Crash mid-wave: A/B completed, C stuck → A/B are NOT re-executed."""
        plan = _make_plan(
            [
                _task("t1"),
                _task("t2"),
                _task("t3"),
                _task(
                    "t4",
                    dependencies=["t1", "t2", "t3"],
                    input_references={"prior_answer": "t3.answer"},
                ),
            ]
        )
        collector = SnapshotCollector()
        block_event = threading.Event()
        executed_1: list[str] = []
        received_1: dict[str, dict[str, Any]] = {}

        executor_1 = _new_executor(
            executed_1, received_1, collector=collector,
            block_event=block_event, block_task_id="t3",
        )
        run_thread = threading.Thread(
            target=executor_1.execute,
            kwargs={
                "plan": plan,
                "request_id": "req-6e",
                "workflow_id": "wf-6e-midwave",
                "organization_id": "org-test",
            },
            daemon=True,
        )
        run_thread.start()

        try:
            # Worker "crashes" while t3 is still running.  The latest durable
            # checkpoint must already contain t1 and t2 as COMPLETED.
            crash_snapshot = collector.find_snapshot(
                completed=["t1", "t2"], pending=["t3"]
            )
        finally:
            block_event.set()  # let the "crashed" run finish and release threads
        run_thread.join(timeout=45)

        assert crash_snapshot is not None, (
            "No per-task checkpoint captured t1/t2 completed while t3 pending"
        )
        crash_records = crash_snapshot["task_records"]
        assert crash_records["t1"]["output"]["answer"] == "answer-t1"
        assert crash_records["t2"]["output"]["answer"] == "answer-t2"
        assert crash_records["t4"]["status"] == "pending"

        # Recovery: a NEW worker resumes from the crash-time checkpoint.
        executed_2: list[str] = []
        received_2: dict[str, dict[str, Any]] = {}
        executor_2 = _new_executor(executed_2, received_2)
        outcome = executor_2.execute(
            plan,
            request_id="req-6e",
            workflow_id="wf-6e-midwave",
            organization_id="org-test",
            preexisting_records=crash_records,
        )

        assert outcome.status == RequestStatus.COMPLETED
        # Completed tasks are NOT re-executed.
        assert "t1" not in executed_2
        assert "t2" not in executed_2
        # Pending tasks continue normally.
        assert "t3" in executed_2
        assert "t4" in executed_2
        # Dependency ordering: t4 resolved its input from the freshly-run t3.
        assert received_2["t4"].get("prior_answer") == "answer-t3"

    def test_resume_dependency_chain_skips_completed_upstream(self) -> None:
        """A dependency chain resumes at the first pending task, in order."""
        plan = _make_plan(
            [
                _task("a"),
                _task("b", dependencies=["a"]),
                _task(
                    "c",
                    dependencies=["b"],
                    input_references={"upstream": "b.answer"},
                ),
            ]
        )
        preexisting = {
            "a": TaskExecutionRecord(
                task_id="a",
                description="Task a",
                agent_type=AgentType.RESEARCH,
                status=AgentExecutionStatus.COMPLETED,
                output={"answer": "answer-a"},
                summary="Done",
            ).model_dump(mode="json"),
            "b": TaskExecutionRecord(
                task_id="b",
                description="Task b",
                agent_type=AgentType.RESEARCH,
                status=AgentExecutionStatus.COMPLETED,
                output={"answer": "answer-b"},
                summary="Done",
                dependencies=["a"],
            ).model_dump(mode="json"),
        }

        executed: list[str] = []
        received: dict[str, dict[str, Any]] = {}
        executor = _new_executor(executed, received)
        outcome = executor.execute(
            plan,
            request_id="req-6e",
            workflow_id="wf-6e-chain",
            organization_id="org-test",
            preexisting_records=preexisting,
        )

        assert outcome.status == RequestStatus.COMPLETED
        assert executed == ["c"]
        assert received["c"].get("upstream") == "answer-b"


# ---------------------------------------------------------------------------
# Test: failed tasks follow existing retry semantics on resume
# ---------------------------------------------------------------------------


class TestFailedTaskSemantics:
    def test_failed_task_not_reexecuted_on_resume(self) -> None:
        """A terminally failed task is not retried by resume; policy applies."""
        plan = _make_plan(
            [
                _task("t1", failure_policy=TaskFailurePolicy.REQUIRE_ALL_DEPENDENCIES),
                _task("t2", failure_policy=TaskFailurePolicy.REQUIRE_ALL_DEPENDENCIES),
            ]
        )
        # Make t2 depend on t1 so t1's failure blocks t2.
        plan.tasks[1].dependencies = ["t1"]

        preexisting = {
            "t1": TaskExecutionRecord(
                task_id="t1",
                description="Task t1",
                agent_type=AgentType.RESEARCH,
                status=AgentExecutionStatus.FAILED,
                errors=["Permanent failure before crash"],
            ).model_dump(mode="json"),
        }

        executed: list[str] = []
        received: dict[str, dict[str, Any]] = {}
        executor = _new_executor(executed, received)
        outcome = executor.execute(
            plan,
            request_id="req-6e",
            workflow_id="wf-6e-failed",
            organization_id="org-test",
            preexisting_records=preexisting,
        )

        # The failed task is terminal — never re-executed.
        assert executed == []
        # The dependent task fails per REQUIRE_ALL_DEPENDENCIES policy.
        assert outcome.status == RequestStatus.FAILED
        assert outcome.records["t2"].status == AgentExecutionStatus.FAILED
        assert outcome.records["t1"].status == AgentExecutionStatus.FAILED
        assert outcome.records["t1"].errors == ["Permanent failure before crash"]


# ---------------------------------------------------------------------------
# Test: bounded Prometheus metrics
# ---------------------------------------------------------------------------


class TestTaskCheckpointMetrics:
    def test_metric_functions_exist(self) -> None:
        from aegisforge.observability.metrics import (
            record_workflow_task_checkpoint,
            record_workflow_tasks_skipped_on_resume,
        )

        record_workflow_task_checkpoint("completed")
        record_workflow_tasks_skipped_on_resume("completed")

    def test_metrics_in_prometheus_output(self) -> None:
        from aegisforge.observability.metrics import (
            HAS_PROMETHEUS,
            record_workflow_task_checkpoint,
            record_workflow_tasks_skipped_on_resume,
        )

        if not HAS_PROMETHEUS:
            pytest.skip("prometheus_client not installed")

        from prometheus_client import generate_latest

        record_workflow_task_checkpoint("completed")
        record_workflow_tasks_skipped_on_resume("completed")
        output = generate_latest().decode()

        assert "workflow_task_checkpoints_total" in output
        assert "workflow_tasks_skipped_on_resume_total" in output


# ---------------------------------------------------------------------------
# Test: real PostgreSQL persistence across store instances and processes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: checkpoint ordering is monotonic under concurrent per-task saves
# ---------------------------------------------------------------------------


def _pg_store_pair() -> Any:
    """Two INDEPENDENT DbCheckpointStore instances with separate engines.

    Separate engines simulate separate runtimes: a store created after the
    first one must see committed checkpoint data (new connections, no
    shared in-memory state).
    """
    from sqlalchemy.orm import sessionmaker

    from aegisforge.db.session import get_engine

    engine_a = get_engine(PG_URL)
    engine_b = get_engine(PG_URL)
    store_a = DbCheckpointStore(
        session_factory=sessionmaker(bind=engine_a, autoflush=False, autocommit=False)
    )
    store_b = DbCheckpointStore(
        session_factory=sessionmaker(bind=engine_b, autoflush=False, autocommit=False)
    )
    yield store_a, store_b
    engine_a.dispose()
    engine_b.dispose()


class TestMonotonicCheckpointOrdering:
    """Phase 6E: per-task checkpoints are saved concurrently by worker threads.

    ``load_latest_by_workflow`` orders by ``created_at``; concurrent saves
    with identical timestamps could make a STALE snapshot win, so a resumed
    workflow would lose completed tasks.  The checkpointer guarantees
    strictly monotonic created_at in save order.  These tests prove the two
    real durability invariants:

    1. A concurrent reader never observes an older checkpoint than one it
       already observed (save order == visibility order).
    2. After concurrent writers stop, the checkpoint of the LAST completed
       save is the one a fresh reader sees (latest save wins).
    """

    def _run_single_writer_concurrent_reader(self, store: Any) -> list[tuple[int, int]]:
        """Invariant 1: with a single writer, save order == seq order, so any
        seq regression observed by a concurrent reader is a staleness bug."""
        checkpointer = WorkflowCheckpointer(
            workflow_id=f"wf-6e-mono-{uuid.uuid4().hex[:8]}",
            request_id="req-6e-mono",
            store=store,
        )
        stop = threading.Event()
        regressions: list[tuple[int, int]] = []

        def reader() -> None:
            last = -1
            while not stop.is_set():
                state = checkpointer.load_resume_state()
                if state is not None:
                    seq = int(state["seq"])
                    if seq < last:
                        regressions.append((last, seq))
                    last = max(last, seq)
                time.sleep(0.001)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        for i in range(200):
            checkpointer.save_after_node("multi_agent_execute", {"seq": i})

        stop.set()
        reader_thread.join(timeout=10)
        return regressions

    def _run_concurrent_writers_last_save_wins(
        self, store: Any
    ) -> tuple[int, int, int]:
        """Invariant 2: after concurrent writers, the last COMPLETED save is
        the one a fresh reader observes.  Returns (last_saved_seq,
        final_read_seq, total_saves)."""
        checkpointer = WorkflowCheckpointer(
            workflow_id=f"wf-6e-last-{uuid.uuid4().hex[:8]}",
            request_id="req-6e-last",
            store=store,
        )
        completion_order: list[int] = []
        order_lock = threading.Lock()
        original_save = store.save_checkpoint

        def recording_save(checkpoint: Any) -> None:
            original_save(checkpoint)
            seq = int(checkpoint.state.get("seq", -1))
            with order_lock:
                completion_order.append(seq)

        store.save_checkpoint = recording_save  # type: ignore[method-assign]
        try:
            def writer(worker: int) -> None:
                for i in range(50):
                    checkpointer.save_after_node(
                        "multi_agent_execute", {"seq": worker * 50 + i}
                    )

            writers = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
            for w in writers:
                w.start()
            for w in writers:
                w.join()
        finally:
            store.save_checkpoint = original_save  # type: ignore[method-assign]

        final_state = checkpointer.load_resume_state()
        assert final_state is not None
        last_saved = completion_order[-1]
        return last_saved, int(final_state["seq"]), len(completion_order)

    def test_in_memory_concurrent_reader_never_regresses(self) -> None:
        regressions = self._run_single_writer_concurrent_reader(
            InMemoryCheckpointStore()
        )
        assert regressions == [], (
            f"Checkpoint state regressed {len(regressions)} time(s): "
            f"{regressions[:5]}"
        )

    def test_in_memory_concurrent_writers_last_save_wins(self) -> None:
        last_saved, final_read, total = self._run_concurrent_writers_last_save_wins(
            InMemoryCheckpointStore()
        )
        assert total == 200
        assert final_read == last_saved, (
            f"Reader saw seq={final_read} but the last completed save was "
            f"seq={last_saved} — a stale checkpoint won"
        )

    @requires_pg
    def test_postgres_concurrent_reader_never_regresses(
        self, pg_store_pair: Any
    ) -> None:
        store_a, _ = pg_store_pair
        regressions = self._run_single_writer_concurrent_reader(store_a)
        assert regressions == [], (
            f"Checkpoint state regressed {len(regressions)} time(s): "
            f"{regressions[:5]}"
        )

    @requires_pg
    def test_postgres_concurrent_writers_last_save_wins(
        self, pg_store_pair: Any
    ) -> None:
        store_a, _ = pg_store_pair
        last_saved, final_read, total = self._run_concurrent_writers_last_save_wins(
            store_a
        )
        assert total == 200
        assert final_read == last_saved, (
            f"Reader saw seq={final_read} but the last completed save was "
            f"seq={last_saved} — a stale checkpoint won"
        )


pg_store_pair = pytest.fixture(_pg_store_pair)


# ---------------------------------------------------------------------------
# Test: real PostgreSQL persistence across store instances and processes
# ---------------------------------------------------------------------------


class TestPostgresPersistence:
    @requires_pg
    def test_db_checkpoint_persists_across_store_instances(
        self, pg_store_pair: Any
    ) -> None:
        """A checkpoint saved via store A is loaded by an independent store B."""
        store_a, store_b = pg_store_pair
        workflow_id = f"wf-6e-pg-{uuid.uuid4().hex[:10]}"
        state = {
            "status": "executing",
            "organization_id": "org-test",
            "task_records": {
                "t1": {"task_id": "t1", "status": "completed",
                        "output": {"answer": "answer-t1"}},
                "t2": {"task_id": "t2", "status": "pending"},
            },
        }

        checkpointer_a = WorkflowCheckpointer(
            workflow_id=workflow_id,
            request_id="req-6e-pg",
            organization_id="org-test",
            store=store_a,
        )
        checkpointer_a.save_after_node("multi_agent_execute", state)

        try:
            # A brand-new runtime (store_b, fresh engine) must see the data.
            checkpointer_b = WorkflowCheckpointer(
                workflow_id=workflow_id,
                request_id="req-6e-pg",
                organization_id="org-test",
                store=store_b,
            )
            loaded = checkpointer_b.load_resume_state()
            assert loaded is not None
            assert loaded["task_records"]["t1"]["status"] == "completed"
            assert loaded["task_records"]["t1"]["output"]["answer"] == "answer-t1"
            assert loaded["task_records"]["t2"]["status"] == "pending"
        finally:
            store_a.delete_workflow_checkpoints(workflow_id)

    @requires_pg
    def test_checkpoint_persists_across_process_boundary(self) -> None:
        """A checkpoint written by a SEPARATE OS process is loadable here."""
        from sqlalchemy.orm import sessionmaker

        from aegisforge.db.session import get_engine

        workflow_id = f"wf-6e-proc-{uuid.uuid4().hex[:10]}"
        checkpoint_id = f"cp-{uuid.uuid4().hex[:12]}"
        request_id = f"req-6e-proc-{uuid.uuid4().hex[:8]}"
        state = {
            "status": "executing",
            "organization_id": "org-proc",
            "task_records": {
                "t1": {"task_id": "t1", "status": "completed",
                        "output": {"answer": "from-other-process"}},
                "t2": {"task_id": "t2", "status": "pending"},
            },
        }

        child_code = """
import json, os, sys
from sqlalchemy.orm import sessionmaker
from aegisforge.db.session import get_engine
from aegisforge.workflows.checkpoint import DbCheckpointStore, WorkflowCheckpoint

factory = sessionmaker(bind=get_engine(os.environ["PG_URL"]))
store = DbCheckpointStore(session_factory=factory)
checkpoint = WorkflowCheckpoint(
    checkpoint_id=os.environ["CHECKPOINT_ID"],
    workflow_id=os.environ["WORKFLOW_ID"],
    request_id=os.environ["REQUEST_ID"],
    node_name="multi_agent_execute",
    state=json.loads(os.environ["STATE"]),
    organization_id=os.environ["ORG_ID"],
)
store.save_checkpoint(checkpoint)
print("SAVED")
"""

        env = {
            **os.environ,
            "PG_URL": PG_URL,
            "CHECKPOINT_ID": checkpoint_id,
            "WORKFLOW_ID": workflow_id,
            "REQUEST_ID": request_id,
            "ORG_ID": "org-proc",
            "STATE": json.dumps(state),
            "PYTHONPATH": str(PROJECT_ROOT / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        try:
            result = subprocess.run(
                [sys.executable, "-c", child_code],
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert result.returncode == 0, (
                f"Child process failed: {result.stderr[-2000:]}"
            )
            assert "SAVED" in result.stdout

            # The parent process loads it with a completely fresh engine —
            # proving durable persistence across a process boundary.
            store = DbCheckpointStore(
                session_factory=sessionmaker(
                    bind=get_engine(PG_URL), autoflush=False, autocommit=False
                )
            )
            loaded = store.load_latest_by_workflow(workflow_id)
            assert loaded is not None
            assert loaded.organization_id == "org-proc"
            assert loaded.state["task_records"]["t1"]["status"] == "completed"
            assert (
                loaded.state["task_records"]["t1"]["output"]["answer"]
                == "from-other-process"
            )
        finally:
            from sqlalchemy.orm import sessionmaker as _sm

            cleanup = DbCheckpointStore(
                session_factory=_sm(bind=get_engine(PG_URL))
            )
            cleanup.delete_workflow_checkpoints(workflow_id)

    @requires_pg
    def test_db_checkpoint_tenant_isolation(self, pg_store_pair: Any) -> None:
        """Resume loads only the correct tenant's checkpoint state."""
        store_a, store_b = pg_store_pair
        wf_alpha = f"wf-6e-alpha-{uuid.uuid4().hex[:10]}"
        wf_beta = f"wf-6e-beta-{uuid.uuid4().hex[:10]}"

        try:
            WorkflowCheckpointer(
                workflow_id=wf_alpha, request_id="req-alpha",
                organization_id="org-alpha", store=store_a,
            ).save_after_node("multi_agent_execute", {
                "status": "executing",
                "organization_id": "org-alpha",
                "task_records": {
                    "t1": {"task_id": "t1", "status": "completed",
                            "output": {"answer": "alpha-secret"}},
                },
            })
            WorkflowCheckpointer(
                workflow_id=wf_beta, request_id="req-beta",
                organization_id="org-beta", store=store_a,
            ).save_after_node("multi_agent_execute", {
                "status": "executing",
                "organization_id": "org-beta",
                "task_records": {
                    "t1": {"task_id": "t1", "status": "completed",
                            "output": {"answer": "beta-secret"}},
                },
            })

            # org-alpha's resume must never see org-beta's records.
            resumed = WorkflowCheckpointer(
                workflow_id=wf_alpha, request_id="req-alpha",
                organization_id="org-alpha", store=store_b,
            ).load_resume_state()
            assert resumed is not None
            assert resumed["organization_id"] == "org-alpha"
            assert resumed["task_records"]["t1"]["output"]["answer"] == "alpha-secret"
            assert "beta-secret" not in json.dumps(resumed)
        finally:
            store_a.delete_workflow_checkpoints(wf_alpha)
            store_a.delete_workflow_checkpoints(wf_beta)

    @requires_pg
    def test_worker_crash_recovery_task_level_resume_against_postgres(
        self, pg_store_pair: Any
    ) -> None:
        """Full flow: crash mid-wave → PG checkpoint → new runtime resumes."""
        store_a, _ = pg_store_pair
        workflow_id = f"wf-6e-crash-{uuid.uuid4().hex[:10]}"
        plan = _make_plan(
            [
                _task("t1"),
                _task("t2"),
                _task("t3"),
                _task(
                    "t4",
                    dependencies=["t1", "t2", "t3"],
                    input_references={"prior_answer": "t3.answer"},
                ),
            ]
        )
        block_event = threading.Event()
        executed_1: list[str] = []
        received_1: dict[str, dict[str, Any]] = {}

        # Runtime A ("worker A"): checkpoints durably to PostgreSQL as each
        # task completes, then "crashes" while t3 is still running.
        # task completes, then "crashes" while t3 is still running.
        checkpointer_a = WorkflowCheckpointer(
            workflow_id=workflow_id,
            request_id="req-6e-crash",
            organization_id="org-test",
            store=store_a,
        )

        def durable_checkpoint(snapshot: dict[str, Any]) -> None:
            frozen = json.loads(json.dumps(snapshot))
            full_state = dict(frozen)
            full_state.update({
                "status": "executing",
                "request_id": "req-6e-crash",
                "workflow_id": workflow_id,
                "organization_id": "org-test",
                "plan": plan.model_dump(mode="json"),
            })
            checkpointer_a.save_after_node("multi_agent_execute", full_state)

        executor_1 = MultiAgentExecutor(
            config=ExecutionConfig(max_concurrency=4, default_task_timeout_seconds=10.0),
            agent_factory=TrackingFactory(
                executed_1, received_1,
                block_event=block_event, block_task_id="t3",
            ),
            checkpoint_callback=durable_checkpoint,
        )
        run_thread = threading.Thread(
            target=executor_1.execute,
            kwargs={
                "plan": plan,
                "request_id": "req-6e-crash",
                "workflow_id": workflow_id,
                "organization_id": "org-test",
            },
            daemon=True,
        )
        run_thread.start()

        try:
            deadline = time.monotonic() + 30
            crash_state: dict[str, Any] | None = None
            while time.monotonic() < deadline and crash_state is None:
                state = checkpointer_a.load_resume_state()
                records = (state or {}).get("task_records", {})
                if (
                    records.get("t1", {}).get("status") == "completed"
                    and records.get("t2", {}).get("status") == "completed"
                    and records.get("t3", {}).get("status") == "pending"
                ):
                    crash_state = state
                    break
                time.sleep(0.05)
        finally:
            block_event.set()
        run_thread.join(timeout=45)

        assert crash_state is not None, "Durable mid-wave checkpoint never appeared"

        # Runtime B ("worker B", fresh engine): loads the checkpoint and
        # resumes — t1/t2 must be skipped, t3/t4 executed.
        executed_2: list[str] = []
        received_2: dict[str, dict[str, Any]] = {}
        executor_2 = _new_executor(executed_2, received_2)
        try:
            outcome = executor_2.execute(
                plan,
                request_id="req-6e-crash",
                workflow_id=workflow_id,
                organization_id="org-test",
                preexisting_records=crash_state["task_records"],
            )

            assert outcome.status == RequestStatus.COMPLETED
            assert "t1" not in executed_2
            assert "t2" not in executed_2
            assert "t3" in executed_2
            assert "t4" in executed_2
        finally:
            store_a.delete_workflow_checkpoints(workflow_id)
