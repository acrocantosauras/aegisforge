"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

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
      // Auto-execute
      await apiClient.executeRequest(request.id, token!).catch(() => {
        // Execution might be async — that's OK
      });
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
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24 }}>
          New Request
        </h1>

        <div className="card" style={{ maxWidth: 700 }}>
          <h2 style={{ marginBottom: 16 }}>Submit a Task</h2>
          <p style={{ fontSize: 14, color: "var(--muted)", marginBottom: 20 }}>
            Describe what you need in natural language. The system will plan and
            execute the task using available agents and tools.
          </p>

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
                {loading ? "Creating..." : "Submit Request"}
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
      </main>
    </div>
  );
}
