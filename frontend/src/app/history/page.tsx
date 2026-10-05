"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState, ErrorBox } from "@/components/ui/states";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { statusBadgeClass, taskStatus, formatRelativeTime } from "@/lib/status";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
  { key: "running", label: "Running" },
  { key: "waiting_for_approval", label: "Awaiting Approval" },
] as const;

function matchesFilter(status: string, filter: string): boolean {
  if (filter === "all") return true;
  if (filter === "running")
    return ["created", "queued", "planning", "executing"].includes(status);
  return status === filter;
}

function HistoryView() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();

  const [items, setItems] = useState<
    { id: string; intent: string; status: string; created_at: string }[]
  >([]);
  const [filter, setFilter] = useState<string>("all");
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    async function fetchHistory() {
      try {
        const data = await apiClient.getRequestHistory(token!);
        if (!cancelled) {
          setItems(Array.isArray(data) ? data : []);
          setLoadError("");
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setItems([]);
          setLoadError(
            err instanceof Error ? err.message : "Failed to load execution history"
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    fetchHistory();
    return () => {
      cancelled = true;
    };
  }, [token, reloadKey]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter(
      (r) =>
        matchesFilter(r.status, filter) &&
        (q === "" || r.intent.toLowerCase().includes(q))
    );
  }, [items, filter, query]);

  const counts = useMemo(() => {
    const c = {
      all: items.length,
      completed: 0,
      failed: 0,
      running: 0,
      waiting_for_approval: 0,
    };
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
    <AppShell
      title="Execution History"
      subtitle="Every request you have submitted, with its workflow outcome. Select a row to open its execution workspace."
      actions={
        <Link href="/execute">
          <button className="primary">New Execution</button>
        </Link>
      }
    >
      <div className="filter-row">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`chip ${filter === f.key ? "active" : ""}`}
            onClick={() => setFilter(f.key)}
            aria-pressed={filter === f.key}
          >
            {f.label}
            {f.key !== "all"
              ? ` (${counts[f.key as keyof typeof counts] ?? 0})`
              : ` (${counts.all})`}
          </button>
        ))}
        <div className="spacer" />
        <input
          aria-label="Search intents"
          placeholder="Search intents…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ maxWidth: 260, padding: "7px 12px", fontSize: "var(--text-caption)" }}
        />
      </div>

      {loadError ? (
        <>
          <ErrorBox>{loadError}</ErrorBox>
          <button className="secondary" onClick={() => setReloadKey((k) => k + 1)}>
            Retry
          </button>
        </>
      ) : loading ? (
        <LoadingLine label="Loading history…" />
      ) : (
        <div className="table-wrap">
          {filtered.length === 0 ? (
            <EmptyState glyph="◇" title={items.length === 0 ? "No executions yet" : "No matches"}>
              <p>
                {items.length === 0
                  ? "Submit your first request to see it here."
                  : "No executions match the current filter."}
              </p>
            </EmptyState>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Submitted</th>
                  <th>Request</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr
                    key={r.id}
                    className="clickable"
                    onClick={() => router.push(`/requests/${r.id}`)}
                  >
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>
                      {formatRelativeTime(r.created_at)}
                    </td>
                    <td className="wrap" style={{ maxWidth: 420 }}>
                      {r.intent.length > 110 ? `${r.intent.slice(0, 110)}…` : r.intent}
                    </td>
                    <td>
                      <span className={statusBadgeClass(r.status)}>
                        {taskStatus(r.status).label}
                      </span>
                    </td>
                    <td>
                      <Link href={`/requests/${r.id}`} className="t-caption text-accent">
                        Open →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </AppShell>
  );
}

/**
 * `useSearchParams()` opts a route out of static prerendering unless it sits
 * inside a Suspense boundary. Keep the boundary here so `next build` succeeds
 * without disabling static generation.
 */
export default function HistoryPage() {
  return (
    <Suspense fallback={null}>
      <HistoryView />
    </Suspense>
  );
}
