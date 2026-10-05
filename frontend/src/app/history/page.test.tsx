import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { pushMock, getRequestHistoryMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  getRequestHistoryMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/components/shell/AppShell", () => ({
  default: ({ children, actions }: { children: React.ReactNode; actions?: React.ReactNode }) => (
    <div>
      {actions}
      {children}
    </div>
  ),
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ token: "tok", isAuthenticated: true, isLoading: false }),
}));

vi.mock("@/lib/api", () => ({
  apiClient: {
    getRequestHistory: getRequestHistoryMock,
  },
}));

import HistoryPage from "@/app/history/page";

const HISTORY = [
  {
    id: "req-done",
    intent: "Conduct enterprise knowledge research on policy",
    status: "completed",
    created_at: "2026-09-28T01:00:00Z",
  },
  {
    id: "req-fail",
    intent: "Analyze a failing vendor feed",
    status: "failed",
    created_at: "2026-09-28T02:00:00Z",
  },
  {
    id: "req-run",
    intent: "Summarize quarterly exposure",
    status: "executing",
    created_at: "2026-09-28T03:00:00Z",
  },
];

describe("HistoryPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    getRequestHistoryMock.mockReset();
  });

  it("renders history rows with status badges and timestamps", async () => {
    getRequestHistoryMock.mockResolvedValue(HISTORY);
    render(<HistoryPage />);

    await waitFor(() => {
      expect(screen.getByText(/enterprise knowledge research/)).toBeInTheDocument();
    });
    expect(screen.getByText(/failing vendor feed/)).toBeInTheDocument();
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("filters by status", async () => {
    getRequestHistoryMock.mockResolvedValue(HISTORY);
    const user = userEvent.setup();
    render(<HistoryPage />);

    await waitFor(() => {
      expect(screen.getByText(/enterprise knowledge research/)).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /Failed/ }));

    expect(screen.getByText(/failing vendor feed/)).toBeInTheDocument();
    expect(screen.queryByText(/enterprise knowledge research/)).not.toBeInTheDocument();
  });

  it("opens the execution workspace when a row is clicked", async () => {
    getRequestHistoryMock.mockResolvedValue(HISTORY);
    const user = userEvent.setup();
    render(<HistoryPage />);

    await waitFor(() => {
      expect(screen.getByText(/failing vendor feed/)).toBeInTheDocument();
    });

    await user.click(screen.getByText(/failing vendor feed/));
    expect(pushMock).toHaveBeenCalledWith("/requests/req-fail");
  });

  it("shows an empty state when there is no history", async () => {
    getRequestHistoryMock.mockResolvedValue([]);
    render(<HistoryPage />);

    await waitFor(() => {
      expect(screen.getByText(/No executions yet/)).toBeInTheDocument();
    });
  });
});
