"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

interface RequestDetail {
  id: string;
  intent: string;
  status: string;
  context: Record<string, unknown>;
  created_at: string;
}

interface ExecutionResult {
  request_id: string;
  workflow_id: string;
  status: string;
  final_result: Record<string, unknown>;
  errors: string[];
}

export default function RequestDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [request, setRequest] = useState<RequestDetail | null>(null);
  const [execution, setExecution] = useState<ExecutionResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [executing, setExecuting] = useState(false);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token || !id) return;

    async function fetchRequest() {
      try {
        const req = await apiClient.getRequest(id, token!);
        setRequest(req);
      } catch {
        setError("Failed to load request");
      } finally {
        setLoading(false);
      }
    }

    fetchRequest();
  }, [token, id]);

  const [error, setError] = useState("");

  const handleExecute = async () => {
    if (!token || !id) return;
    setExecuting(true);
    setError("");
    try {
      const result = await apiClient.executeRequest(id, token);
      setExecution(result);
      setRequest((prev) => (prev ? { ...prev, status: result.status } : prev));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Execution failed");
    } finally {
      setExecuting(false);
    }
  };

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
        <button
          className="secondary"
          onClick={() => router.back()}
          style={{ marginBottom: 16 }}
        >
          ← Back
        </button>

        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading request...</p>
        ) : !request ? (
          <p style={{ color: "var(--danger)" }}>Request not found</p>
        ) : (
          <>
            {/* Header */}
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: 24,
              }}
            >
              <div>
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Request Detail</h1>
                <p style={{ fontFamily: "monospace", fontSize: 12, color: "var(--muted)" }}>
                  {request.id}
                </p>
              </div>
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                {statusBadge(request.status)}
                {(request.status === "created" || request.status === "failed") && (
                  <button
                    className="primary"
                    onClick={handleExecute}
                    disabled={executing}
                  >
                    {executing ? "Executing..." : "Execute"}
                  </button>
                )}
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

            {/* Intent */}
            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ fontSize: 14, marginBottom: 8 }}>Task Description</h2>
              <p style={{ fontSize: 14 }}>{request.intent}</p>
            </div>

            {/* Execution Result */}
            {execution && (
              <div className="card" style={{ marginBottom: 16 }}>
                <h2 style={{ fontSize: 14, marginBottom: 12 }}>Execution Result</h2>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div>
                    <span style={{ fontSize: 12, color: "var(--muted)" }}>Workflow ID</span>
                    <p style={{ fontFamily: "monospace", fontSize: 13 }}>{execution.workflow_id}</p>
                  </div>
                  <div>
                    <span style={{ fontSize: 12, color: "var(--muted)" }}>Status</span>
                    <p>{statusBadge(execution.status)}</p>
                  </div>
                </div>

                {execution.errors.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <span style={{ fontSize: 12, color: "var(--danger)" }}>Errors</span>
                    {execution.errors.map((err, i) => (
                      <p key={i} style={{ fontSize: 13, color: "var(--danger)" }}>
                        {err}
                      </p>
                    ))}
                  </div>
                )}

                {Object.keys(execution.final_result).length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <span style={{ fontSize: 12, color: "var(--muted)" }}>Final Result</span>
                    <pre
                      style={{
                        fontSize: 12,
                        background: "#f8f9fa",
                        padding: 12,
                        borderRadius: 6,
                        overflow: "auto",
                        marginTop: 4,
                      }}
                    >
                      {JSON.stringify(execution.final_result, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
