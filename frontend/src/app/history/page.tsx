"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

const STATUS_BADGES: Record<string, string> = {
  completed: "badge success",
  failed: "badge danger",
  cancelled: "badge danger",
  executing: "badge info",
  planning: "badge info",
  queued: "badge info",
  created: "badge muted",
  waiting_for_approval: "badge warning",
};

const FILTERS = [
  { key: "all", label: "All" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
  { key: "running", label: "Running" },
  { key: "waiting_for_approval", label: "Needs Approval" },
] as const;

function matchesFilter(status: string, filter: string): boolean {
  if (filter === "all") return true;
  if (filter === "running")
    return ["created", "queued", "planning", "executing"].includes(status);
  return status === filter;
}

export default function HistoryPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  const [items, setItems] = useState<
    { id: string; intent: string; status: string; created_at: string }[]
  >([]);
  const [filter, setFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    async function fetchHistory() {
      try {
        const data = await apiClient.getRequestHistory(token!);
        if (!cancelled) setItems(Array.isArray(data) ? data : []);
      } catch {
        if (!cancelled) setItems([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    fetchHistory();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter(
      (r) =>
        matchesFilter(r.status, filter) &&
        (q === "" || r.intent.toLowerCase().includes(q))
    );
  }, [items, filter, query]);

  const counts = useMemo(() => {
    const c = { all: items.length, completed: 0, failed: 0, running: 0, waiting_for_approval: 0 };
    for (const r of items) {
      if (matchesFilter(r.status, "completed")) c.completed += 1;
      else if (matchesFilter(r.status, "failed")) c.failed += 1;
      else if (matchesFilter(r.status, "running")) c.running += 1;
      else if (r.status === "waiting_for_approval") c.waiting_for_approval += 1;
    }
    return c;
  }, [items]);

  if (isLoading || !isAuthenticated) return null;

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 6 }}>
          Execution History
        </h1>
        <p style={{ color: "var(--muted)", marginBottom: 20 }}>
          Every request you have submitted, with its workflow outcome.
          Select a row to open its execution workspace.
        </p>

        <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap", alignItems: "center" }}>
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={filter === f.key ? "primary" : "secondary"}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
              {f.key !== "all" ? ` (${counts[f.key as keyof typeof counts] ?? 0})` : ` (${counts.all})`}
            </button>
            ))}
          <input
            aria-label="Search intents"
            placeholder="Search intents…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{
              marginLeft: "auto",
              padding: "8px 12px",
              border: "1px solid var(--border)",
              borderRadius: 6,
              minWidth: 220,
            }}
          />
          <Link href="/requests/new">
            <button className="primary">New Request</button>
          </Link>
        </div>

        <div className="card">
          {loading ? (
            <div className="empty-state">Loading history…</div>
          ) : filtered.length === 0 ? (
            <div className="empty-state">
              {items.length === 0
                ? "No executions yet. Submit your first request to see it here."
                : "No executions match the current filter."}
            </div>
            ) : (
            <table>
              <thead>
                <tr>
                  <th>Submitted</th>
                  <th>Intent</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr
                    key={r.id}
                    style={{ cursor: "pointer" }}
                    onClick={() => router.push(`/requests/${r.id}`)}
                  >
                    <td style={{ whiteSpace: "nowrap", fontSize: 12, color: "var(--muted)" }}>
                      {new Date(r.created_at).toLocaleString()}
                    </td>
                    <td>{r.intent.length > 90 ? r.intent.slice(0, 90) + "…" : r.intent}</td>
                    <td>
                      <span className={STATUS_BADGES[r.status] ?? "badge muted"}>
                        {r.status.replace(/_/g, " ")}
                      </span>
                    </td>
                    <td>
                      <Link href={`/requests/${r.id}`}>Open</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </div>
  );
}
