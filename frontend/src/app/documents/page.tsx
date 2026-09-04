"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";

interface Document {
  id: string;
  title: string;
  content_type: string;
  chunk_count: number;
  status: string;
  created_at: string;
}

export default function DocumentsPage() {
  const { token, isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadError, setUploadError] = useState("");
  const [uploadSuccess, setUploadSuccess] = useState("");

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!token) return;

    async function fetchDocuments() {
      try {
        const result = await apiClient.listDocuments(token!);
        setDocuments(result.documents || []);
      } catch {
        // Endpoint may not exist yet
      } finally {
        setLoading(false);
      }
    }

    fetchDocuments();
  }, [token]);

  const handleUpload = async () => {
    const file = fileInputRef.current?.files?.[0];
    if (!file || !token) return;

    setUploading(true);
    setUploadError("");
    setUploadSuccess("");

    try {
      const result = await apiClient.uploadDocument(
        file,
        uploadTitle || file.name,
        token
      );
      setUploadSuccess(
        `Uploaded "${result.title}" — ${result.chunk_count} chunks created.`
      );
      setUploadTitle("");
      if (fileInputRef.current) fileInputRef.current.value = "";

      // Refresh the list
      const updated = await apiClient.listDocuments(token);
      setDocuments(updated.documents || []);
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!token || !confirm("Delete this document and all its chunks?")) return;
    try {
      await apiClient.deleteDocument(id, token);
      setDocuments((prev) => prev.filter((d) => d.id !== id));
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Delete failed");
    }
  };

  if (isLoading || !isAuthenticated) return null;

  return (
    <div className="layout">
      <Sidebar />
      <main className="main-content">
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24 }}>
          Documents & Knowledge Base
        </h1>

        {/* Upload Section */}
        <div className="card" style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, marginBottom: 12 }}>Upload Document</h2>
          <p style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
            Supported formats: PDF, DOCX, TXT, Markdown. Max 10MB.
          </p>

          {uploadError && (
            <div
              className="error"
              style={{ marginBottom: 12, padding: 10, background: "#f8d7da", borderRadius: 6 }}
            >
              {uploadError}
            </div>
          )}

          {uploadSuccess && (
            <div
              style={{
                marginBottom: 12,
                padding: 10,
                background: "#d1e7dd",
                color: "#0f5132",
                borderRadius: 6,
                fontSize: 13,
              }}
            >
              {uploadSuccess}
            </div>
          )}

          <div className="form-group">
            <label>Title (optional)</label>
            <input
              type="text"
              value={uploadTitle}
              onChange={(e) => setUploadTitle(e.target.value)}
              placeholder="Document title..."
            />
          </div>

          <div className="form-group">
            <input ref={fileInputRef} type="file" accept=".pdf,.docx,.txt,.md" />
          </div>

          <button
            className="primary"
            onClick={handleUpload}
            disabled={uploading}
          >
            {uploading ? "Uploading & Ingesting..." : "Upload & Ingest"}
          </button>
        </div>

        {/* Document List */}
        <div className="card">
          <h2 style={{ fontSize: 16, marginBottom: 12 }}>
            Uploaded Documents ({documents.length})
          </h2>

          {loading ? (
            <p style={{ color: "var(--muted)" }}>Loading documents...</p>
          ) : documents.length === 0 ? (
            <div className="empty-state">
              No documents uploaded yet. Upload your first document above.
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Type</th>
                  <th>Chunks</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>
                      <div>
                        <strong>{doc.title}</strong>
                        <div style={{ fontSize: 11, color: "var(--muted)", fontFamily: "monospace" }}>
                          {doc.id}
                        </div>
                      </div>
                    </td>
                    <td>{doc.content_type}</td>
                    <td>{doc.chunk_count}</td>
                    <td>
                      <span
                        className={`badge ${
                          doc.status === "completed" ? "success" : "warning"
                        }`}
                      >
                        {doc.status}
                      </span>
                    </td>
                    <td>
                      <button
                        className="danger"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        onClick={() => handleDelete(doc.id)}
                      >
                        Delete
                      </button>
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
