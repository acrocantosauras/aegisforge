import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewRequestPage from "@/app/requests/new/page";

const { pushMock, backMock, createRequestMock, executeRequestMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  backMock: vi.fn(),
  createRequestMock: vi.fn(),
  executeRequestMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: backMock }),
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
    createRequest: createRequestMock,
    executeRequest: executeRequestMock,
  },
}));

describe("NewRequestPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    backMock.mockReset();
    createRequestMock.mockReset();
    executeRequestMock.mockReset();
  });

  it("disables submit until the intent is long enough", async () => {
    const user = userEvent.setup();
    render(<NewRequestPage />);

    const submit = screen.getByRole("button", { name: "Submit Request" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Task Description"), "short");
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Task Description"), "a much longer task description");
    expect(submit).toBeEnabled();
  });

  it("creates the request, auto-executes, and navigates to the detail page", async () => {
    createRequestMock.mockResolvedValue({ id: "req-new-1", status: "created" });
    executeRequestMock.mockResolvedValue({ status: "queued" });
    const user = userEvent.setup();
    render(<NewRequestPage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "Research latest AI frameworks and summarize"
    );
    await user.click(screen.getByRole("button", { name: "Submit Request" }));

    await waitFor(() => {
      expect(createRequestMock).toHaveBeenCalledWith(
        "Research latest AI frameworks and summarize",
        {},
        "tok"
      );
      expect(executeRequestMock).toHaveBeenCalledWith("req-new-1", "tok");
      expect(pushMock).toHaveBeenCalledWith("/requests/req-new-1");
    });
  });

  it("still navigates when auto-execution fails", async () => {
    createRequestMock.mockResolvedValue({ id: "req-new-2", status: "created" });
    executeRequestMock.mockRejectedValue(new Error("worker busy"));
    const user = userEvent.setup();
    render(<NewRequestPage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "Analyze market trends for Q3 planning"
    );
    await user.click(screen.getByRole("button", { name: "Submit Request" }));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/requests/req-new-2");
    });
  });

  it("shows the error when request creation fails", async () => {
    createRequestMock.mockRejectedValue(new Error("Request validation failed"));
    const user = userEvent.setup();
    render(<NewRequestPage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "A task that will fail to be created"
    );
    await user.click(screen.getByRole("button", { name: "Submit Request" }));

    await waitFor(() => {
      expect(screen.getByText("Request validation failed")).toBeInTheDocument();
    });
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("cancel goes back without submitting", async () => {
    const user = userEvent.setup();
    render(<NewRequestPage />);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(backMock).toHaveBeenCalled();
    expect(createRequestMock).not.toHaveBeenCalled();
  });
});