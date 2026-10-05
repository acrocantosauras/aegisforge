"use client";

import { useEffect, useState } from "react";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState, ErrorBox } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

interface Agent {
  agent_type: string;
  name: string;
  description: string;
  capabilities: string[];
}

const GLYPHS: Record<string, string> = {
  planner: "◈",
  research: "⌕",
  rag: "▤",
  analysis: "◎",
  synthesis: "✦",
  evaluator: "✓",
};

const ROLE_STAGE: Record<string, string> = {
  planner: "stage 1 · planning",
  research: "stage 2 · execution",
  rag: "stage 2 · execution",
  analysis: "stage 3 · reasoning",
  synthesis: "stage 4 · response",
  evaluator: "stage 5 · evaluation",
};

export default function AgentsPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    apiClient
      .listAgents(token)
      .then((data) => {
        if (!cancelled) setAgents(data.agents ?? []);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setAgents([]);
          setError(err instanceof Error ? err.message : "Failed to load agents");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, reloadKey]);

  if (isLoading || !isAuthenticated) return null;

  return (
    <AppShell
      title="Agent Registry"
      subtitle="The specialized agents available to multi-agent workflows. Each agent has defined capabilities and a fixed role in the execution pipeline."
    >
      {loading ? (
        <LoadingLine label="Loading agent registry…" />
      ) : error ? (
        <>
          <ErrorBox>{error}</ErrorBox>
          <button
            className="secondary"
            onClick={() => setReloadKey((k) => k + 1)}
            style={{ marginTop: 12 }}
          >
            Retry
          </button>
        </>
      ) : agents.length === 0 ? (
        <EmptyState glyph="◈" title="No agents registered">
          The agent catalog is empty — this should not happen on a healthy
          platform.
        </EmptyState>
      ) : (
        <div className="registry-grid">
          {agents.map((a) => (
            <div key={a.agent_type} className="registry-card">
              <div className="head">
                <span className="row" style={{ gap: 10 }}>
                  <span
                    aria-hidden="true"
                    style={{
                      width: 30,
                      height: 30,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      borderRadius: "var(--radius-sm)",
                      background: "var(--accent-dim)",
                      border: "1px solid var(--accent-border)",
                      color: "var(--accent)",
                      fontSize: 14,
                    }}
                  >
                    {GLYPHS[a.agent_type] ?? "•"}
                  </span>
                  <span className="name">{a.name}</span>
                </span>
                <span className="mono-pill">
                  {ROLE_STAGE[a.agent_type] ?? "pipeline"}
                </span>
              </div>
              <p className="desc">{a.description}</p>
              <div className="chips">
                {a.capabilities.map((c) => (
                  <span key={c} className="tool-chip">
                    {c}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </AppShell>
  );
}
