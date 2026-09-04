"""Tests for observability: tracing, metrics, and structured logging."""
from __future__ import annotations

from aegisforge.observability.tracing import (
    ExecutionMetrics,
    MetricsCollector,
    Tracer,
    get_metrics_collector,
    reset_metrics_collector,
)

# --- Tracer Tests ---


def test_tracer_creates_spans() -> None:
    tracer = Tracer(request_id="req-1", workflow_id="wf-1")
    with tracer.span("test_span") as s:
        s.attributes["key"] = "value"
    assert len(tracer.context.spans) == 1
    assert tracer.context.spans[0].name == "test_span"
    assert tracer.context.spans[0].duration_ms >= 0
    assert tracer.context.spans[0].status == "ok"


def test_tracer_multiple_spans() -> None:
    tracer = Tracer(request_id="req-1")
    with tracer.span("span_1"):
        pass
    with tracer.span("span_2"):
        pass
    assert len(tracer.context.spans) == 2


def test_tracer_span_exception() -> None:
    tracer = Tracer(request_id="req-1")
    try:
        with tracer.span("failing_span"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert tracer.context.spans[0].status == "error"
    assert "boom" in tracer.context.spans[0].attributes.get("error", "")


def test_tracer_log_summary() -> None:
    tracer = Tracer(request_id="req-1", workflow_id="wf-1")
    with tracer.span("op1"):
        pass
    summary = tracer.log_summary()
    assert summary["request_id"] == "req-1"
    assert summary["span_count"] == 1
    assert summary["total_duration_ms"] >= 0


def test_tracer_context_total_duration() -> None:
    tracer = Tracer(request_id="req-1")
    with tracer.span("s1"):
        pass
    with tracer.span("s2"):
        pass
    total = tracer.context.total_duration_ms()
    assert total >= 0


def test_tracer_get_span_by_name() -> None:
    tracer = Tracer(request_id="req-1")
    with tracer.span("find_me"):
        pass
    span = tracer.context.get_span_by_name("find_me")
    assert span is not None
    assert span.name == "find_me"


def test_tracer_get_span_by_name_missing() -> None:
    tracer = Tracer(request_id="req-1")
    assert tracer.context.get_span_by_name("nonexistent") is None


# --- Metrics Collector Tests ---


def test_metrics_collector_create() -> None:
    collector = MetricsCollector()
    metrics = collector.create(request_id="req-1", workflow_id="wf-1")
    assert metrics.request_id == "req-1"
    assert metrics.workflow_id == "wf-1"


def test_metrics_collector_get() -> None:
    collector = MetricsCollector()
    collector.create(request_id="req-1")
    metrics = collector.get("req-1")
    assert metrics is not None


def test_metrics_collector_list() -> None:
    collector = MetricsCollector()
    collector.create(request_id="req-1")
    collector.create(request_id="req-2")
    all_metrics = collector.list_metrics()
    assert len(all_metrics) == 2


def test_metrics_collector_summary() -> None:
    collector = MetricsCollector()
    m1 = collector.create(request_id="req-1")
    m1.workflow_duration_ms = 100
    m1.tool_call_count = 5
    m1.final_status = "completed"

    m2 = collector.create(request_id="req-2")
    m2.workflow_duration_ms = 200
    m2.failure_count = 1
    m2.final_status = "failed"

    summary = collector.get_summary()
    assert summary["count"] == 2
    assert summary["avg_workflow_duration_ms"] == 150
    assert summary["total_tool_calls"] == 5
    assert summary["total_failures"] == 1


def test_metrics_collector_empty_summary() -> None:
    collector = MetricsCollector()
    summary = collector.get_summary()
    assert summary["count"] == 0


def test_global_metrics_collector() -> None:
    reset_metrics_collector()
    collector = get_metrics_collector()
    assert isinstance(collector, MetricsCollector)
    reset_metrics_collector()


# --- ExecutionMetrics Tests ---


def test_execution_metrics_to_dict() -> None:
    metrics = ExecutionMetrics(
        request_id="req-1",
        planner_latency_ms=50,
        retrieval_latency_ms=100,
        tool_call_count=3,
        retry_count=1,
        final_status="completed",
    )
    d = metrics.to_dict()
    assert d["request_id"] == "req-1"
    assert d["planner_latency_ms"] == 50
    assert d["tool_call_count"] == 3
    assert d["final_status"] == "completed"
