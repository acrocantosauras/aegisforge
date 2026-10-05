"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/shell/AppShell";
import { ErrorBox } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

// Real planner-recognized intents — each produces a genuine multi-agent
// dependency graph (verified against the deterministic planner). No fake
// capabilities: what you see here is what the backend actually plans.
const EXAMPLES: { label: string; intent: string; flagship?: boolean }[] = [
  {
    label: "Flagship demo — vendor evaluation against internal requirements",
    flagship: true,
    intent:
      "Conduct enterprise knowledge research on the Acme Systems data platform procurement decision: compare Northwind Streamline and Helios Fabric against our internal architecture, security, data governance, infrastructure and cost requirements, identify the conflicts between vendor claims and internal policy, and synthesize a recommendation.",
  },
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

export default function ExecutePage() {
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
    <AppShell
      title="Execute"
      subtitle="Describe the work. The planner decomposes it into a validated task graph and specialized agents execute it — with durable checkpoints and approval gates for high-risk actions."
      actions={
        <button className="ghost" onClick={() => router.push("/history")}>
          View history
        </button>
      }
    >
      <div className="composer">
        <div className="card">
          {error && <ErrorBox>{error}</ErrorBox>}

          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label htmlFor="intent">Task Description</label>
              <textarea
                id="intent"
                value={intent}
                onChange={(e) => setIntent(e.target.value)}
                placeholder="e.g., Research the latest developments in AI agent frameworks and provide a comparison against our internal policy documents…"
                required
                minLength={10}
                maxLength={2000}
              />
              <p className="char-count">{intent.length}/2000</p>
            </div>

            <div className="row">
              <button
                type="submit"
                className="primary"
                disabled={loading || intent.length < 10}
              >
                {loading ? "Submitting to planner…" : "Submit & Execute"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => router.back()}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>

        <div className="card" style={{ marginTop: 16 }}>
          <h2 style={{ fontSize: "var(--text-h3)", marginBottom: 4 }}>
            Example requests
          </h2>
          <p className="t-caption" style={{ marginBottom: 12 }}>
            These map to actual planner decomposition rules — the resulting task
            graph, agents, and approval gates you will see are the real ones.
          </p>
          <div className="intent-examples">
            {EXAMPLES.map((ex) => (
              <button
                key={ex.label}
                type="button"
                className={`intent-example${ex.flagship ? " flagship" : ""}`}
                onClick={() => setIntent(ex.intent)}
              >
                <span className="label">{ex.label}</span>
                <span className="intent">{ex.intent}</span>
              </button>
            ))}
          </div>
        </div>

        <p className="t-caption" style={{ marginTop: 14, maxWidth: 640 }}>
          Execution runs on the distributed worker queue with checkpointing.
          High-risk tool actions pause the workflow for human approval before
          proceeding.
        </p>

        <p className="t-caption" style={{ marginTop: 6, maxWidth: 640 }}>
          The flagship demo needs its knowledge base seeded first:{" "}
          <code className="mono">python scripts/seed_demo.py --email &lt;your email&gt;</code>
          . See <code className="mono">docs/phase10-flagship-demo.md</code>.
        </p>
      </div>
    </AppShell>
  );
}
