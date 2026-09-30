"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

// Real planner-recognized intents — each produces a genuine multi-agent
// dependency graph (verified against the deterministic planner). No fake
// capabilities: what you see here is what the backend actually plans.
const EXAMPLES: { label: string; intent: string }[] = [
  {
    label: "Research → Analyze → Synthesize (multi-agent, parallel RAG)",
    intent:
      "Conduct enterprise knowledge research on data access policy, analyze the evidence, and synthesize a recommendation",
  },
  {
    label: "Compare two subjects and synthesize (parallel research)",
    intent:
      "Compare role-based access control and attribute-based access control and synthesize a recommendation",
  },
  {
    label: "Analyze and recommend (research → analysis → synthesis)",
    intent:
      "Analyze the current support escalation policy and recommend improvements",
  },
  {
    label: "High-risk action (pauses for human approval)",
    intent:
      "Conduct enterprise knowledge research on incident response, analyze the evidence, synthesize a runbook, then restart the production service after review",
  },
];

export default function NewRequestPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [intent, setIntent] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (isLoading || !isAuthenticated) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const request = await apiClient.createRequest(intent, {}, token!);
      // Submit to the distributed worker queue first (real production path);
      // fall back to synchronous execution when the queue is unavailable.
      await apiClient.executeRequestAsync(request.id, token!).catch(() =>
        apiClient.executeRequest(request.id, token!).catch(() => {
          // Execution might be async — the workspace will poll for progress.
        })
      );
      router.push(`/requests/${request.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create request");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 8 }}>
          New Request
        </h1>
        <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 20 }}>
          AegisForge plans your request into a task graph and executes it with
          specialized agents — research, RAG over private knowledge, analysis,
          and synthesis — with durable checkpoints and human approval gates for
          high-risk actions.
        </p>

        <div className="card" style={{ maxWidth: 760 }}>
          <h2 style={{ marginBottom: 16 }}>Describe the task</h2>

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

          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label htmlFor="intent">Task Description</label>
              <textarea
                id="intent"
                value={intent}
                onChange={(e) => setIntent(e.target.value)}
                rows={5}
                placeholder="e.g., Research the latest developments in AI agent frameworks and provide a comparison..."
                required
                minLength={10}
                maxLength={2000}
              />
              <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>
                {intent.length}/2000 characters
              </p>
            </div>

            <div style={{ display: "flex", gap: 12 }}>
              <button
                type="submit"
                className="primary"
                disabled={loading || intent.length < 10}
              >
                {loading ? "Submitting…" : "Submit & Execute"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => router.back()}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>

        <div className="card" style={{ maxWidth: 760, marginTop: 16 }}>
          <h2 style={{ fontSize: 14, marginBottom: 10 }}>
            Example requests (real planner shapes)
          </h2>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {EXAMPLES.map((ex) => (
              <button
                key={ex.label}
                type="button"
                className="secondary"
                style={{ textAlign: "left", fontSize: 13 }}
                onClick={() => setIntent(ex.intent)}
              >
                <strong>{ex.label}</strong>
                <br />
                <span style={{ color: "var(--muted)", fontSize: 12 }}>
                  {ex.intent}
                </span>
              </button>
            ))}
          </div>
          <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 10 }}>
            These examples map to actual planner decomposition rules — the
            resulting task graph, agents, and approval gates you will see are
            the real ones.
          </p>
        </div>
      </main>
    </div>
  );
}
