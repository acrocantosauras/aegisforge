"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/shell/AppShell";
import { useAuth } from "@/lib/auth";

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const part = token.split(".")[1];
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function ReadOnlyPill() {
  return <span className="mono-pill read-only-pill">read-only</span>;
}

export default function SettingsPage() {
  const { token, isAuthenticated, isLoading, logout } = useAuth();
  const router = useRouter();
  const [claims, setClaims] = useState<Record<string, unknown> | null>(null);
  const [email, setEmail] = useState("operator");

  useEffect(() => {
    if (token) setClaims(decodeJwtPayload(token));
  }, [token]);

  useEffect(() => {
    setEmail(localStorage.getItem("aegisforge_email") || "operator");
  }, []);

  if (isLoading || !isAuthenticated) return null;

  const exp = typeof claims?.exp === "number" ? new Date(claims.exp * 1000) : null;
  const iat = typeof claims?.iat === "number" ? new Date(claims.iat * 1000) : null;
  const sub = typeof claims?.sub === "string" ? claims.sub : null;
  const org =
    (typeof claims?.org === "string" && claims.org) ||
    (typeof claims?.organization_id === "string" && claims.organization_id) ||
    (typeof claims?.tenant_id === "string" && claims.tenant_id) ||
    null;
  const roles = Array.isArray(claims?.roles)
    ? (claims!.roles as unknown[]).map(String).join(", ")
    : typeof claims?.scope === "string"
      ? claims.scope
      : null;

  return (
    <AppShell
      title="Settings"
      subtitle="Your account, session, and the platform configuration this client can see. Nothing here is editable from the UI."
    >
      <div className="settings-grid">
        <div className="card">
          <div className="card-header">
            <h2>Profile</h2>
            <ReadOnlyPill />
          </div>
          <div className="setting-row">
            <span className="k">
              Email
              <span className="sub">Sign-in identity for this workspace</span>
            </span>
            <span className="mono-pill">{email}</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Subject
              <span className="sub">JWT sub claim — your user ID</span>
            </span>
            <span className="mono-pill">{sub ? `${sub.slice(0, 18)}…` : "not exposed"}</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Organization
              <span className="sub">Tenant boundary for all data access</span>
            </span>
            <span className="mono-pill">{org ?? "not exposed by token"}</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Roles
              <span className="sub">Authorization claims on this session</span>
            </span>
            <span className="mono-pill">{roles ?? "not exposed by token"}</span>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h2>Session</h2>
          </div>
          <div className="setting-row">
            <span className="k">
              Authentication
              <span className="sub">Bearer JWT, transmitted per request</span>
            </span>
            <span className="mono-pill">access_token</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Stored in
              <span className="sub">Where this browser keeps your token</span>
            </span>
            <span className="mono-pill">localStorage</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Issued
              <span className="sub">Token issuance time (iat)</span>
            </span>
            <span className="mono-pill">{iat ? iat.toLocaleString() : "—"}</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Expires
              <span className="sub">You will be returned to sign-in after expiry</span>
            </span>
            <span className="mono-pill">{exp ? exp.toLocaleString() : "—"}</span>
          </div>
          <div className="setting-row">
            <span className="k">
              Sign out
              <span className="sub">Clear the stored token on this device</span>
            </span>
            <button
              className="danger"
              onClick={() => {
                logout();
                router.push("/login");
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h2>Platform</h2>
          <ReadOnlyPill />
        </div>
        <div className="setting-row">
          <span className="k">
            API endpoint
            <span className="sub">Control-plane base URL this client calls</span>
          </span>
          <span className="mono-pill">{API_BASE}</span>
        </div>
        <div className="setting-row">
          <span className="k">
            Execution mode
            <span className="sub">
              Distributed worker queue with checkpointing (falls back to sync
              execution when the queue is unreachable)
            </span>
          </span>
          <span className="mono-pill">queue + checkpoint</span>
        </div>
        <div className="setting-row">
          <span className="k">
            Approval policy
            <span className="sub">High-risk tool actions pause for human review</span>
          </span>
          <span className="mono-pill">high · critical</span>
        </div>
        <div className="setting-row">
          <span className="k">
            Data residency
            <span className="sub">
              Documents, vectors, and checkpoints are tenant-isolated in
              PostgreSQL + pgvector
            </span>
          </span>
          <span className="mono-pill">tenant-isolated</span>
        </div>
      </div>
    </AppShell>
  );
}
