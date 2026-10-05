import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ExecutePage from "@/app/execute/page";

const { pushMock, backMock, createRequestMock, executeRequestMock, executeAsyncMock } =
  vi.hoisted(() => ({
    pushMock: vi.fn(),
    backMock: vi.fn(),
    createRequestMock: vi.fn(),
    executeRequestMock: vi.fn(),
    executeAsyncMock: vi.fn(),
  }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: backMock }),
  usePathname: () => "/execute",
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
    createRequest: createRequestMock,
    executeRequest: executeRequestMock,
    executeRequestAsync: executeAsyncMock,
  },
}));

describe("ExecutePage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    backMock.mockReset();
    createRequestMock.mockReset();
    executeRequestMock.mockReset();
    executeAsyncMock.mockReset();
    // Default: async submission succeeds.
    executeAsyncMock.mockResolvedValue({ status: "queued" });
  });

  it("disables submit until the intent is long enough", async () => {
    const user = userEvent.setup();
    render(<ExecutePage />);

    const submit = screen.getByRole("button", { name: "Submit & Execute" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Task Description"), "short");
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Task Description"), "a much longer task description");
    expect(submit).toBeEnabled();
  });

  it("creates the request, submits via async queue, and navigates to the detail page", async () => {
    createRequestMock.mockResolvedValue({ id: "req-new-1", status: "created" });
    const user = userEvent.setup();
    render(<ExecutePage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "Research latest AI frameworks and summarize"
    );
    await user.click(screen.getByRole("button", { name: "Submit & Execute" }));

    await waitFor(() => {
      expect(createRequestMock).toHaveBeenCalledWith(
        "Research latest AI frameworks and summarize",
        {},
        "tok"
      );
      expect(executeAsyncMock).toHaveBeenCalledWith("req-new-1", "tok");
      expect(pushMock).toHaveBeenCalledWith("/requests/req-new-1");
    });
  });

  it("falls back to sync execution when the async queue rejects", async () => {
    createRequestMock.mockResolvedValue({ id: "req-new-3", status: "created" });
    executeAsyncMock.mockRejectedValue(new Error("queue unavailable"));
    executeRequestMock.mockResolvedValue({ status: "completed" });
    const user = userEvent.setup();
    render(<ExecutePage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "Research incident response policy and summarize"
    );
    await user.click(screen.getByRole("button", { name: "Submit & Execute" }));

    await waitFor(() => {
      expect(executeRequestMock).toHaveBeenCalledWith("req-new-3", "tok");
      expect(pushMock).toHaveBeenCalledWith("/requests/req-new-3");
    });
  });

  it("still navigates when auto-execution fails", async () => {
    createRequestMock.mockResolvedValue({ id: "req-new-2", status: "created" });
    executeAsyncMock.mockRejectedValue(new Error("queue unavailable"));
    executeRequestMock.mockRejectedValue(new Error("worker busy"));
    const user = userEvent.setup();
    render(<ExecutePage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "Analyze market trends for Q3 planning"
    );
    await user.click(screen.getByRole("button", { name: "Submit & Execute" }));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/requests/req-new-2");
    });
  });

  it("shows the error when request creation fails", async () => {
    createRequestMock.mockRejectedValue(new Error("Request validation failed"));
    const user = userEvent.setup();
    render(<ExecutePage />);

    await user.type(
      screen.getByLabelText("Task Description"),
      "A task that will fail to be created"
    );
    await user.click(screen.getByRole("button", { name: "Submit & Execute" }));

    await waitFor(() => {
      expect(screen.getByText("Request validation failed")).toBeInTheDocument();
    });
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("cancel goes back without submitting", async () => {
    const user = userEvent.setup();
    render(<ExecutePage />);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(backMock).toHaveBeenCalled();
    expect(createRequestMock).not.toHaveBeenCalled();
  });
});
