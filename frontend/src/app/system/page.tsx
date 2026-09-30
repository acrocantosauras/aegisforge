"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import type { SystemStatus } from "@/lib/api";

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 10,
        height: 10,
        borderRadius: "50%",
        marginRight: 8,
        background: ok ? "var(--success)" : "var(--danger)",
      }}
    />
  );
}

export default function SystemPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.push("/login");
  }, [isAuthenticated, isLoading, router]);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      setStatus(await apiClient.systemStatus());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load system status");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    if (!token) return;
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [token, load]);

  if (isLoading || !isAuthenticated) return null;

  const overall = status?.status ?? "unknown";

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 20,
          }}
        >
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 700 }}>System Health</h1>
            <p style={{ fontSize: 12, color: "var(--muted)" }}>
              Real status measured live — never fabricated. Refreshes every 5s.
            </p>
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            {status && (
              <span
                className={`badge ${
                  overall === "healthy" ? "success" : overall === "degraded" ? "warning" : "muted"
                }`}
              >
                {overall.toUpperCase()}
              </span>
            )}
            <button className="secondary" onClick={load} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {error && (
          <div
            className="error"
            style={{ marginBottom: 16, padding: 12, background: "#f8d7da", borderRadius: 6 }}
          >
            {error}
          </div>
        )}

        {!status ? (
          <p style={{ color: "var(--muted)" }}>Loading system status…</p>
        ) : (
          <>
            <div className="stat-grid">
              <div className="stat-card">
                <div className="label">API</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <Dot ok={status.components.api === "ok"} />
                  {status.components.api === "ok" ? "Healthy" : "Down"}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Database</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <Dot ok={status.components.database === "ok"} />
                  {status.components.database === "ok" ? "Healthy" : "Unavailable"}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Redis</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <Dot ok={status.components.redis === "ok"} />
                  {status.components.redis === "ok" ? "Healthy" : "Unavailable"}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Workers</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <Dot ok={status.components.workers.active > 0} />
                  {status.components.workers.healthy} active
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Queue Depth</div>
                <div className="value">{status.components.queue.depth}</div>
              </div>
            </div>

            <div className="card">
              <div className="card-header">
                <h2>Worker Registry</h2>
              </div>
              {status.components.workers.list.length === 0 ? (
                <div className="empty-state">
                  No workers have heartbeated recently. Jobs will queue until a
                  worker connects.
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Worker</th>
                      <th>Last Heartbeat</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.components.workers.list.map((w) => (
                      <tr key={w.worker_id}>
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>
                          {w.worker_id}
                        </td>
                        <td>{w.last_heartbeat_age_seconds.toFixed(1)}s ago</td>
                        <td>
                          <span className={`badge ${w.healthy ? "success" : "danger"}`}>
                            {w.healthy ? "healthy" : "stale"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
