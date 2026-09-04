import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import DashboardPage from "@/app/dashboard/page";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("@/components/Sidebar", () => ({
  default: () => <nav>Sidebar</nav>,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    isAuthenticated: true,
    isLoading: false,
  }),
}));

vi.mock("@/lib/api", () => ({
  apiClient: {
    listRequests: vi.fn(),
    listApprovals: vi.fn(),
    getMetrics: vi.fn(),
  },
}));

import { apiClient } from "@/lib/api";
const mockedApi = vi.mocked(apiClient);

describe("DashboardPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    mockedApi.listRequests.mockReset();
    mockedApi.listApprovals.mockReset();
    mockedApi.getMetrics.mockReset();
  });

  it("renders stats, requests, and pending approvals from the API", async () => {
    mockedApi.listRequests.mockResolvedValue([
      { id: "req-1", intent: "Research AI agents", status: "completed", created_at: "now" },
      { id: "req-2", intent: "Draft a report", status: "waiting_for_approval", created_at: "now" },
    ]);
    mockedApi.listApprovals.mockResolvedValue({
      approvals: [
        {
          approval_id: "app-1",
          job_id: "job-1",
          request_id: "req-1",
          action_description: "Send email to external party",
          risk_level: "high",
          status: "pending",
          reason: "",
          created_at: "now",
        },
      ],
      total: 1,
    });
    mockedApi.getMetrics.mockResolvedValue({
      count: 2,
      avg_workflow_duration_ms: 100,
      total_tool_calls: 5,
      total_retries: 0,
      total_failures: 0,
      success_rate: 1.0,
    });

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Research AI agents")).toBeInTheDocument();
    });
    expect(screen.getByText("Draft a report")).toBeInTheDocument();
    expect(screen.getByText("Needs Approval")).toBeInTheDocument();
    expect(screen.getByText("Send email to external party")).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument(); // success rate
  });

  it("handles API failures gracefully and still renders the empty dashboard", async () => {
    mockedApi.listRequests.mockRejectedValue(new Error("boom"));
    mockedApi.listApprovals.mockRejectedValue(new Error("boom"));
    mockedApi.getMetrics.mockRejectedValue(new Error("boom"));

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("No requests yet. Create your first request.")).toBeInTheDocument();
    });
  });

  it("navigates to the request detail when a row is clicked", async () => {
    mockedApi.listRequests.mockResolvedValue([
      { id: "req-42", intent: "Click me", status: "completed", created_at: "now" },
    ]);
    mockedApi.listApprovals.mockResolvedValue({ approvals: [], total: 0 });
    mockedApi.getMetrics.mockResolvedValue({
      count: 1,
      avg_workflow_duration_ms: 0,
      total_tool_calls: 0,
      total_retries: 0,
      total_failures: 0,
      success_rate: 0,
    });

    render(<DashboardPage />);

    const row = await screen.findByText("Click me");
    row.click();
    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/requests/req-42");
    });
  });
});