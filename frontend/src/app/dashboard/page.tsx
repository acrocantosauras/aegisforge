"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

interface Request {
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

interface Metrics {
  count: number;
  total_tool_calls: number;
  total_retries: number;
  total_failures: number;
  success_rate: number;
}

export default function DashboardPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [requests, setRequests] = useState<Request[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;

    async function fetchData() {
      try {
        const [reqs, apps, met] = await Promise.all([
          apiClient.listRequests(token!),
          apiClient.listApprovals(token!).catch(() => ({ approvals: [], total: 0 })),
          apiClient.getMetrics(token!).catch(() => ({ count: 0, total_tool_calls: 0, total_retries: 0, total_failures: 0, success_rate: 0 })),
        ]);
        setRequests(Array.isArray(reqs) ? reqs : []);
        setApprovals(apps.approvals || []);
        setMetrics(met);
      } catch {
        // Silent — some endpoints may not exist yet
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [token]);

  if (isLoading || !isAuthenticated) return null;

  const statusBadge = (status: string) => {
    switch (status) {
      case "completed":
        return <span className="badge success">Completed</span>;
      case "failed":
        return <span className="badge danger">Failed</span>;
      case "executing":
      case "planning":
        return <span className="badge info">Running</span>;
      case "waiting_for_approval":
        return <span className="badge warning">Needs Approval</span>;
      default:
        return <span className="badge muted">{status}</span>;
    }
  };

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24 }}>Dashboard</h1>

        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading dashboard...</p>
        ) : (
          <>
            {/* Stats */}
            <div className="stat-grid">
              <div className="stat-card">
                <div className="label">Total Requests</div>
                <div className="value">{metrics?.count ?? requests.length}</div>
              </div>
              <div className="stat-card">
                <div className="label">Tool Calls</div>
                <div className="value">{metrics?.total_tool_calls ?? 0}</div>
              </div>
              <div className="stat-card">
                <div className="label">Success Rate</div>
                <div className="value">
                  {metrics?.success_rate
                    ? `${(metrics.success_rate * 100).toFixed(0)}%`
                    : "—"}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Pending Approvals</div>
                <div className="value">{approvals.length}</div>
              </div>
            </div>

            {/* Recent Requests */}
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="card-header">
                <h2>Recent Requests</h2>
                <Link href="/requests/new">
                  <button className="primary">New Request</button>
                </Link>
              </div>
              {requests.length === 0 ? (
                <div className="empty-state">No requests yet. Create your first request.</div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Intent</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {requests.slice(0, 10).map((req) => (
                      <tr
                        key={req.id}
                        style={{ cursor: "pointer" }}
                        onClick={() => router.push(`/requests/${req.id}`)}
                      >
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>{req.id.slice(0, 16)}</td>
                        <td>{req.intent.slice(0, 80)}</td>
                        <td>{statusBadge(req.status)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* Pending Approvals */}
            {approvals.length > 0 && (
              <div className="card">
                <div className="card-header">
                  <h2>Pending Approvals</h2>
                  <Link href="/approvals">View All</Link>
                </div>
                <table>
                  <thead>
                    <tr>
                      <th>Action</th>
                      <th>Risk Level</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {approvals.slice(0, 5).map((app) => (
                      <tr key={app.approval_id}>
                        <td>{app.action_description.slice(0, 60)}</td>
                        <td>
                          <span
                            className={`badge ${
                              app.risk_level === "high" || app.risk_level === "critical"
                                ? "danger"
                                : "warning"
                            }`}
                          >
                            {app.risk_level}
                          </span>
                        </td>
                        <td>{app.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
