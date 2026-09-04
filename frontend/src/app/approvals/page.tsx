"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

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

export default function ApprovalsPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [decisionReason, setDecisionReason] = useState<Record<string, string>>({});
  const [processingId, setProcessingId] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;

    async function fetchApprovals() {
      try {
        const result = await apiClient.listApprovals(token!);
        setApprovals(result.approvals || []);
      } catch {
        // Endpoint may not exist yet
      } finally {
        setLoading(false);
      }
    }

    fetchApprovals();
  }, [token]);

  const handleDecision = async (approvalId: string, decision: "approve" | "reject") => {
    if (!token) return;
    setProcessingId(approvalId);
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
      alert(err instanceof Error ? err.message : "Failed to process decision");
    } finally {
      setProcessingId(null);
    }
  };

  if (isLoading || !isAuthenticated) return null;

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24 }}>
          Approval Requests
        </h1>

        {loading ? (
          <p style={{ color: "var(--muted)" }}>Loading approvals...</p>
        ) : approvals.length === 0 ? (
          <div className="card">
            <div className="empty-state">No pending approval requests.</div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {approvals.map((approval) => (
              <div key={approval.approval_id} className="card">
                <div className="card-header">
                  <div>
                    <h2 style={{ fontSize: 16 }}>{approval.action_description}</h2>
                    <p style={{ fontSize: 12, color: "var(--muted)", fontFamily: "monospace" }}>
                      {approval.approval_id}
                    </p>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <span
                      className={`badge ${
                        approval.risk_level === "high" || approval.risk_level === "critical"
                          ? "danger"
                          : "warning"
                      }`}
                    >
                      {approval.risk_level}
                    </span>
                    <span
                      className={`badge ${
                        approval.status === "approved"
                          ? "success"
                          : approval.status === "rejected"
                          ? "danger"
                          : "info"
                      }`}
                    >
                      {approval.status}
                    </span>
                  </div>
                </div>

                {approval.reason && (
                  <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 12 }}>
                    Reason: {approval.reason}
                  </p>
                )}

                {approval.status === "pending" && (
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12, marginTop: 8 }}>
                    <div className="form-group">
                      <label>Decision Reason</label>
                      <input
                        type="text"
                        value={decisionReason[approval.approval_id] || ""}
                        onChange={(e) =>
                          setDecisionReason((prev) => ({
                            ...prev,
                            [approval.approval_id]: e.target.value,
                          }))
                        }
                        placeholder="Optional reason for your decision..."
                      />
                    </div>
                    <div style={{ display: "flex", gap: 12 }}>
                      <button
                        className="primary"
                        onClick={() => handleDecision(approval.approval_id, "approve")}
                        disabled={processingId === approval.approval_id}
                      >
                        {processingId === approval.approval_id ? "Processing..." : "Approve"}
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
                )}

                <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 8 }}>
                  Created: {approval.created_at}
                </p>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
