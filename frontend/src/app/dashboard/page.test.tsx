import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DashboardPage from "@/app/dashboard/page";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
  usePathname: () => "/",
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
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
    listAgents: vi.fn().mockResolvedValue({ agents: [], total: 0 }),
  },
}));

import { apiClient } from "@/lib/api";
const mockedApi = vi.mocked(apiClient);

describe("DashboardPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    mockedApi.listRequests.mockReset();
    mockedApi.listApprovals.mockReset();
    mockedApi.listAgents.mockReset();
    mockedApi.listAgents.mockResolvedValue({ agents: [], total: 0 });
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

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Research AI agents")).toBeInTheDocument();
    });
    expect(screen.getByText("Draft a report")).toBeInTheDocument();
    expect(screen.getByText("Needs Approval")).toBeInTheDocument();
    expect(screen.getByText("Send email to external party")).toBeInTheDocument();

    // Stats are derived from the tenant-scoped request list, never from the
    // process-global metrics endpoint: 2 requests, 1 of 1 finished workflows
    // completed, 1 still in flight.
    const stats = Array.from(
      document.querySelectorAll(".stat-grid .stat-card")
    ).map((card) => ({
      label: card.querySelector(".label")?.textContent,
      value: card.querySelector(".value")?.textContent,
    }));
    expect(stats).toEqual([
      { label: "Requests", value: "2" },
      { label: "Success Rate", value: "100%" },
      { label: "In Progress", value: "1" },
      { label: "Pending Approvals", value: "1" },
    ]);
  });

  it("handles API failures gracefully and still renders the empty dashboard", async () => {
    mockedApi.listRequests.mockRejectedValue(new Error("boom"));
    mockedApi.listApprovals.mockRejectedValue(new Error("boom"));

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("No executions yet")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Could not reach the control plane API.")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument();
  });

  it("reloads the dashboard when Retry is pressed", async () => {
    mockedApi.listRequests.mockRejectedValueOnce(new Error("boom"));
    mockedApi.listApprovals.mockResolvedValue({ approvals: [], total: 0 });
    const user = userEvent.setup();

    render(<DashboardPage />);

    const retry = await screen.findByRole("button", { name: /Retry/ });
    mockedApi.listRequests.mockResolvedValue([
      { id: "req-9", intent: "Recovered", status: "completed", created_at: "now" },
    ]);
    await user.click(retry);

    expect(await screen.findByText("Recovered")).toBeInTheDocument();
    expect(mockedApi.listRequests).toHaveBeenCalledTimes(2);
  });

  it("navigates to the request detail when a row is clicked", async () => {
    mockedApi.listRequests.mockResolvedValue([
      { id: "req-42", intent: "Click me", status: "completed", created_at: "now" },
    ]);
    mockedApi.listApprovals.mockResolvedValue({ approvals: [], total: 0 });

    render(<DashboardPage />);

    const row = await screen.findByText("Click me");
    row.click();
    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/requests/req-42");
    });
  });
});