/**
 * Shared state components: loading, skeleton, error, empty.
 * The className names are a test contract — pages historically select
 * ".error", ".empty-state" etc. Keep class names stable.
 */

export function LoadingLine({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading-line" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function Skeleton({ w = "100%", h = 14 }: { w?: string | number; h?: string | number }) {
  return <div className="skeleton" style={{ width: w, height: h }} aria-hidden="true" />;
}

export function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="error" role="alert">
      {children}
    </div>
  );
}

export function SuccessBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="success-box" role="status">
      {children}
    </div>
  );
}

export function WarnBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="warn-box" role="status">
      {children}
    </div>
  );
}

export function EmptyState({
  glyph = "◇",
  title,
  children,
}: {
  glyph?: string;
  title?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="glyph" aria-hidden="true">
        {glyph}
      </span>
      {title && <div style={{ fontWeight: 600, color: "var(--fg-secondary)" }}>{title}</div>}
      {children}
    </div>
  );
}
