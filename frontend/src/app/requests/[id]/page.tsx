"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import type {
  WorkflowGraph,
  WorkflowTask,
  WorkflowEvaluation,
  WorkflowResult,
} from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  completed: "badge success",
  running: "badge info",
  executing: "badge info",
  planning: "badge info",
  pending: "badge muted",
  queued: "badge muted",
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

function agentIcon(agentType: string): string {
  switch (agentType) {
    case "planner":
      return "◈";
    case "research":
      return "🔎";
    case "rag":
      return "📚";
    case "analysis":
      return "📊";
    case "synthesis":
      return "✦";
    case "evaluation":
      return "✓";
    default:
      return "•";
  }
}

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
  const [error, setError] = useState("");
  const [executing, setExecuting] = useState(false);
  const [loading, setLoading] = useState(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.push("/login");
  }, [isAuthenticated, isLoading, router]);

  const loadAll = useCallback(
    async (requestId: string, tok: string) => {
      try {
        const req = await apiClient.getRequest(requestId, tok);
        setRequestIntent(req.intent);
        setRequestStatus(req.status);

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

    pollRef.current = setInterval(() => {
      const terminal =
        ["completed", "failed", "cancelled"].includes(requestStatus) &&
        ["completed", "failed", "cancelled"].includes(wfStatus);
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

  const handleExecute = async () => {
    if (!token || !id) return;
    setExecuting(true);
    setError("");
    try {
      // Prefer the distributed queue path; fall back to sync execution.
      await apiClient
        .executeRequestAsync(id, token)
        .catch(() => apiClient.executeRequest(id, token));
      setRequestStatus("executing");
      setLoading(true);
      await loadAll(id, token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Execution failed");
    } finally {
      setExecuting(false);
    }
  };

  if (isLoading || !isAuthenticated) return null;

  const isTerminal =
    ["completed", "failed", "cancelled"].includes(requestStatus) &&
    (workflowId === null || ["completed", "failed", "cancelled"].includes(wfStatus));

  // Simple layered task-graph layout: waves by dependency depth.
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
  const waves: WorkflowTask[][] = [];
  for (const t of tasks) {
    const w = Math.max(0, waveOf(t, taskMap));
    while (waves.length <= w) waves.push([]);
    waves[w].push(t);
  }

  const stopPollingManually = () => {
    if (pollRef.current) clearInterval(pollRef.current);
  };

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <button
          className="secondary"
          onClick={() => router.push("/dashboard")}
          style={{ marginBottom: 16 }}
        >
          ← Dashboard
        </button>

        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading request…</p>
        ) : !requestIntent ? (
          <p style={{ color: "var(--danger)" }}>Request not found</p>
        ) : (
          <>
            {/* Header */}
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                marginBottom: 20,
                gap: 16,
                flexWrap: "wrap",
              }}
            >
              <div>
                <h1 style={{ fontSize: 22, fontWeight: 700 }}>Execution Workspace</h1>
                <p
                  style={{
                    fontFamily: "monospace",
                    fontSize: 12,
                    color: "var(--muted)",
                  }}
                >
                  request {id}
                  {workflowId ? ` · workflow ${workflowId}` : ""}
                </p>
              </div>
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                {statusBadge(requestStatus || "created")}
                {["created", "failed"].includes(requestStatus) && (
                  <button
                    className="primary"
                    onClick={handleExecute}
                    disabled={executing}
                  >
                    {executing ? "Submitting…" : "Execute"}
                  </button>
                )}
                {!isTerminal && workflowId && (
                  <span style={{ fontSize: 12, color: "var(--muted)" }}>
                    ⟳ live (2.5s)
                    <button
                      className="secondary"
                      onClick={stopPollingManually}
                      style={{ marginLeft: 8, padding: "2px 8px", fontSize: 11 }}
                    >
                      stop
                    </button>
                  </span>
                )}
              </div>
            </div>

            {error && (
              <div
                className="error"
                style={{
                  marginBottom: 16,
                  padding: 12,
                  background: "#f8d7da",
                  borderRadius: 6,
                }}
              >
                {error}
              </div>
            )}

            {/* Intent */}
            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ fontSize: 14, marginBottom: 8 }}>Request</h2>
              <p style={{ fontSize: 14 }}>{requestIntent}</p>
            </div>

            {/* Pipeline overview */}
            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ fontSize: 14, marginBottom: 12 }}>Pipeline</h2>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  flexWrap: "wrap",
                  fontSize: 12,
                }}
              >
                {["PLANNED", "TASK GRAPH", "AGENTS", "TOOLS", "EVALUATION", "RESULT"].map(
                  (stage, i) => {
                    const reached =
                      (i === 0 && tasks.length > 0) ||
                      (i === 1 && tasks.some((t) => t.dependencies.length > 0)) ||
                      (i === 2 && tasks.some((t) => t.status !== "pending")) ||
                      (i === 3 && tasks.some((t) => t.tools_used.length > 0)) ||
                      (i === 4 && evaluation !== null && Object.keys(evaluation).length > 0) ||
                      (i === 5 && result?.answer);
                    return (
                      <span key={stage} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        {i > 0 && <span style={{ color: "var(--muted)" }}>→</span>}
                        <span
                          className={`badge ${reached ? "success" : "muted"}`}
                          style={{ letterSpacing: "0.5px" }}
                        >
                          {stage}
                        </span>
                      </span>
                    );
                  }
                )}
              </div>
            </div>

            {/* Task graph (waves by dependency depth) */}
            {waves.length > 0 && (
              <div className="card" style={{ marginBottom: 16 }}>
                <h2 style={{ fontSize: 14, marginBottom: 12 }}>Task Graph</h2>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  {waves.map((wave, wi) => (
                    <div key={wi}>
                      <div
                        style={{
                          fontSize: 11,
                          color: "var(--muted)",
                          marginBottom: 6,
                          textTransform: "uppercase",
                          letterSpacing: "0.5px",
                        }}
                      >
                        {wi === 0 ? "Wave 1 (parallel entry)" : `Wave ${wi + 1}`}
                      </div>
                      <div
                        style={{
                          display: "flex",
                          gap: 12,
                          flexWrap: "wrap",
                        }}
                      >
                        {wave.map((t) => (
                          <div
                            key={t.task_id}
                            className="card"
                            style={{
                              margin: 0,
                              padding: 12,
                              minWidth: 240,
                              flex: "1 1 240px",
                              borderLeft:
                                t.status === "completed"
                                  ? "3px solid var(--success)"
                                  : t.status === "pending"
                                  ? "3px solid var(--border)"
                                  : ["failed", "timeout", "denied"].includes(t.status)
                                  ? "3px solid var(--danger)"
                                  : "3px solid var(--primary)",
                            }}
                          >
                            <div
                              style={{
                                display: "flex",
                                justifyContent: "space-between",
                                alignItems: "center",
                                marginBottom: 6,
                              }}
                            >
                              <strong style={{ fontSize: 13 }}>
                                {agentIcon(t.agent_type)} {t.agent_type}
                              </strong>
                              {statusBadge(t.status)}
                            </div>
                            <p
                              style={{
                                fontSize: 12,
                                color: "var(--muted)",
                                marginBottom: 8,
                                minHeight: 32,
                              }}
                            >
                              {t.description.length > 110
                                ? `${t.description.slice(0, 110)}…`
                                : t.description}
                            </p>
                            <div
                              style={{
                                display: "flex",
                                gap: 10,
                                fontSize: 11,
                                color: "var(--muted)",
                                flexWrap: "wrap",
                              }}
                            >
                              <span>⏱ {t.duration_ms != null ? `${t.duration_ms} ms` : "—"}</span>
                              <span>↻ {t.retries} retries</span>
                              {t.tools_used.length > 0 && (
                                <span>🛠 {t.tools_used.join(", ")}</span>
                              )}
                              {t.evidence_count > 0 && <span>📎 {t.evidence_count} evidence</span>}
                              {t.approval_required && (
                                <span className="badge warning">approval gate</span>
                              )}
                            </div>
                            {t.dependencies.length > 0 && (
                              <p style={{ fontSize: 10, color: "var(--muted)", marginTop: 6 }}>
                                depends on: {t.dependencies.length} task(s)
                              </p>
                            )}
                            {t.errors.length > 0 && (
                              <p style={{ fontSize: 11, color: "var(--danger)", marginTop: 6 }}>
                                {t.errors[0].slice(0, 120)}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                      {wi < waves.length - 1 && (
                        <div style={{ textAlign: "center", color: "var(--muted)", fontSize: 14 }}>
                          ↓
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Evaluation */}
            {evaluation && Object.keys(evaluation).length > 0 && (
              <div className="card" style={{ marginBottom: 16 }}>
                <h2 style={{ fontSize: 14, marginBottom: 12 }}>Evaluation</h2>
                <div className="stat-grid" style={{ marginBottom: 0 }}>
                  <div className="stat-card">
                    <div className="label">Overall Score</div>
                    <div className="value">
                      {typeof evaluation.overall_score === "number"
                        ? evaluation.overall_score.toFixed(2)
                        : "—"}
                    </div>
                  </div>
                  <div className="stat-card">
                    <div className="label">Final Response</div>
                    <div className="value" style={{ fontSize: 18 }}>
                      {evaluation.final_response?.produced
                        ? `${evaluation.final_response.grounded ? "Grounded" : "Produced"}`
                        : "—"}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>
                      {evaluation.final_response?.citation_count ?? 0} citation(s)
                      {evaluation.final_response?.failed_upstream_count
                        ? ` · ${evaluation.final_response.failed_upstream_count} failed upstream`
                        : ""}
                    </div>
                  </div>
                  <div className="stat-card">
                    <div className="label">Collaboration</div>
                    <div className="value">
                      {typeof evaluation.collaboration?.score === "number"
                        ? evaluation.collaboration.score.toFixed(2)
                        : "—"}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>
                      info passed:{" "}
                      {evaluation.collaboration?.information_passed_ratio != null
                        ? `${(evaluation.collaboration.information_passed_ratio * 100).toFixed(0)}%`
                        : "—"}
                    </div>
                  </div>
                  <div className="stat-card">
                    <div className="label">Planning</div>
                    <div className="value">
                      {typeof evaluation.planning?.score === "number"
                        ? evaluation.planning.score.toFixed(2)
                        : "—"}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>
                      {evaluation.planning?.task_count ?? tasks.length} task(s),{" "}
                      {evaluation.planning?.parallel_waves ?? waves.length} wave(s)
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Final result */}
            {result && result.answer && (
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="card-header">
                  <h2 style={{ fontSize: 14 }}>Final Result</h2>
                  <span style={{ fontSize: 11, color: "var(--muted)" }}>
                    by {result.agent_type || "synthesis"} agent
                    {typeof result.confidence === "number" &&
                      ` · confidence ${(result.confidence * 100).toFixed(0)}%`}
                  </span>
                </div>
                <p
                  style={{
                    fontSize: 14,
                    whiteSpace: "pre-wrap",
                    background: "#fff",
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    padding: 16,
                  }}
                >
                  {result.answer}
                </p>

                {result.citations.length > 0 && (
                  <>
                    <h3 style={{ fontSize: 12, margin: "14px 0 6px" }}>Citations</h3>
                    <ul style={{ fontSize: 12, paddingLeft: 20 }}>
                      {result.citations.map((c, i) => (
                        <li key={i} style={{ marginBottom: 4 }}>
                          <span style={{ fontFamily: "monospace" }}>
                            {String(c.source ?? c.document_id ?? c.chunk_id ?? `#${i + 1}`)}
                          </span>
                          {typeof c.quality_score === "number" &&
                            ` (quality ${(c.quality_score * 100).toFixed(0)}%)`}
                          {typeof c.score === "number" &&
                            ` (relevance ${c.score.toFixed(2)})`}
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                {result.tools_used.length > 0 && (
                  <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 10 }}>
                    🛠 Tools used: {result.tools_used.join(", ")}
                  </p>
                )}

                {result.failed_upstream.length > 0 && (
                  <p style={{ fontSize: 12, color: "var(--warning)", marginTop: 6 }}>
                    ⚠ {result.failed_upstream.length} upstream task(s) failed; result is partial.
                  </p>
                )}

                {result.errors.length > 0 && (
                  <p style={{ fontSize: 12, color: "var(--danger)", marginTop: 6 }}>
                    {result.errors[0].slice(0, 200)}
                  </p>
                )}
              </div>
            )}

            {/* Empty state before execution */}
            {!workflowId && !error && (
              <div className="card">
                <div className="empty-state">
                  This request has not been executed yet.
                  <br />
                  Press <strong>Execute</strong> to submit it to the multi-agent pipeline.
                </div>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
