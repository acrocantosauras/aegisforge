/**
 * Status mapping shared across the app: workflow/task/request states,
 * MCP/tool health, risk levels. Single source of truth for badges and dots.
 */

export type StatusVisual =
  | "success"
  | "running"
  | "warning"
  | "error"
  | "pending"
  | "muted";

interface StatusSpec {
  label: string;
  cls: string;
  visual: StatusVisual;
}

const TASK_STATUS: Record<string, StatusSpec> = {
  completed: { label: "Complete", cls: "success", visual: "success" },
  succeeded: { label: "Complete", cls: "success", visual: "success" },
  running: { label: "Running", cls: "running", visual: "running" },
  executing: { label: "Running", cls: "running", visual: "running" },
  planning: { label: "Planning", cls: "running", visual: "running" },
  queued: { label: "Queued", cls: "info", visual: "running" },
  pending: { label: "Waiting", cls: "pending", visual: "pending" },
  waiting_for_approval: {
    label: "Needs Approval",
    cls: "warning",
    visual: "warning",
  },
  action_requires_approval: {
    label: "Approval Gate",
    cls: "warning",
    visual: "warning",
  },
  retrying: { label: "Retrying", cls: "warning", visual: "warning" },
  failed: { label: "Failed", cls: "danger", visual: "error" },
  timeout: { label: "Timed Out", cls: "danger", visual: "error" },
  denied: { label: "Denied", cls: "danger", visual: "error" },
  cancelled: { label: "Cancelled", cls: "muted", visual: "muted" },
  created: { label: "Created", cls: "pending", visual: "pending" },
};

export function taskStatus(status: string): StatusSpec {
  return TASK_STATUS[status.toLowerCase()] ?? {
    label: status.replace(/_/g, " ") || "unknown",
    cls: "muted",
    visual: "muted" as StatusVisual,
  };
}

export function statusBadgeClass(status: string): string {
  return `badge ${taskStatus(status).cls}`;
}

/** Dot class for live status pills. */
export function statusDotClass(visual: StatusVisual): string {
  switch (visual) {
    case "success":
      return "status-dot ok";
    case "running":
      return "status-dot ok pulse";
    case "warning":
      return "status-dot warn";
    case "error":
      return "status-dot err";
    default:
      return "status-dot idle";
  }
}

/* ---------- Tool / MCP health ---------- */

export type ToolHealth = "healthy" | "degraded" | "unavailable" | "unknown";

export function toolHealthClass(h: string): string {
  switch (h.toLowerCase()) {
    case "healthy":
      return "ok";
    case "degraded":
      return "warn";
    case "unavailable":
      return "err";
    default:
      return "idle";
  }
}

/* ---------- Risk levels ---------- */

export function riskBadgeClass(risk: string): string {
  switch (risk.toLowerCase()) {
    case "critical":
    case "high":
      return "badge danger";
    case "medium":
      return "badge warning";
    default:
      return "badge pending";
  }
}

/* ---------- Formatting helpers ---------- */

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Date.now() - then;
  const abs = Math.abs(diff);
  const future = diff < 0;
  const fmt = (n: number, unit: string) =>
    future ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (abs < 60_000) return fmt(Math.max(1, Math.round(abs / 1000)), "s");
  if (abs < 3_600_000) return fmt(Math.round(abs / 60_000), "m");
  if (abs < 86_400_000) return fmt(Math.round(abs / 3_600_000), "h");
  return fmt(Math.round(abs / 86_400_000), "d");
}

export function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 12)}…` : id;
}
