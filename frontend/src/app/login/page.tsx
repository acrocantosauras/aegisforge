"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ShieldCheck, GitBranch, Activity } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { BrandMark } from "@/components/shell/AppShell";

const HIGHLIGHTS = [
  {
    icon: <GitBranch size={15} />,
    title: "Multi-agent orchestration",
    text: "Requests are planned into validated task graphs and executed by specialized agents.",
  },
  {
    icon: <ShieldCheck size={15} />,
    title: "Security by default",
    text: "Tenant isolation, permission gates, and human approval for high-risk actions.",
  },
  {
    icon: <Activity size={15} />,
    title: "Durable execution",
    text: "Checkpointed workflows survive restarts — retries, recovery, and audit trails built in.",
  },
];

export default function LoginPage() {
  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const { login, register } = useAuth();
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      if (isRegister) {
        await register(email, password, fullName);
        localStorage.setItem("aegisforge_email", email);
      } else {
        await login(email, password);
        localStorage.setItem("aegisforge_email", email);
      }
      router.push("/dashboard");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-panel">
        <div className="login-brand-side">
          <div className="lp-hero-bg" aria-hidden="true">
            <div className="lp-hero-grid" />
            <div className="lp-hero-glow" />
          </div>
          <div className="brand-row">
            <BrandMark size={30} />
            <span className="brand-name" style={{ fontSize: "1.05rem" }}>
              AEGIS<span className="thin">FORGE</span>
            </span>
          </div>
          <h2 className="login-tagline">
            The control plane for multi-agent AI work.
          </h2>
          <div className="login-highlights">
            {HIGHLIGHTS.map((h) => (
              <div key={h.title} className="login-highlight">
                <span className="hl-icon">{h.icon}</span>
                <span>
                  <strong>{h.title}</strong>
                  <p>{h.text}</p>
                </span>
              </div>
            ))}
          </div>
          <p className="login-foot">
            Plan → execute → evaluate → recover. Real workflows, real oversight.
          </p>
        </div>

        <div className="login-form-side">
          <div className="login-form-box">
            <h1 style={{ fontSize: "var(--text-h2)", fontWeight: 640, letterSpacing: "-0.02em" }}>
              {isRegister ? "Create your account" : "Sign in to AegisForge"}
            </h1>
            <p className="t-small" style={{ marginBottom: 24 }}>
              {isRegister
                ? "Set up your operator account to launch workflows."
                : "Enter your credentials to access the workspace."}
            </p>

            {error && (
              <div className="error" role="alert" style={{ marginBottom: 16 }}>
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit}>
              {isRegister && (
                <div className="form-group">
                  <label htmlFor="fullName">Full Name</label>
                  <input
                    id="fullName"
                    type="text"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    required
                    autoComplete="name"
                  />
                </div>
              )}

              <div className="form-group">
                <label htmlFor="email">Email</label>
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                />
              </div>

              <div className="form-group">
                <label htmlFor="password">Password</label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={8}
                  autoComplete={isRegister ? "new-password" : "current-password"}
                />
              </div>

              <button
                type="submit"
                className="primary"
                style={{ width: "100%", padding: 11, marginTop: 8 }}
                disabled={loading}
              >
                {loading ? "Please wait…" : isRegister ? "Create Account" : "Sign In"}
              </button>
            </form>

            <p style={{ textAlign: "center", marginTop: 18, fontSize: 13, color: "var(--fg-muted)" }}>
              {isRegister ? "Already have an account?" : "Need an account?"}{" "}
              <button
                type="button"
                className="ghost"
                style={{ color: "var(--accent)", padding: 0, fontWeight: 600 }}
                onClick={() => {
                  setIsRegister(!isRegister);
                  setError("");
                }}
              >
                {isRegister ? "Sign In" : "Register"}
              </button>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
