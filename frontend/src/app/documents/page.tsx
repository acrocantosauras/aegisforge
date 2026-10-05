"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/shell/AppShell";
import { LoadingLine, EmptyState, ErrorBox } from "@/components/ui/states";
import { ConfirmDialog } from "@/components/ui/Modal";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { formatRelativeTime } from "@/lib/status";

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
  const [loadError, setLoadError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Document | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

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
        setLoadError("");
      } catch (err: unknown) {
        setLoadError(
          err instanceof Error ? err.message : "Failed to load the knowledge index"
        );
      } finally {
        setLoading(false);
      }
    }

    fetchDocuments();
  }, [token, reloadKey]);

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
        `Uploaded "${result.title}" — ${result.chunk_count} chunks indexed.`
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

  const confirmDelete = async () => {
    if (!token || !deleteTarget) return;
    setDeleting(true);
    setActionError("");
    try {
      await apiClient.deleteDocument(deleteTarget.id, token);
      setDocuments((prev) => prev.filter((d) => d.id !== deleteTarget.id));
      setDeleteTarget(null);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  };

  if (isLoading || !isAuthenticated) return null;

  const totalChunks = documents.reduce((acc, d) => acc + d.chunk_count, 0);

  return (
    <AppShell
      title="Knowledge"
      subtitle="Your enterprise corpus. Documents are chunked, embedded, and tenant-isolated in pgvector — RAG agents retrieve from this index with citations."
      actions={
        <div className="stat-grid" style={{ marginBottom: 0, gap: 12 }}>
          <div className="stat-card" style={{ padding: "8px 16px" }}>
            <div className="label">Documents</div>
            <div className="value" style={{ fontSize: "1.1rem" }}>
              {documents.length}
            </div>
          </div>
          <div className="stat-card" style={{ padding: "8px 16px" }}>
            <div className="label">Chunks</div>
            <div className="value" style={{ fontSize: "1.1rem" }}>
              {totalChunks}
            </div>
          </div>
        </div>
      }
    >
      {/* Upload */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-header">
          <h2>Ingest Document</h2>
          <span className="t-caption mono">PDF · DOCX · TXT · MD · max 10MB</span>
        </div>

        {uploadError && (
          <div className="error" role="alert" style={{ marginBottom: 12 }}>
            {uploadError}
          </div>
        )}

        {uploadSuccess && (
          <div className="success-box">{uploadSuccess}</div>
        )}

        {actionError && <ErrorBox>{actionError}</ErrorBox>}

        <div className="row wrap" style={{ alignItems: "flex-end", gap: 12 }}>
          <div className="form-group grow" style={{ marginBottom: 0 }}>
            <label htmlFor="uploadTitle">Title (optional)</label>
            <input
              id="uploadTitle"
              type="text"
              value={uploadTitle}
              onChange={(e) => setUploadTitle(e.target.value)}
              placeholder="Defaults to the file name…"
            />
          </div>
          <div className="form-group" style={{ marginBottom: 0, minWidth: 220 }}>
            <label htmlFor="documentFile">File</label>
            <input
              id="documentFile"
              ref={fileInputRef}
              type="file"
              accept=".pdf,.docx,.txt,.md"
            />
          </div>
          <button className="primary" onClick={handleUpload} disabled={uploading}>
            {uploading ? "Ingesting…" : "Upload & Ingest"}
          </button>
        </div>
      </div>

      {/* Library */}
      {loadError ? (
        <>
          <ErrorBox>{loadError}</ErrorBox>
          <button className="secondary" onClick={() => setReloadKey((k) => k + 1)}>
            Retry
          </button>
        </>
      ) : loading ? (
        <LoadingLine label="Loading knowledge index…" />
      ) : documents.length === 0 ? (
        <div className="card">
          <EmptyState glyph="▤" title="No documents yet">
            <p>
              Upload your first document above. Once ingested, RAG agents cite
              it when answering relevant requests.
            </p>
          </EmptyState>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Type</th>
                <th>Chunks</th>
                <th>Index Status</th>
                <th>Ingested</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td className="wrap">
                    <strong style={{ fontSize: "var(--text-small)" }}>{doc.title}</strong>
                    <div className="t-caption mono">{doc.id}</div>
                  </td>
                  <td className="mono">{doc.content_type}</td>
                  <td className="mono">{doc.chunk_count}</td>
                  <td>
                    <span
                      className={`badge ${
                        doc.status === "completed" ? "success" : "warning"
                      }`}
                    >
                      {doc.status === "completed" ? "indexed" : doc.status}
                    </span>
                  </td>
                  <td className="mono">{formatRelativeTime(doc.created_at)}</td>
                  <td>
                    <button
                      className="danger compact"
                      onClick={() => setDeleteTarget(doc)}
                      aria-label={`Delete ${doc.title}`}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete document"
        danger
        busy={deleting}
        confirmLabel="Delete document"
        onConfirm={confirmDelete}
        onCancel={() => {
          setDeleteTarget(null);
          setActionError("");
        }}
        message={
          <>
            Delete <strong>{deleteTarget?.title}</strong> and all its indexed chunks?
            This cannot be undone.
          </>
        }
      />
    </AppShell>
  );
}
