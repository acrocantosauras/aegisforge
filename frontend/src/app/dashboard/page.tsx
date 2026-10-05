"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { statusBadgeClass, taskStatus, formatRelativeTime } from "@/lib/status";

interface RequestItem {
  id: string;
  intent: string;
  status: string;
  created_at: string;
}

interface Approval {
  approval_id: string;
  action_description: string;
  risk_level: string;
  status: string;
  created_at: string;
}

interface Agent {
  agent_type: string;
  name: string;
  description: string;
  capabilities: string[];
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

export default function DashboardPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [requests, setRequests] = useState<RequestItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;
    setLoading(true);

    async function fetchData() {
      try {
        const [reqs, apps, ags] = await Promise.all([
          apiClient.listRequests(token!),
          apiClient
            .listApprovals(token!)
            .catch(() => ({ approvals: [], total: 0 })),
          apiClient.listAgents(token!).catch(() => null),
        ]);
        setRequests(Array.isArray(reqs) ? reqs : []);
        setApprovals(apps.approvals || []);
        setAgents(ags?.agents ?? []);
        setLoadError(false);
      } catch {
        setLoadError(true);
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [token, reloadKey]);

  if (isLoading || !isAuthenticated) return null;

  // All statistics on this page are derived from tenant-scoped sources (the
  // caller's own requests + approval queue). The aggregate
  // /evaluation/metrics endpoint is served from the API process's in-process
  // collector — it is empty for worker-executed runs and is not filtered by
  // tenant — so it must not be presented as "your" activity.
  const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];
  const terminalRequests = requests.filter((r) =>
    TERMINAL_STATUSES.includes(r.status)
  );
  const completedRequests = requests.filter((r) => r.status === "completed");
  const inProgress = requests.filter((r) => !TERMINAL_STATUSES.includes(r.status));

  const successRate =
    terminalRequests.length > 0
      ? `${Math.round(
          (completedRequests.length / terminalRequests.length) * 100
        )}%`
      : "—";

  return (
    <AppShell
      title="Overview"
      subtitle="Live state of your requests, agents, and platform activity."
      actions={
        <Link href="/execute">
          <button className="primary">New Execution</button>
        </Link>
      }
    >
      {loading ? (
        <LoadingLine label="Loading workspace…" />
      ) : (
        <>
          {/* Stats */}
          <div className="stat-grid">
            <div className="stat-card">
              <div className="label">Requests</div>
              <div className="value">{requests.length}</div>
              <div className="hint">total submitted</div>
            </div>
            <div className="stat-card">
              <div className="label">Success Rate</div>
              <div className="value">{successRate}</div>
              <div className="hint">completed workflows</div>
            </div>
            <div className="stat-card">
              <div className="label">In Progress</div>
              <div className="value">{inProgress.length}</div>
              <div className="hint">
                {inProgress.length > 0 ? "running or awaiting approval" : "queue clear"}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Pending Approvals</div>
              <div className="value" style={{ color: approvals.length > 0 ? "var(--warning)" : undefined }}>
                {approvals.length}
              </div>
              <div className="hint">
                {approvals.length > 0 ? "action required" : "queue clear"}
              </div>
            </div>
          </div>

          {loadError && (
            <div
              className="error"
              role="alert"
              style={{
                marginBottom: 16,
                display: "flex",
                alignItems: "center",
                gap: 14,
                flexWrap: "wrap",
              }}
            >
              <span>Could not reach the control plane API.</span>
              <button
                className="secondary compact"
                onClick={() => setReloadKey((k) => k + 1)}
              >
                Retry
              </button>
            </div>
          )}

          <div className="exec-split">
            {/* Recent executions */}
            <div>
              <div className="card">
                <div className="card-header">
                  <h2>Recent Executions</h2>
                  <Link href="/history" className="t-caption text-accent">
                    View all →
                  </Link>
                </div>
                {requests.length === 0 ? (
                  <EmptyState glyph="◇" title="No executions yet">
                    <p>
                      Submit your first request to see the planner, agents, and
                      evaluation pipeline in action.
                    </p>
                    <Link href="/execute">
                      <button className="primary" style={{ marginTop: 16 }}>
                        Launch first workflow
                      </button>
                    </Link>
                  </EmptyState>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column" }}>
                    {requests.slice(0, 6).map((req) => (
                      <div
                        key={req.id}
                        className="clickable"
                        role="link"
                        tabIndex={0}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 14,
                          padding: "11px 4px",
                          borderBottom: "1px solid var(--border-subtle)",
                          cursor: "pointer",
                        }}
                        onClick={() => router.push(`/requests/${req.id}`)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") router.push(`/requests/${req.id}`);
                        }}
                      >
                        <span className={statusBadgeClass(req.status)}>
                          {taskStatus(req.status).label}
                        </span>
                        <span
                          className="grow"
                          style={{
                            fontSize: "var(--text-small)",
                            color: "var(--fg)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {req.intent}
                        </span>
                        <span className="t-caption mono" style={{ whiteSpace: "nowrap" }}>
                          {formatRelativeTime(req.created_at)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Pending approvals */}
              {approvals.length > 0 && (
                <div className="card" style={{ marginTop: 16 }}>
                  <div className="card-header">
                    <h2>Awaiting Approval</h2>
                    <Link href="/approvals" className="t-caption text-accent">
                      Review queue →
                    </Link>
                  </div>
                  {approvals.slice(0, 4).map((a) => (
                    <div
                      key={a.approval_id}
                      className="row wrap"
                      style={{
                        justifyContent: "space-between",
                        padding: "10px 0",
                        borderBottom: "1px solid var(--border-subtle)",
                      }}
                    >
                      <span style={{ fontSize: "var(--text-small)" }}>
                        {a.action_description.length > 70
                          ? `${a.action_description.slice(0, 70)}…`
                          : a.action_description}
                      </span>
                      <span
                        className={`badge ${
                          a.risk_level === "high" || a.risk_level === "critical"
                            ? "danger"
                            : "warning"
                        }`}
                      >
                        {a.risk_level} risk
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Agent activity rail */}
            <div className="card">
              <div className="card-header">
                <h2>Agent Fleet</h2>
                <Link href="/agents" className="t-caption text-accent">
                  Registry →
                </Link>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {agents.length === 0 ? (
                  <p className="t-caption">No agents registered.</p>
                ) : (
                  agents.map((a) => (
                    <div key={a.agent_type} className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                      <span
                        className="agent-ico"
                        aria-hidden="true"
                        style={{
                          width: 28,
                          height: 28,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          borderRadius: "var(--radius-sm)",
                          background: "var(--accent-dim)",
                          border: "1px solid var(--accent-border)",
                          color: "var(--accent)",
                          fontSize: 13,
                          flexShrink: 0,
                        }}
                      >
                        {agentGlyph(a.agent_type)}
                      </span>
                      <span style={{ minWidth: 0 }}>
                        <span
                          style={{
                            display: "block",
                            fontFamily: "var(--font-mono)",
                            fontSize: "var(--text-caption)",
                            color: "var(--fg)",
                          }}
                        >
                          {a.name || a.agent_type}
                        </span>
                        <span className="t-caption" style={{ display: "block" }}>
                          {a.description}
                        </span>
                        {a.capabilities?.length > 0 && (
                          <span className="evidence-chips" style={{ marginTop: 6 }}>
                            {a.capabilities.slice(0, 3).map((c) => (
                              <span key={c} className="tool-chip">
                                {c}
                              </span>
                            ))}
                          </span>
                        )}
                      </span>
                    </div>
                  ))
                )}
              </div>
              <p className="t-caption" style={{ marginTop: 14 }}>
                Live from the agent registry. Availability and tool bindings are
                managed by the platform.
              </p>
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}
