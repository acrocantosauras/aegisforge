"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import type {
  WorkflowGraph,
  WorkflowTask,
  WorkflowEvaluation,
  WorkflowResult,
} from "@/lib/api";
import {
  taskStatus,
  formatDuration,
  formatRelativeTime,
  riskBadgeClass,
} from "@/lib/status";

const STATUS_STYLES: Record<string, string> = {
  completed: "badge success",
  running: "badge running",
  executing: "badge running",
  planning: "badge running",
  queued: "badge info",
  pending: "badge pending",
  failed: "badge danger",
  timeout: "badge danger",
  denied: "badge danger",
  retrying: "badge warning",
  waiting_for_approval: "badge warning",
  action_requires_approval: "badge warning",
  cancelled: "badge muted",
};

function statusBadge(status: string) {
  return (
    <span className={STATUS_STYLES[status.toLowerCase()] ?? "badge muted"}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function agentGlyph(agentType: string): string {
  switch (agentType) {
    case "planner":
      return "◈";
    case "research":
      return "⌕";
    case "rag":
      return "▤";
    case "analysis":
      return "◎";
    case "synthesis":
      return "✦";
    case "evaluator":
      return "✓";
    default:
      return "•";
  }
}

function nodeStateClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "completed" || s === "succeeded") return "st-completed";
  if (["failed", "timeout", "denied"].includes(s)) return "st-failed";
  if (["retrying", "waiting_for_approval", "action_requires_approval"].includes(s))
    return "st-warning";
  if (["running", "executing", "planning", "queued"].includes(s)) return "st-running";
  return "st-pending";
}

/** Timeline dot class (reuses the shared .timeline-item variants). */
function timelineClass(status: string): string {
  const s = status.toLowerCase();
  if (["completed", "succeeded"].includes(s)) return "ok";
  if (["running", "executing", "planning", "queued"].includes(s)) return "run";
  if (
    ["retrying", "waiting_for_approval", "action_requires_approval", "pending"].includes(s)
  )
    return "warn";
  if (["failed", "timeout", "denied"].includes(s)) return "err";
  return "";
}

/** Edge colour/behaviour keyed off the downstream task's status. */
function edgeClassForStatus(status: string): string {
  const s = status.toLowerCase();
  if (["failed", "timeout", "denied"].includes(s)) return "edge-failed";
  if (["running", "executing", "planning", "queued"].includes(s)) return "edge-active";
  if (["retrying", "waiting_for_approval", "action_requires_approval"].includes(s))
    return "edge-waiting";
  if (["completed", "succeeded"].includes(s)) return "edge-completed";
  return "edge-pending";
}

interface EdgeSpec {
  from: string;
  to: string;
}

interface ToolInfo {
  name: string;
  risk_level: string;
  read_only: boolean;
  external_side_effect: boolean;
  requires_approval: boolean;
  timeout_seconds: number;
}

interface McpToolInfo {
  server_id: string;
  risk_level: string;
  read_only: boolean;
}

const FAILED_STATES = ["failed", "timeout", "denied"];
const APPROVAL_STATES = ["waiting_for_approval", "action_requires_approval"];

/**
 * Dependency edges drawn as an SVG overlay on top of the wave layout.
 * Positions are measured from the rendered DOM so the diagram stays correct
 * across any responsive layout — no graph library required.
 */
function GraphEdges({
  edges,
  tasks,
  containerRef,
}: {
  edges: EdgeSpec[];
  tasks: WorkflowTask[];
  containerRef: React.RefObject<HTMLDivElement>;
}) {
  const [paths, setPaths] = useState<{ key: string; d: string; cls: string }[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container || edges.length === 0) {
      setPaths([]);
      return;
    }
    const statusById = new Map(tasks.map((t) => [t.task_id, t.status]));
    const recompute = () => {
      const cRect = container.getBoundingClientRect();
      setSize({ w: cRect.width, h: cRect.height });
      const next: { key: string; d: string; cls: string }[] = [];
      for (const e of edges) {
        const s = container.querySelector<HTMLElement>(`[data-task-id="${e.from}"]`);
        const t = container.querySelector<HTMLElement>(`[data-task-id="${e.to}"]`);
        if (!s || !t) continue;
        const sr = s.getBoundingClientRect();
        const tr = t.getBoundingClientRect();
        const sx = sr.left - cRect.left + sr.width / 2;
        const sy = sr.top - cRect.top + sr.height;
        const tx = tr.left - cRect.left + tr.width / 2;
        const ty = tr.top - cRect.top;
        const dy = Math.max(16, (ty - sy) / 2);
        next.push({
          key: `${e.from}->${e.to}`,
          cls: edgeClassForStatus(statusById.get(e.to) ?? ""),
          d: `M ${sx.toFixed(1)} ${sy.toFixed(1)} C ${sx.toFixed(1)} ${(sy + dy).toFixed(
            1
          )}, ${tx.toFixed(1)} ${(ty - dy).toFixed(1)}, ${tx.toFixed(1)} ${ty.toFixed(1)}`,
        });
      }
      setPaths(next);
    };
    recompute();
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(recompute);
      ro.observe(container);
    }
    window.addEventListener("resize", recompute);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", recompute);
    };
  }, [edges, tasks, containerRef]);

  if (size.w === 0 || size.h === 0 || paths.length === 0) return null;

  return (
    <svg
      className="task-edges"
      width={size.w}
      height={size.h}
      viewBox={`0 0 ${size.w} ${size.h}`}
      aria-hidden="true"
    >
      {paths.map((p) => (
        <path key={p.key} className={`task-edge ${p.cls}`} d={p.d} />
      ))}
    </svg>
  );
}

/** Renders the primitive retrieval metadata of a single evidence record. */
function EvidenceItem({ item, index }: { item: Record<string, unknown>; index: number }) {
  const source = String(item.source ?? item.document_id ?? item.chunk_id ?? `source ${index + 1}`);
  const entries = Object.entries(item).filter(
    ([key, v]) =>
      !["source", "document_id", "chunk_id"].includes(key) &&
      v != null &&
      (typeof v === "string" || typeof v === "number" || typeof v === "boolean")
  );
  return (
    <div className="evidence-item">
      <div className="src">{source}</div>
      <div className="evidence-meta">
        {entries.slice(0, 6).map(([key, v]) => (
          <span key={key} className="tool-chip">
            {key}: {typeof v === "string" && v.length > 80 ? `${v.slice(0, 80)}…` : String(v)}
          </span>
        ))}
      </div>
    </div>
  );
}

const TERMINAL = ["completed", "failed", "cancelled"];

/**
 * Request states where the server still has work in flight. A request that
 * is still `created` (never submitted for execution) is deliberately absent:
 * there is nothing on the backend that will change on its own, so polling it
 * would be a permanent request storm against a static resource.
 */
const ACTIVE_REQUEST_STATES = [
  "planning",
  "executing",
  "evaluating",
  "queued",
  "running",
  "retrying",
];

export default function RequestDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [wfStatus, setWfStatus] = useState<string>("");
  const [tasks, setTasks] = useState<WorkflowTask[]>([]);
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [evaluation, setEvaluation] = useState<WorkflowEvaluation | null>(null);
  const [result, setResult] = useState<WorkflowResult | null>(null);
  const [requestIntent, setRequestIntent] = useState("");
  const [requestStatus, setRequestStatus] = useState("");
  const [createdAt, setCreatedAt] = useState("");
  const [error, setError] = useState("");
  const [executing, setExecuting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [toolCatalog, setToolCatalog] = useState<Record<string, ToolInfo>>({});
  const [mcpByTool, setMcpByTool] = useState<Record<string, McpToolInfo>>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /** True once this page has successfully submitted the request for execution. */
  const submittedRef = useRef(false);
  const graphRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.push("/login");
  }, [isAuthenticated, isLoading, router]);

  const loadAll = useCallback(
    async (requestId: string, tok: string) => {
      try {
        const req = await apiClient.getRequest(requestId, tok);
        setRequestIntent(req.intent);
        setRequestStatus(req.status);
        setCreatedAt(req.created_at ?? "");

        const lookup = await apiClient
          .getWorkflowByRequest(requestId, tok)
          .catch(() => null);
        if (!lookup) {
          setWorkflowId(null);
          return;
        }
        setWorkflowId(lookup.workflow_id);

        const [taskData, graphData, evalData, resultData] = await Promise.all([
          apiClient.getWorkflowTasks(lookup.workflow_id, tok),
          apiClient.getWorkflowGraph(lookup.workflow_id, tok).catch(() => null),
          apiClient
            .getWorkflowEvaluation(lookup.workflow_id, tok)
            .catch(() => null),
          apiClient
            .getWorkflowResult(lookup.workflow_id, tok)
            .catch(() => null),
        ]);
        setWfStatus(taskData.status);
        setTasks(taskData.tasks);
        setGraph(graphData);
        setEvaluation(evalData?.evaluation ?? null);
        setResult(resultData);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load request");
      } finally {
        setLoading(false);
      }
    },
    []
  );

  // Poll while the workflow is still in flight; stop at terminal states.
  useEffect(() => {
    if (!token || !id) return;
    loadAll(id, token);

    const inFlight = (status: string) =>
      !["completed", "failed", "cancelled", ""].includes(status) &&
      status !== "unknown";

    // Only arm the poller while the backend can still change something:
    // either a workflow row exists (it may still be executing / awaiting an
    // approval), this page just submitted the request, or the request itself
    // is in an active state. A request that was never executed has no
    // workflow and stays `created` forever — polling it would be pointless.
    const pollable =
      workflowId !== null ||
      submittedRef.current ||
      ACTIVE_REQUEST_STATES.includes(requestStatus);

    if (!pollable) return;

    pollRef.current = setInterval(() => {
      const terminal =
        TERMINAL.includes(requestStatus) &&
        (workflowId === null || TERMINAL.includes(wfStatus));
      if (terminal) {
        if (pollRef.current) clearInterval(pollRef.current);
        return;
      }
      if (inFlight(wfStatus) || inFlight(requestStatus) || !workflowId) {
        loadAll(id, token!);
      }
    }, 2500);

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [token, id, loadAll, requestStatus, wfStatus, workflowId]);

  // Enrich selected-task tool usage with the real catalog (risk, timeout,
  // read-only, approval, MCP server). Failures are non-fatal — the workspace
  // still renders the raw tool names if the catalogs are unavailable.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => apiClient.listTools(token))
      .then((r) => {
        if (!cancelled) {
          setToolCatalog(
            Object.fromEntries((r?.tools ?? []).map((t) => [t.name, t]))
          );
        }
      })
      .catch(() => {});
    Promise.resolve()
      .then(() => apiClient.listMCPTools(token))
      .then((r) => {
        if (!cancelled) {
          setMcpByTool(
            Object.fromEntries(
              (r?.tools ?? []).map((t) => [
                t.name,
                { server_id: t.server_id, risk_level: t.risk_level, read_only: t.read_only },
              ])
            )
          );
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleExecute = async () => {
    if (!token || !id) return;
    setExecuting(true);
    setError("");
    try {
      // Prefer the distributed queue path; fall back to sync execution.
      await apiClient
        .executeRequestAsync(id, token)
        .catch(() => apiClient.executeRequest(id, token));
      // Execution was submitted — keep polling even if the workflow lookup
      // has not resolved yet.
      submittedRef.current = true;
      setRequestStatus("executing");
      setLoading(true);
      await loadAll(id, token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Execution failed");
    } finally {
      setExecuting(false);
    }
  };

  // Dependency-wave layout (same algorithm the backend planner uses).
  const waves = useMemo(() => {
    const waveOf = (t: WorkflowTask, map: Map<string, WorkflowTask>): number => {
      if (t.dependencies.length === 0) return 0;
      return (
        1 +
        Math.max(
          ...t.dependencies.map((d) => {
            const dep = map.get(d);
            return dep ? waveOf(dep, map) : -1;
          })
        )
      );
    };
    const taskMap = new Map(tasks.map((t) => [t.task_id, t]));
    const out: WorkflowTask[][] = [];
    for (const t of tasks) {
      const w = Math.max(0, waveOf(t, taskMap));
      while (out.length <= w) out.push([]);
      out[w].push(t);
    }
    return out;
  }, [tasks]);

  const waveIndexById = useMemo(() => {
    const m = new Map<string, number>();
    waves.forEach((wave, i) => wave.forEach((t) => m.set(t.task_id, i)));
    return m;
  }, [waves]);

  const failedTasks = useMemo(
    () => tasks.filter((t) => FAILED_STATES.includes(t.status.toLowerCase())),
    [tasks]
  );
  // Computed before the memo below so the approval banner can react to the
  // run reaching a terminal state.
  const isTerminal =
    TERMINAL.includes(requestStatus) &&
    (workflowId === null || TERMINAL.includes(wfStatus));
  const approvalTasks = useMemo(() => {
    // A task that is actively gated right now.
    const gated = tasks.filter((t) =>
      APPROVAL_STATES.includes(t.status.toLowerCase())
    );
    if (gated.length > 0) return gated;
    // `approval_required` is a static property of the task: it means "this
    // task needs a human when it runs", not "one is pending". Once the run
    // is terminal the decision has been made, so the banner must clear
    // instead of claiming approval is still required.
    if (isTerminal) return [];
    return tasks.filter(
      (t) => t.approval_required && !TERMINAL.includes(t.status.toLowerCase())
    );
  }, [tasks, isTerminal]);
  const retrying = useMemo(
    () => tasks.some((t) => t.status.toLowerCase() === "retrying"),
    [tasks]
  );

  const timeline = useMemo(() => {
    const steps: { key: string; label: string; sub: string; cls: string; time: string }[] = [
      {
        key: "request",
        label: "REQUEST CREATED",
        sub: taskStatus(requestStatus || "created").label,
        cls: timelineClass(requestStatus || "created"),
        time: createdAt ? formatRelativeTime(createdAt) : "",
      },
    ];
    const ordered = [...tasks].sort(
      (a, b) =>
        (waveIndexById.get(a.task_id) ?? 0) - (waveIndexById.get(b.task_id) ?? 0)
    );
    for (const t of ordered) {
      steps.push({
        key: t.task_id,
        label: `${t.agent_type.toUpperCase()} · ${taskStatus(t.status).label}`,
        sub:
          t.description.length > 70 ? `${t.description.slice(0, 70)}…` : t.description,
        cls: timelineClass(t.status),
        time: t.duration_ms != null ? formatDuration(t.duration_ms) : "",
      });
    }
    steps.push({
      key: "evaluation",
      label: "EVALUATION",
      sub:
        typeof evaluation?.overall_score === "number"
          ? `overall ${evaluation.overall_score.toFixed(2)}`
          : "pending",
      cls: evaluation && Object.keys(evaluation).length > 0 ? "ok" : "warn",
      time: "",
    });
    steps.push({
      key: "result",
      label: "RESULT",
      sub: result?.answer
        ? `${result.citations.length} citation${result.citations.length === 1 ? "" : "s"}`
        : "pending",
      cls: result?.answer ? "ok" : "warn",
      time: "",
    });
    return steps;
  }, [tasks, waveIndexById, requestStatus, createdAt, evaluation, result]);

  if (isLoading || !isAuthenticated) return null;

  const selected = selectedTask
    ? tasks.find((t) => t.task_id === selectedTask) ?? null
    : null;

  const hasEval = evaluation && Object.keys(evaluation).length > 0;

  return (
    <AppShell
      title="Execution Workspace"
      wide
      actions={
        <>
          {statusBadge(requestStatus || "created")}
          {["created", "failed"].includes(requestStatus) && (
            <button className="primary" onClick={handleExecute} disabled={executing}>
              {executing ? "Submitting…" : "Execute"}
            </button>
          )}
          {!isTerminal && workflowId && (
            <span className="live-indicator">
              <span className="status-dot ok pulse" /> live · 2.5s
            </span>
          )}
        </>
      }
    >
      {loading ? (
        <LoadingLine label="Loading execution…" />
      ) : !requestIntent ? (
        <EmptyState glyph="⚠" title="Request not found">
          This request does not exist or belongs to another account.
        </EmptyState>
      ) : (
        <>
          {/* Header */}
          <div className="exec-header" style={{ marginBottom: 12, marginTop: -18 }}>
            <div style={{ minWidth: 0 }}>
              <p style={{ fontSize: "var(--text-body)", color: "var(--fg)", margin: 0 }}>
                {requestIntent}
              </p>
              <p className="exec-ids">
                request {id}
                {workflowId ? ` · workflow ${workflowId}` : ""}
                {createdAt ? ` · ${formatRelativeTime(createdAt)}` : ""}
              </p>
            </div>
          </div>

          {error && (
            <div className="error" role="alert" style={{ marginBottom: 16 }}>
              {error}
            </div>
          )}

          {/* Attention: failures / approval gates surfaced at the top level */}
          {failedTasks.length > 0 && (
            <div className="attention-banner danger" role="alert">
              <div className="ab-head">
                <span className="status-dot err" />
                <strong>WORKFLOW FAILED</strong>
                <span className="t-caption mono">
                  {failedTasks.length} task{failedTasks.length === 1 ? "" : "s"} affected
                </span>
              </div>
              <ul className="ab-list">
                {failedTasks.map((t) => (
                  <li key={t.task_id}>
                    <button
                      type="button"
                      className="ab-task"
                      onClick={() => setSelectedTask(t.task_id)}
                    >
                      <span className="mono">{t.agent_type}</span>
                      <span className="ab-desc">{t.description.slice(0, 90)}</span>
                      {t.errors[0] && (
                        <span className="ab-err">{t.errors[0].slice(0, 160)}</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
              <div className="ab-foot">
                <span className="t-caption mono">
                  recovery: {retrying ? "retrying" : "no automatic retry pending"} · workflow{" "}
                  {wfStatus || requestStatus}
                </span>
                {["created", "failed"].includes(requestStatus) && (
                  <button
                    className="secondary compact"
                    onClick={handleExecute}
                    disabled={executing}
                  >
                    {executing ? "Retrying…" : "Retry execution"}
                  </button>
                )}
              </div>
            </div>
          )}

          {failedTasks.length === 0 && approvalTasks.length > 0 && (
            <div className="attention-banner warning" role="status">
              <div className="ab-head">
                <span className="status-dot warn" />
                <strong>HUMAN APPROVAL REQUIRED</strong>
                <span className="t-caption mono">
                  {approvalTasks.length} task{approvalTasks.length === 1 ? "" : "s"} gated
                </span>
              </div>
              <ul className="ab-list">
                {approvalTasks.map((t) => (
                  <li key={t.task_id}>
                    <button
                      type="button"
                      className="ab-task"
                      onClick={() => setSelectedTask(t.task_id)}
                    >
                      <span className="mono">{t.agent_type}</span>
                      <span className="ab-desc">{t.description.slice(0, 90)}</span>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="ab-foot">
                <Link href="/approvals" className="t-caption text-accent">
                  Review approval queue →
                </Link>
              </div>
            </div>
          )}

          {/* Task graph */}
          {waves.length > 0 && (
            <div className="card graph-card">
              <div className="card-header">
                <h2>Task Graph</h2>
                <span className="t-caption mono">
                  {tasks.length} tasks · {waves.length} wave{waves.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="task-graph" ref={graphRef}>
                {graph && graph.edges.length > 0 && (
                  <GraphEdges edges={graph.edges} tasks={tasks} containerRef={graphRef} />
                )}
                <div className="task-waves">
                  {waves.map((wave, wi) => (
                  <div key={wi}>
                    <div className="task-wave-label">
                      Wave {wi + 1}
                      {wi === 0 ? " · parallel entry" : ""}
                    </div>
                    <div className="task-wave">
                      {wave.map((t) => (
                        <button
                          key={t.task_id}
                          data-task-id={t.task_id}
                          type="button"
                          className={`task-node ${nodeStateClass(t.status)} ${
                            selectedTask === t.task_id ? "selected" : ""
                          }`}
                          onClick={() =>
                            setSelectedTask(selectedTask === t.task_id ? null : t.task_id)
                          }
                          aria-pressed={selectedTask === t.task_id}
                        >
                          <div className="task-node-head">
                            <span className="task-node-agent">
                              <span className="agent-ico" aria-hidden="true">
                                {agentGlyph(t.agent_type)}
                              </span>
                              {t.agent_type}
                            </span>
                            {statusBadge(t.status)}
                          </div>
                          <p className="task-node-desc">
                            {t.description.length > 110
                              ? `${t.description.slice(0, 110)}…`
                              : t.description}
                          </p>
                          <div className="task-node-meta">
                            <span>⏱ {formatDuration(t.duration_ms)}</span>
                            {t.retries > 0 && <span className="hot">↻ {t.retries}</span>}
                            {t.tools_used.length > 0 && <span>🛠 {t.tools_used.length}</span>}
                            {t.evidence_count > 0 && <span>▤ {t.evidence_count}</span>}
                            {t.approval_required && (
                              <span className="hot">⚑ approval</span>
                            )}
                          </div>
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
                </div>
                <div className="task-graph-legend">
                  <span className="t-caption mono">dependency flow</span>
                  <span className="edge-legend">
                    <i className="edge-completed" /> completed
                  </span>
                  <span className="edge-legend">
                    <i className="edge-active" /> running
                  </span>
                  <span className="edge-legend">
                    <i className="edge-waiting" /> waiting
                  </span>
                  <span className="edge-legend">
                    <i className="edge-failed" /> failed
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* Execution timeline */}
          {tasks.length > 0 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="card-header">
                <h2>Execution Timeline</h2>
                <span className="t-caption mono">
                  {tasks.length} task{tasks.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="timeline">
                {timeline.map((s) => (
                  <div key={s.key} className={`timeline-item ${s.cls}`}>
                    <div className="t">{s.time}</div>
                    <div className="m">
                      <strong>{s.label}</strong>
                      {s.sub ? ` — ${s.sub}` : ""}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Selected task detail — split layout */}
          {selected && (
            <div className="exec-split" style={{ marginBottom: 16 }}>
              <div className="card detail-card">
                <div className="card-header">
                  <h2>{selected.description.slice(0, 60)}</h2>
                  {statusBadge(selected.status)}
                </div>
                <dl className="kv">
                  <dt>Agent</dt>
                  <dd className="mono">{selected.agent_type || "—"}</dd>
                  <dt>Duration</dt>
                  <dd className="mono">{formatDuration(selected.duration_ms)}</dd>
                  <dt>Retries</dt>
                  <dd className="mono">{selected.retries}</dd>
                  <dt>Tools</dt>
                  <dd>
                    {selected.tools_used.length > 0 ? (
                      <table className="tool-activity">
                        <thead>
                          <tr>
                            <th>Tool</th>
                            <th>Access</th>
                            <th>Risk</th>
                            <th>Timeout</th>
                            <th>Approval</th>
                            <th>MCP server</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selected.tools_used.map((name) => {
                            const info = toolCatalog[name];
                            const mcp = mcpByTool[name];
                            return (
                              <tr key={name}>
                                <td className="mono">{name}</td>
                                <td>
                                  {info ? (info.read_only ? "read-only" : "write") : "—"}
                                </td>
                                <td>
                                  {info ? (
                                    <span className={riskBadgeClass(info.risk_level)}>
                                      {info.risk_level}
                                    </span>
                                  ) : (
                                    "—"
                                  )}
                                </td>
                                <td className="mono">
                                  {info ? `${info.timeout_seconds}s` : "—"}
                                </td>
                                <td>
                                  {info?.requires_approval ? (
                                    <span className="badge warning">required</span>
                                  ) : info ? (
                                    <span className="badge muted">no</span>
                                  ) : (
                                    "—"
                                  )}
                                </td>
                                <td className="mono">
                                  {mcp ? mcp.server_id : info ? "platform" : "unregistered"}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    ) : (
                      "—"
                    )}
                  </dd>
                  <dt>Evidence</dt>
                  <dd className="mono">{selected.evidence_count} sources</dd>
                  <dt>Depends on</dt>
                  <dd className="mono">
                    {selected.dependencies.length > 0
                      ? `${selected.dependencies.length} upstream task(s)`
                      : "none (entry task)"}
                  </dd>
                </dl>
                {selected.summary && (
                  <>
                    <hr className="divider" />
                    <p className="t-small" style={{ lineHeight: 1.65 }}>
                      {selected.summary}
                    </p>
                  </>
                )}
                {selected.errors.length > 0 && (
                  <>
                    <hr className="divider" />
                    {selected.errors.map((e, i) => (
                      <p key={i} className="t-caption text-error" style={{ marginBottom: 4 }}>
                        {e.slice(0, 200)}
                      </p>
                    ))}
                  </>
                )}
              </div>
              <div className="card detail-card">
                <div className="card-header">
                  <h2>Execution Metadata</h2>
                </div>
                <dl className="kv">
                  <dt>Task ID</dt>
                  <dd className="mono">{selected.task_id}</dd>
                  <dt>Workflow</dt>
                  <dd className="mono">{workflowId ?? "—"}</dd>
                  <dt>Approval</dt>
                  <dd>
                    {selected.approval_required ? (
                      <span className="badge warning">gate active</span>
                    ) : (
                      <span className="badge muted">not required</span>
                    )}
                  </dd>
                </dl>
                <p className="t-caption" style={{ marginTop: 12 }}>
                  Only execution metadata is shown — never the model&apos;s
                  private reasoning.
                </p>
              </div>
            </div>
          )}

          {/* Evaluation */}
          {hasEval && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="card-header">
                <h2>Workflow Evaluation</h2>
                <span className="t-caption mono">
                  overall{" "}
                  {typeof evaluation!.overall_score === "number"
                    ? evaluation!.overall_score.toFixed(2)
                    : "—"}
                </span>
              </div>
              <p className="t-caption" style={{ marginTop: 4, marginBottom: 10 }}>
                Workflow-level quality (planning / collaboration / final response), measured from the
                run records — not the per-task verdict that drives retries.
              </p>
              <div className="eval-grid">
                {[
                  {
                    k: "Planning",
                    // The API reports planning quality as ``overall_score``;
                    // ``score`` is kept as a fallback for older payloads.
                    v:
                      evaluation!.planning?.overall_score ??
                      evaluation!.planning?.score,
                    hint: `${evaluation!.planning?.task_count ?? tasks.length} tasks · ${
                      evaluation!.planning?.parallel_waves ?? waves.length
                    } waves`,
                  },
                  {
                    k: "Collaboration",
                    v: evaluation!.collaboration?.score,
                    hint:
                      evaluation!.collaboration?.information_passed_ratio != null
                        ? `${Math.round(
                            evaluation!.collaboration.information_passed_ratio * 100
                          )}% info passed`
                        : "—",
                  },
                  {
                    k: "Final Response",
                    v: evaluation!.final_response?.score,
                    hint: evaluation!.final_response?.produced
                      ? `${evaluation!.final_response.grounded ? "grounded" : "produced"} · ${
                          evaluation!.final_response.citation_count ?? 0
                        } citations`
                      : "pending",
                  },
                ].map((s) => (
                  <div key={s.k} className="eval-score">
                    <div className="k">{s.k}</div>
                    <div className="v">
                      {typeof s.v === "number" ? s.v.toFixed(2) : "—"}
                    </div>
                    {typeof s.v === "number" && (
                      <div className="score-bar" aria-hidden="true">
                        <i style={{ width: `${Math.round(s.v * 100)}%` }} />
                      </div>
                    )}
                    <div className="t-caption">{s.hint}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Final result */}
          {result && result.answer && (
            <div className="card result-panel" style={{ marginBottom: 16 }}>
              <div className="card-header">
                <h2>Final Result</h2>
                <span className="t-caption mono">
                  {result.agent_type || "synthesis"} agent
                  {typeof result.confidence === "number" &&
                    ` · confidence ${Math.round(result.confidence * 100)}%`}
                </span>
              </div>
              <div className="result-answer">{result.answer}</div>

              {result.citations.length > 0 && (
                <>
                  <h3
                    className="t-caps"
                    style={{ margin: "16px 0 6px", color: "var(--fg-secondary)" }}
                  >
                    Citations
                  </h3>
                  <div>
                    {result.citations.map((c, i) => (
                      <div key={i} className="citation-row">
                        <span className="src">
                          {String(c.source ?? c.document_id ?? c.chunk_id ?? `#${i + 1}`)}
                        </span>
                        {typeof c.quality_score === "number" && (
                          <span>quality {Math.round(c.quality_score * 100)}%</span>
                        )}
                        {typeof c.score === "number" && (
                          <span>relevance {c.score.toFixed(2)}</span>
                        )}
                      </div>
                    ))}
                  </div>
                </>
              )}

              {result.evidence.length > 0 && (
                <details className="evidence-details">
                  <summary>
                    Evidence · {result.evidence.length} retrieved source
                    {result.evidence.length === 1 ? "" : "s"}
                  </summary>
                  <div className="evidence-list">
                    {result.evidence.map((e, i) => (
                      <EvidenceItem key={i} item={e} index={i} />
                    ))}
                  </div>
                </details>
              )}

              {result.tools_used.length > 0 && (
                <div className="evidence-chips" style={{ marginTop: 14 }}>
                  {result.tools_used.map((t) => (
                    <span key={t} className="tool-chip">
                      {t}
                    </span>
                  ))}
                </div>
              )}

              {result.failed_upstream.length > 0 && (
                <p className="t-caption text-warning" style={{ marginTop: 10 }}>
                  ⚠ {result.failed_upstream.length} upstream task(s) failed; result
                  is partial.
                </p>
              )}

              {result.errors.length > 0 && (
                <p className="t-caption text-error" style={{ marginTop: 6 }}>
                  {result.errors[0].slice(0, 200)}
                </p>
              )}
            </div>
          )}

          {/* Empty state before execution */}
          {!workflowId && !error && (
            <div className="card">
              <EmptyState glyph="◇" title="Not executed yet">
                <p>
                  Press <strong>Execute</strong> to submit this request to the
                  multi-agent pipeline.
                </p>
              </EmptyState>
            </div>
          )}

        </>
      )}
    </AppShell>
  );
}
