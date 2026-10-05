"use client";

import { useEffect, useState } from "react";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState, ErrorBox } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { riskBadgeClass } from "@/lib/status";

interface Tool {
  name: string;
  description: string;
  version: string;
  permission_requirements: string[];
  timeout_seconds: number;
  requires_approval: boolean;
  risk_level: string;
  read_only: boolean;
  external_side_effect: boolean;
  data_sensitivity: string;
}

interface MCPServer {
  server_id: string;
  name: string;
  description: string;
  version: string;
  transport: string;
  enabled: boolean;
  allowed_tools: string[];
  risk_level: string;
  read_only_default: boolean;
  timeout_seconds: number;
  health: Record<string, unknown>;
  tool_count: number;
}

export default function ToolsPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const [tab, setTab] = useState<"tools" | "mcp">("tools");
  const [tools, setTools] = useState<Tool[]>([]);
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([
      apiClient.listTools(token).catch(() => null),
      apiClient.listMCPServers(token).catch(() => null),
    ]).then(([t, s]) => {
      if (cancelled) return;
      setTools(t?.tools ?? []);
      setServers(s?.servers ?? []);
      if (!t && !s) setError("Tool and MCP catalogs are unavailable.");
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [token, reloadKey]);

  if (isLoading || !isAuthenticated) return null;

  return (
    <AppShell
      title="Tools & MCP"
      subtitle="The tool boundary agents can act through: permission-checked, risk-classified, deny-by-default. MCP servers are operator-configured platform infrastructure."
    >
      <div className="filter-row">
        <button
          className={`chip ${tab === "tools" ? "active" : ""}`}
          onClick={() => setTab("tools")}
          aria-pressed={tab === "tools"}
        >
          Platform tools ({tools.length})
        </button>
        <button
          className={`chip ${tab === "mcp" ? "active" : ""}`}
          onClick={() => setTab("mcp")}
          aria-pressed={tab === "mcp"}
        >
          MCP servers ({servers.length})
        </button>
      </div>

      {loading ? (
        <LoadingLine label="Loading tool catalog…" />
      ) : error && tools.length === 0 && servers.length === 0 ? (
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
      ) : tab === "tools" ? (
        tools.length === 0 ? (
          <EmptyState glyph="🛠" title="No tools registered" />
        ) : (
          <div className="registry-grid">
            {tools.map((t) => (
              <div key={t.name} className="registry-card">
                <div className="head">
                  <span className="name">{t.name}</span>
                  <span className={riskBadgeClass(t.risk_level)}>{t.risk_level} risk</span>
                </div>
                <p className="desc">{t.description}</p>
                <div className="chips" style={{ marginBottom: 10 }}>
                  {t.read_only && <span className="tool-chip">read-only</span>}
                  {t.external_side_effect && (
                    <span className="tool-chip" style={{ color: "var(--warning)" }}>
                      side effects
                    </span>
                  )}
                  {t.requires_approval && (
                    <span className="tool-chip" style={{ color: "var(--warning)" }}>
                      approval required
                    </span>
                  )}
                  <span className="tool-chip">timeout {t.timeout_seconds}s</span>
                  <span className="tool-chip">v{t.version}</span>
                </div>
                {t.permission_requirements.length > 0 && (
                  <p className="t-caption mono">
                    permissions: {t.permission_requirements.join(", ")}
                  </p>
                )}
              </div>
            ))}
          </div>
        )
      ) : servers.length === 0 ? (
        <EmptyState glyph="◉" title="No MCP servers configured">
          MCP integration is operator-configured. When servers are added to the
          catalog, their health, risk classification, and allowed tools appear
          here.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Server</th>
                <th>Transport</th>
                <th>Tools</th>
                <th>Risk</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {servers.map((s) => (
                <tr key={s.server_id}>
                  <td>
                    <span style={{ fontWeight: 600 }}>{s.name}</span>
                    <div className="t-caption mono">{s.server_id}</div>
                  </td>
                  <td className="mono">{s.transport}</td>
                  <td className="mono">{s.tool_count}</td>
                  <td>
                    <span className={riskBadgeClass(s.risk_level)}>{s.risk_level}</span>
                  </td>
                  <td>
                    {s.enabled ? (
                      <span className="row" style={{ gap: 7 }}>
                        <span className="status-dot ok" /> enabled
                      </span>
                    ) : (
                      <span className="row" style={{ gap: 7 }}>
                        <span className="status-dot idle" /> disabled
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AppShell>
  );
}
