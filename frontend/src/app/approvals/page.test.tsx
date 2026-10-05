import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApprovalsPage from "@/app/approvals/page";

const { pushMock, listApprovalsMock, approveRequestMock, rejectRequestMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  listApprovalsMock: vi.fn(),
  approveRequestMock: vi.fn(),
  rejectRequestMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
  usePathname: () => "/",
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
    listApprovals: listApprovalsMock,
    approveRequest: approveRequestMock,
    rejectRequest: rejectRequestMock,
  },
}));

const pendingApproval = {
  approval_id: "app-1",
  job_id: "job-1",
  request_id: "req-1",
  action_description: "Send payment to vendor",
  risk_level: "critical",
  status: "pending",
  reason: "",
  created_at: "2026-09-01T00:00:00Z",
};

describe("ApprovalsPage", () => {
  beforeEach(() => {
    listApprovalsMock.mockReset();
    approveRequestMock.mockReset();
    rejectRequestMock.mockReset();
  });

  it("shows an empty state when there are no approvals", async () => {
    listApprovalsMock.mockResolvedValue({ approvals: [], total: 0 });
    render(<ApprovalsPage />);
    await waitFor(() => {
      expect(screen.getByText("Queue clear")).toBeInTheDocument();
    });
  });

  it("renders pending approvals with risk badges and decision buttons", async () => {
    listApprovalsMock.mockResolvedValue({ approvals: [pendingApproval], total: 1 });
    render(<ApprovalsPage />);

    await waitFor(() => {
      expect(screen.getByText("Send payment to vendor")).toBeInTheDocument();
    });
    expect(screen.getByText("critical risk")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("approving calls the API with the provided reason and updates status", async () => {
    approveRequestMock.mockResolvedValue({ approval_id: "app-1", status: "approved" });
    listApprovalsMock.mockResolvedValue({ approvals: [pendingApproval], total: 1 });
    const user = userEvent.setup();
    render(<ApprovalsPage />);

    await user.type(
      await screen.findByPlaceholderText(/Optional context recorded/),
      "Approved by finance"
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(approveRequestMock).toHaveBeenCalledWith("app-1", "Approved by finance", "tok");
    });
    // The row reflects the new status locally
    await waitFor(() => {
      expect(screen.getByText("approved")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("rejecting calls the API and marks the approval rejected", async () => {
    rejectRequestMock.mockResolvedValue({ approval_id: "app-1", status: "rejected" });
    listApprovalsMock.mockResolvedValue({ approvals: [pendingApproval], total: 1 });
    const user = userEvent.setup();
    render(<ApprovalsPage />);

    await user.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(rejectRequestMock).toHaveBeenCalledWith("app-1", "", "tok");
    });
    await waitFor(() => {
      expect(screen.getByText("rejected")).toBeInTheDocument();
    });
  });

  it("surfaces decision errors without losing the approval", async () => {
    approveRequestMock.mockRejectedValue(new Error("Approval already processed"));
    listApprovalsMock.mockResolvedValue({ approvals: [pendingApproval], total: 1 });
    const user = userEvent.setup();
    render(<ApprovalsPage />);

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText("Approval already processed")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});