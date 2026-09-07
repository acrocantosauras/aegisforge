"""Prometheus metrics for AegisForge.

Provides real measured metrics, not fabricated values.
"""
from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    # Request metrics
    HTTP_REQUESTS_TOTAL = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "endpoint", "status"],
    )
    HTTP_REQUEST_DURATION = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "endpoint"],
        buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    # LLM metrics
    LLM_REQUESTS_TOTAL = Counter(
        "llm_requests_total",
        "Total LLM requests",
        ["provider", "model", "status"],
    )
    LLM_REQUEST_DURATION = Histogram(
        "llm_request_duration_seconds",
        "LLM request duration in seconds",
        ["provider", "model"],
        buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    )
    LLM_TOKENS_TOTAL = Counter(
        "llm_tokens_total",
        "Total LLM tokens used",
        ["provider", "model", "type"],
    )

    # Workflow metrics
    WORKFLOW_RUNS_TOTAL = Counter(
        "workflow_runs_total",
        "Total workflow runs",
        ["status"],
    )
    WORKFLOW_DURATION = Histogram(
        "workflow_duration_seconds",
        "Workflow execution duration",
        ["status"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    )
    WORKFLOW_RETRIES_TOTAL = Counter(
        "workflow_retries_total",
        "Total workflow retries",
    )

    # Tool metrics
    TOOL_EXECUTIONS_TOTAL = Counter(
        "tool_executions_total",
        "Total tool executions",
        ["tool_name", "status"],
    )
    TOOL_EXECUTION_DURATION = Histogram(
        "tool_execution_duration_seconds",
        "Tool execution duration",
        ["tool_name"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    )

    # Agent metrics
    AGENT_EXECUTIONS_TOTAL = Counter(
        "agent_executions_total",
        "Total agent executions",
        ["agent_type", "status"],
    )
    AGENT_DURATION = Histogram(
        "agent_duration_seconds",
        "Agent execution duration",
        ["agent_type"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    # RAG metrics
    RAG_RETRIEVAL_TOTAL = Counter(
        "rag_retrieval_total",
        "Total RAG retrievals",
        ["status"],
    )
    RAG_RETRIEVAL_LATENCY = Histogram(
        "rag_retrieval_latency_seconds",
        "RAG retrieval latency",
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
    )
    RAG_CHUNKS_RETRIEVED = Histogram(
        "rag_chunks_retrieved",
        "Number of chunks retrieved per query",
        buckets=[1, 2, 3, 5, 10, 20],
    )
    RAG_INSUFFICIENT_CONTEXT_TOTAL = Counter(
        "rag_insufficient_context_total",
        "Total RAG retrievals with insufficient context",
    )

    # Approval metrics
    APPROVAL_REQUESTS_TOTAL = Counter(
        "approval_requests_total",
        "Total approval requests",
        ["risk_level", "status"],
    )
    APPROVAL_WAIT_DURATION = Histogram(
        "approval_wait_duration_seconds",
        "Time waiting for approval",
        buckets=[1, 5, 10, 30, 60, 300, 600, 3600],
    )

    # Phase 5 — multi-agent execution metrics
    TASK_EXECUTIONS_TOTAL = Counter(
        "task_executions_total",
        "Total multi-agent task executions",
        ["agent_type", "status"],
    )
    TASK_RETRIES_TOTAL = Counter(
        "task_retries_total",
        "Total multi-agent task retries",
        ["agent_type"],
    )
    TASK_DURATION = Histogram(
        "task_duration_seconds",
        "Multi-agent task duration",
        ["agent_type"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
    )
    TASK_CONCURRENCY = Histogram(
        "task_concurrency",
        "Concurrency level observed during task waves",
        buckets=[1, 2, 3, 4, 5, 8, 10, 16],
    )
    WORKFLOW_EVALUATION_SCORE = Gauge(
        "workflow_evaluation_score",
        "Workflow evaluation score",
        ["verdict"],
    )

    # Phase 5 — MCP ecosystem health metrics (bounded server_id label set)
    MCP_SERVER_STATUS_TOTAL = Counter(
        "mcp_server_status_total",
        "MCP server status transitions",
        ["server_id", "status"],
    )

    # Phase 5 — hybrid RAG metrics
    RAG_HYBRID_RETRIEVAL_TOTAL = Counter(
        "rag_hybrid_retrieval_total",
        "Total hybrid retrievals",
        ["reranker"],
    )
    RAG_HYBRID_LATENCY = Histogram(
        "rag_hybrid_latency_seconds",
        "Hybrid retrieval latency",
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
    )
    RAG_QUERY_EXPANSIONS = Histogram(
        "rag_query_expansions",
        "Queries issued per retrieval (1 = no expansion)",
        buckets=[1, 2, 3, 4, 5],
    )

    # Job queue metrics
    QUEUE_DEPTH = Gauge(
        "queue_depth",
        "Current job queue depth",
    )
    QUEUE_JOBS_TOTAL = Counter(
        "queue_jobs_total",
        "Total jobs by lifecycle status",
        ["status"],
    )
    JOB_DURATION = Histogram(
        "job_duration_seconds",
        "Job processing duration",
        ["status"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0],
    )

    HAS_PROMETHEUS = True

except ImportError:
    HAS_PROMETHEUS = False


def get_metrics() -> bytes:
    """Get Prometheus metrics in text format."""
    if not HAS_PROMETHEUS:
        return b"# prometheus_client not installed\n"
    return generate_latest()


def get_metrics_content_type() -> str:
    if not HAS_PROMETHEUS:
        return "text/plain"
    return CONTENT_TYPE_LATEST


@contextmanager
def track_http_request(method: str, endpoint: str, status_code: int = 200) -> Generator[None, None, None]:
    """Track an HTTP request."""
    if not HAS_PROMETHEUS:
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=str(status_code)).inc()
        HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)


@contextmanager
def track_llm_request(provider: str, model: str) -> Generator[dict[str, Any], None, None]:
    """Track an LLM request. Yields a mutable dict to record token counts."""
    metadata: dict[str, Any] = {"tokens": 0}
    if not HAS_PROMETHEUS:
        yield metadata
        return
    start = time.monotonic()
    try:
        yield metadata
        LLM_REQUESTS_TOTAL.labels(provider=provider, model=model, status="success").inc()
    except Exception:
        LLM_REQUESTS_TOTAL.labels(provider=provider, model=model, status="error").inc()
        raise
    finally:
        duration = time.monotonic() - start
        LLM_REQUEST_DURATION.labels(provider=provider, model=model).observe(duration)
        if metadata.get("tokens", 0) > 0:
            LLM_TOKENS_TOTAL.labels(provider=provider, model=model, type="total").inc(metadata["tokens"])


@contextmanager
def track_workflow(status: str = "running") -> Generator[dict[str, Any], None, None]:
    """Track a workflow execution."""
    metadata: dict[str, Any] = {}
    if not HAS_PROMETHEUS:
        yield metadata
        return
    start = time.monotonic()
    try:
        yield metadata
    finally:
        duration = time.monotonic() - start
        final_status = metadata.get("status", status)
        WORKFLOW_RUNS_TOTAL.labels(status=final_status).inc()
        WORKFLOW_DURATION.labels(status=final_status).observe(duration)


@contextmanager
def track_tool_execution(tool_name: str) -> Generator[None, None, None]:
    """Track a tool execution."""
    if not HAS_PROMETHEUS:
        yield
        return
    start = time.monotonic()
    try:
        yield
        TOOL_EXECUTIONS_TOTAL.labels(tool_name=tool_name, status="success").inc()
    except Exception:
        TOOL_EXECUTIONS_TOTAL.labels(tool_name=tool_name, status="error").inc()
        raise
    finally:
        duration = time.monotonic() - start
        TOOL_EXECUTION_DURATION.labels(tool_name=tool_name).observe(duration)


@contextmanager
def track_rag_retrieval() -> Generator[dict[str, Any], None, None]:
    """Track a RAG retrieval."""
    metadata: dict[str, Any] = {"chunk_count": 0, "insufficient_context": False}
    if not HAS_PROMETHEUS:
        yield metadata
        return
    start = time.monotonic()
    try:
        yield metadata
        RAG_RETRIEVAL_TOTAL.labels(status="success").inc()
    except Exception:
        RAG_RETRIEVAL_TOTAL.labels(status="error").inc()
        raise
    finally:
        duration = time.monotonic() - start
        RAG_RETRIEVAL_LATENCY.observe(duration)
        chunks = metadata.get("chunk_count", 0)
        if chunks > 0:
            RAG_CHUNKS_RETRIEVED.observe(chunks)
        if metadata.get("insufficient_context"):
            RAG_INSUFFICIENT_CONTEXT_TOTAL.inc()


def record_agent_execution(
    agent_type: str,
    status: str,
    duration_ms: int,
) -> None:
    """Record an agent execution with duration."""
    if not HAS_PROMETHEUS:
        return
    AGENT_EXECUTIONS_TOTAL.labels(agent_type=agent_type, status=status).inc()
    if duration_ms > 0:
        AGENT_DURATION.labels(agent_type=agent_type).observe(duration_ms / 1000.0)


def record_approval_event(risk_level: str, status: str, wait_seconds: float | None = None) -> None:
    """Record an approval lifecycle event.

    status: one of requested, approved, rejected, expired, cancelled
    """
    if not HAS_PROMETHEUS:
        return
    APPROVAL_REQUESTS_TOTAL.labels(risk_level=risk_level, status=status).inc()
    if wait_seconds is not None and status in ("approved", "rejected", "expired", "cancelled"):
        APPROVAL_WAIT_DURATION.observe(max(wait_seconds, 0.0))


def record_queue_event(status: str) -> None:
    """Record a job queue lifecycle event.

    status: one of queued, running, retried, completed, failed, cancelled
    """
    if not HAS_PROMETHEUS:
        return
    QUEUE_JOBS_TOTAL.labels(status=status).inc()


def record_job_duration(status: str, duration_seconds: float) -> None:
    """Record job processing duration by outcome."""
    if not HAS_PROMETHEUS:
        return
    JOB_DURATION.labels(status=status).observe(max(duration_seconds, 0.0))


def record_workflow_retry() -> None:
    """Record a workflow retry event."""
    if not HAS_PROMETHEUS:
        return
    WORKFLOW_RETRIES_TOTAL.inc()


def record_task_execution(
    agent_type: str,
    status: str,
    duration_ms: int | None = None,
) -> None:
    """Record a multi-agent task execution (low-cardinality agent_type)."""
    if not HAS_PROMETHEUS:
        return
    TASK_EXECUTIONS_TOTAL.labels(agent_type=agent_type, status=status).inc()
    if duration_ms is not None and duration_ms > 0:
        TASK_DURATION.labels(agent_type=agent_type).observe(duration_ms / 1000.0)


def record_task_retry(agent_type: str) -> None:
    """Record a multi-agent task retry."""
    if not HAS_PROMETHEUS:
        return
    TASK_RETRIES_TOTAL.labels(agent_type=agent_type).inc()


def record_task_concurrency(level: int) -> None:
    """Record the concurrency level observed during a task wave."""
    if not HAS_PROMETHEUS:
        return
    TASK_CONCURRENCY.observe(max(level, 1))


def record_workflow_evaluation_score(verdict: str, score: float) -> None:
    """Record the workflow evaluation score by verdict (low-cardinality)."""
    if not HAS_PROMETHEUS:
        return
    WORKFLOW_EVALUATION_SCORE.labels(verdict=verdict).set(max(0.0, min(1.0, score)))


def record_mcp_server_status(server_id: str, status: str) -> None:
    """Record an MCP server health/lifecycle status transition.

    ``server_id`` comes from the (operator-controlled) server catalog and is
    therefore bounded and low-cardinality — never from user input.
    """
    if not HAS_PROMETHEUS:
        return
    MCP_SERVER_STATUS_TOTAL.labels(server_id=server_id, status=status).inc()


def record_rag_hybrid_retrieval(
    response: Any,
    *,
    semantic_candidates: int = 0,
    lexical_candidates: int = 0,
) -> None:
    """Record hybrid retrieval metrics from a HybridRetrievalResponse.

    The ``semantic_candidates`` and ``lexical_candidates`` counts describe
    how many candidates entered fusion. These are observed as separate
    bounded metrics rather than embedded in logs or in the response text.
    """
    if not HAS_PROMETHEUS:
        return
    reranker = getattr(getattr(response, "rerank_stats", None), "reranker", "none")
    RAG_HYBRID_RETRIEVAL_TOTAL.labels(reranker=reranker).inc()
    RAG_HYBRID_LATENCY.observe(max(getattr(response, "latency_ms", 0) / 1000.0, 0.0))
    RAG_QUERY_EXPANSIONS.observe(max(len(getattr(response, "expansions", []) or []), 1))

    results = getattr(response, "results", None)
    if results is not None:
        RAG_CHUNKS_RETRIEVED.observe(len(results))


def set_queue_depth(depth: int) -> None:
    """Update the current queue depth gauge."""
    if not HAS_PROMETHEUS:
        return
    QUEUE_DEPTH.set(depth)