"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import {
  LayoutDashboard,
  PlayCircle,
  History,
  Bot,
  Database,
  Wrench,
  ShieldCheck,
  Activity,
  Settings,
  LogOut,
  Menu,
  X,
} from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiClient, type SystemStatus } from "@/lib/api";

export function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M12 2L4 5.5v5.1c0 4.6 3.2 8.9 8 10.4 4.8-1.5 8-5.8 8-10.4V5.5L12 2z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path
        d="M12 6.5l-4.2 1.8v2.9c0 2.7 1.8 5.2 4.2 6.1 2.4-.9 4.2-3.4 4.2-6.1V8.3L12 6.5z"
        fill="currentColor"
        opacity="0.35"
      />
      <path d="M12 8.5v7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M9.5 10.5L12 8.5l2.5 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  badge?: number;
}

const SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "Workspace",
    items: [
      { href: "/dashboard", label: "Overview", icon: <LayoutDashboard size={15} /> },
      { href: "/execute", label: "Execute", icon: <PlayCircle size={15} /> },
      { href: "/history", label: "History", icon: <History size={15} /> },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { href: "/agents", label: "Agents", icon: <Bot size={15} /> },
      { href: "/documents", label: "Knowledge", icon: <Database size={15} /> },
      { href: "/tools", label: "Tools & MCP", icon: <Wrench size={15} /> },
    ],
  },
  {
    label: "Operations",
    items: [
      { href: "/approvals", label: "Approvals", icon: <ShieldCheck size={15} /> },
      { href: "/system", label: "System Health", icon: <Activity size={15} /> },
      { href: "/settings", label: "Settings", icon: <Settings size={15} /> },
    ],
  },
];

export function useSystemStatus(enabled: boolean, token?: string | null) {
  const [status, setStatus] = useState<SystemStatus | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const load = () =>
      apiClient
        .systemStatus(token)
        .then((s) => {
          if (!cancelled) setStatus(s);
        })
        .catch(() => {
          if (!cancelled) setStatus(null);
        });
    load();
    const t = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [enabled, token]);

  return status;
}

export function SystemPill({ status }: { status: SystemStatus | null }) {
  if (!status) {
    return (
      <span className="system-pill" aria-label="System status unknown">
        <span className="status-dot idle" />
        SYSTEM
      </span>
    );
  }
  const cls =
    status.status === "healthy" ? "ok" : status.status === "degraded" ? "degraded" : "down";
  const c = status.components;
  const label =
    status.status === "healthy"
      ? "ALL SYSTEMS OPERATIONAL"
      : status.status === "degraded"
        ? "DEGRADED"
        : "OFFLINE";
  return (
    <span className={`system-pill ${cls}`} aria-label={`System status: ${status.status}`}>
      <span className={c.workers.active > 0 ? "status-dot ok pulse" : "status-dot warn"} />
      {label}
    </span>
  );
}

export default function AppShell({
  title,
  subtitle,
  actions,
  children,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { token, logout, isAuthenticated, isLoading } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [approvalCount, setApprovalCount] = useState(0);
  const status = useSystemStatus(!!isAuthenticated && !isLoading, token);

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated) router.push("/login");
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;
    apiClient
      .listApprovals(token)
      .then((r) => setApprovalCount(r.total ?? 0))
      .catch(() => {});
  }, [token, pathname]);

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  if (isLoading || !isAuthenticated) return null;

  const email = localStorage.getItem("aegisforge_email") || "operator";
  const initials = email.slice(0, 2).toUpperCase();

  return (
    <div className="shell">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <Link href="/dashboard" className="brand" aria-label="AegisForge home">
          <BrandMark />
          <span className="brand-name">
            AEGIS<span className="thin">FORGE</span>
          </span>
        </Link>

        {SECTIONS.map((section) => (
          <div key={section.label}>
            <div className="section-label">{section.label}</div>
            {section.items.map((item) => {
              const active =
                pathname === item.href ||
                (item.href === "/execute" && pathname.startsWith("/requests"));
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item ${active ? "active" : ""}`}
                >
                  {item.icon}
                  {item.label}
                  {item.href === "/approvals" && approvalCount > 0 && (
                    <span className="nav-badge" aria-label={`${approvalCount} pending`}>
                      {approvalCount}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        ))}

        <div className="spacer" />

        <div className="user-box">
          <div className="user-row">
            <span className="user-avatar" aria-hidden="true">
              {initials}
            </span>
            <span className="user-meta">
              <span className="name">{email}</span>
              <span className="sub">operator · default-org</span>
            </span>
          </div>
          <button
            className="signout ghost"
            onClick={() => {
              logout();
              router.push("/login");
            }}
          >
            <LogOut size={13} />
            Sign out
          </button>
        </div>
      </aside>

      {menuOpen && (
        <button
          className="sidebar-overlay"
          aria-label="Close navigation"
          onClick={() => setMenuOpen(false)}
        />
      )}

      <div className="main-col">
        <header className="topbar">
          <button
            className="ghost compact sidebar-toggle"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            onClick={() => setMenuOpen((v) => !v)}
          >
            {menuOpen ? <X size={16} /> : <Menu size={16} />}
          </button>
          <span className="page-title">{title}</span>
          <div className="search">
            <input
              type="search"
              placeholder="Search executions, agents, docs…"
              aria-label="Global search"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  const q = (e.target as HTMLInputElement).value.trim();
                  if (q) router.push(`/history?q=${encodeURIComponent(q)}`);
                }
              }}
            />
          </div>
          <SystemPill status={status} />
        </header>
        <main className={`page ${wide ? "full" : ""} fade-in`} id="main-content">
          <div className="page-head">
            <div>
              <h1>{title}</h1>
              {subtitle && <p className="page-sub">{subtitle}</p>}
            </div>
            {actions && <div className="actions">{actions}</div>}
          </div>
          {children}
        </main>
      </div>
    </div>
  );
}
