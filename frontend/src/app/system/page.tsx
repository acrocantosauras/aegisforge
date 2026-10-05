"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import type { SystemStatus } from "@/lib/api";

interface AuditEvent {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  outcome: string;
  created_at: string;
}

function ComponentCard({
  name,
  detail,
  state,
  metric,
}: {
  name: string;
  detail: string;
  state: "ok" | "warn" | "err" | "idle";
  metric?: string;
}) {
  const label =
    state === "ok" ? "healthy" : state === "warn" ? "degraded" : state === "err" ? "unavailable" : "—";
  return (
    <div className="stat-card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="label">{name}</div>
        <span
          className={`status-dot ${state}`}
          role="img"
          aria-label={`${name}: ${label}`}
        />
      </div>
      <div
        className="value"
        style={{
          fontSize: "1.1rem",
          color:
            state === "ok"
              ? "var(--success)"
              : state === "warn"
                ? "var(--warning)"
                : state === "err"
                  ? "var(--error)"
                  : "var(--fg-muted)",
        }}
      >
        {label}
      </div>
      <div className="hint">
        {metric ? `${metric} · ` : ""}
        {detail}
      </div>
    </div>
  );
}

export default function SystemPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.push("/login");
  }, [isAuthenticated, isLoading, router]);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      setStatus(await apiClient.systemStatus(token));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load system status");
    } finally {
      setRefreshing(false);
    }
  }, [token]);

  useEffect(() => {
    if (!token) return;
    load();
    apiClient
      .listAuditEvents(token, 12)
      .then((r) => setAudit(r.events ?? []))
      .catch(() => {});
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [token, load]);

  if (isLoading || !isAuthenticated) return null;

  const overall = status?.status ?? "unknown";
  const c = status?.components;

  return (
    <AppShell
      title="System Health"
      subtitle="Real status measured live from the control plane — never fabricated. Auto-refreshes every 5 seconds."
      actions={
        <>
          {status && (
            <span
              className={`system-pill ${
                overall === "healthy" ? "ok" : overall === "degraded" ? "degraded" : "down"
              }`}
            >
              {overall.toUpperCase()}
            </span>
          )}
          <button className="secondary" onClick={load} disabled={refreshing}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </>
      }
    >
      {error && (
        <div className="error" role="alert" style={{ marginBottom: 16 }}>
          {error}
        </div>
      )}

      {!status ? (
        error ? (
          <div className="card">
            <p className="t-small" style={{ marginBottom: 10 }}>
              Live status could not be loaded from the control plane.
            </p>
            <button className="secondary" onClick={load} disabled={refreshing}>
              {refreshing ? "Retrying…" : "Retry"}
            </button>
          </div>
        ) : (
          <LoadingLine label="Probing components…" />
        )
      ) : (
        <>
          <div className="stat-grid">
            <ComponentCard
              name="API"
              detail="FastAPI control plane"
              state={c!.api === "ok" ? "ok" : "err"}
            />
            <ComponentCard
              name="Database"
              detail="PostgreSQL + pgvector"
              state={c!.database === "ok" ? "ok" : "err"}
            />
            <ComponentCard
              name="Redis"
              detail="Job queue + registry"
              state={c!.redis === "ok" ? "ok" : "err"}
            />
            <ComponentCard
              name="Workers"
              detail={
                c!.workers.status === "none_active"
                  ? "jobs will queue"
                  : "heartbeats current"
              }
              state={c!.workers.active > 0 ? "ok" : c!.redis === "ok" ? "warn" : "err"}
              metric={`${c!.workers.healthy}/${c!.workers.active}`}
            />
            <ComponentCard
              name="Queue"
              detail="pending jobs"
              state={c!.queue.depth > 20 ? "warn" : "ok"}
              metric={String(c!.queue.depth)}
            />
          </div>

          {/* Worker registry */}
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-header">
              <h2>Worker Registry</h2>
              <span className="t-caption mono">TTL 120s · heartbeat 30s</span>
            </div>
            {c!.workers.list.length === 0 ? (
              <EmptyState glyph="▢" title="No active workers">
                No workers have heartbeated recently. Jobs will queue until a
                worker connects.
              </EmptyState>
            ) : (
              <div className="table-wrap" style={{ border: "none" }}>
                <table>
                  <thead>
                    <tr>
                      <th>Worker</th>
                      <th>Last Heartbeat</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {c!.workers.list.map((w) => (
                      <tr key={w.worker_id}>
                        <td className="mono">{w.worker_id}</td>
                        <td className="mono">
                          {w.last_heartbeat_age_seconds.toFixed(1)}s ago
                        </td>
                        <td>
                          <span className={`badge ${w.healthy ? "success" : "danger"}`}>
                            {w.healthy ? "healthy" : "stale"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Audit trail */}
          <div className="card">
            <div className="card-header">
              <h2>Audit Trail</h2>
              <span className="t-caption mono">latest {audit.length} events</span>
            </div>
            {audit.length === 0 ? (
              <EmptyState glyph="≡" title="No audit events">
                Security-relevant actions (auth, approvals, document changes)
                will appear here.
              </EmptyState>
            ) : (
              <div style={{ display: "flex", flexDirection: "column" }}>
                {audit.map((e) => (
                  <div
                    key={e.id}
                    className="row wrap"
                    style={{
                      justifyContent: "space-between",
                      padding: "8px 0",
                      borderBottom: "1px solid var(--border-subtle)",
                      gap: 10,
                    }}
                  >
                    <span className="row" style={{ gap: 10, minWidth: 0 }}>
                      <span
                        className={`badge ${
                          e.outcome === "success"
                            ? "success"
                            : e.outcome === "denied" || e.outcome === "failure"
                              ? "danger"
                              : "muted"
                        }`}
                      >
                        {e.outcome}
                      </span>
                      <span className="mono" style={{ fontSize: "var(--text-caption)" }}>
                        {e.action}
                      </span>
                      <span className="t-caption">
                        {e.resource_type}
                        {e.resource_id ? ` · ${e.resource_id.slice(0, 14)}…` : ""}
                      </span>
                    </span>
                    <span className="t-caption mono" style={{ flexShrink: 0 }}>
                      {new Date(e.created_at).toLocaleTimeString()}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </AppShell>
  );
}
