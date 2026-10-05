"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState, ErrorBox } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { riskBadgeClass, formatRelativeTime } from "@/lib/status";

interface Approval {
  approval_id: string;
  job_id: string;
  request_id: string;
  action_description: string;
  risk_level: string;
  status: string;
  reason: string;
  created_at: string;
}

function RiskCard({ approval, children }: { approval: Approval; children: React.ReactNode }) {
  const high = ["high", "critical"].includes(approval.risk_level.toLowerCase());
  return (
    <div
      className="card"
      style={{
        borderColor: high ? "var(--error-border)" : "var(--warning-border)",
        boxShadow: high ? "0 0 0 1px var(--error-border)" : undefined,
      }}
    >
      {children}
    </div>
  );
}

export default function ApprovalsPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [decisionError, setDecisionError] = useState("");
  const [decisionReason, setDecisionReason] = useState<Record<string, string>>({});
  const [processingId, setProcessingId] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    try {
      const result = await apiClient.listApprovals(token);
      setApprovals(result.approvals || []);
      setLoadError("");
    } catch (err: unknown) {
      setLoadError(
        err instanceof Error ? err.message : "Failed to load the approval queue"
      );
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  const handleDecision = async (approvalId: string, decision: "approve" | "reject") => {
    if (!token) return;
    setProcessingId(approvalId);
    setDecisionError("");
    try {
      const reason = decisionReason[approvalId] || "";
      if (decision === "approve") {
        await apiClient.approveRequest(approvalId, reason, token);
      } else {
        await apiClient.rejectRequest(approvalId, reason, token);
      }
      // Refresh the list
      setApprovals((prev) =>
        prev.map((a) =>
          a.approval_id === approvalId
            ? { ...a, status: decision === "approve" ? "approved" : "rejected" }
            : a
        )
      );
    } catch (err: unknown) {
      setDecisionError(
        err instanceof Error ? err.message : "Failed to process decision"
      );
    } finally {
      setProcessingId(null);
    }
  };

  if (isLoading || !isAuthenticated) return null;

  const pending = approvals.filter((a) => a.status === "pending");
  const decided = approvals.filter((a) => a.status !== "pending");

  return (
    <AppShell
      title="Approvals"
      subtitle="High-risk tool actions pause their workflow until a human decides. Every decision is recorded in the audit trail."
    >
      {loadError ? (
        <>
          <ErrorBox>{loadError}</ErrorBox>
          <button className="secondary" onClick={load}>
            Retry
          </button>
        </>
      ) : loading ? (
        <LoadingLine label="Loading approval queue…" />
      ) : approvals.length === 0 ? (
        <div className="card">
          <EmptyState glyph="✓" title="Queue clear">
            <p>No approval requests. High-risk actions will appear here when a workflow hits an approval gate.</p>
          </EmptyState>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {decisionError && <ErrorBox>{decisionError}</ErrorBox>}
          {pending.map((approval) => (
            <RiskCard key={approval.approval_id} approval={approval}>
              <div className="card-header" style={{ alignItems: "flex-start" }}>
                <div style={{ minWidth: 0 }}>
                  <h2 style={{ fontSize: "var(--text-body)" }}>
                    {approval.action_description}
                  </h2>
                  <p className="t-caption mono" style={{ marginTop: 4 }}>
                    {approval.approval_id} · requested {formatRelativeTime(approval.created_at)}
                  </p>
                </div>
                <div className="row" style={{ flexShrink: 0 }}>
                  <span className={riskBadgeClass(approval.risk_level)}>
                    {approval.risk_level} risk
                  </span>
                  <span className="badge warning">pending</span>
                </div>
              </div>

              {approval.reason && (
                <p className="t-small" style={{ marginBottom: 12, color: "var(--fg-secondary)" }}>
                  <span className="t-caps" style={{ display: "block", marginBottom: 4 }}>
                    Reason
                  </span>
                  {approval.reason}
                </p>
              )}

              <div
                style={{
                  borderTop: "1px solid var(--border)",
                  paddingTop: 14,
                  marginTop: 4,
                }}
              >
                <div className="form-group">
                  <label htmlFor={`reason-${approval.approval_id}`}>
                    Decision Reason
                  </label>
                  <input
                    id={`reason-${approval.approval_id}`}
                    type="text"
                    value={decisionReason[approval.approval_id] || ""}
                    onChange={(e) =>
                      setDecisionReason((prev) => ({
                        ...prev,
                        [approval.approval_id]: e.target.value,
                      }))
                    }
                    placeholder="Optional context recorded with your decision…"
                  />
                </div>
                <div className="row">
                  <button
                    className="primary"
                    onClick={() => handleDecision(approval.approval_id, "approve")}
                    disabled={processingId === approval.approval_id}
                  >
                    {processingId === approval.approval_id ? "Processing…" : "Approve"}
                  </button>
                  <button
                    className="danger"
                    onClick={() => handleDecision(approval.approval_id, "reject")}
                    disabled={processingId === approval.approval_id}
                  >
                    Reject
                  </button>
                </div>
              </div>
            </RiskCard>
          ))}

          {decided.length > 0 && (
            <div className="card">
              <div className="card-header">
                <h2>Recent Decisions</h2>
              </div>
              {decided.map((a) => (
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
                    {a.action_description.length > 80
                      ? `${a.action_description.slice(0, 80)}…`
                      : a.action_description}
                  </span>
                  <span
                    className={`badge ${
                      a.status === "approved" ? "success" : "danger"
                    }`}
                  >
                    {a.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}
