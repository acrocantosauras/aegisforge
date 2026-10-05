import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { pushMock, systemStatusMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  systemStatusMock: vi.fn(),
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
  useAuth: () => ({ token: "tok", isAuthenticated: true, isLoading: false }),
}));

vi.mock("@/lib/api", () => ({
  apiClient: {
    systemStatus: systemStatusMock,
    listAuditEvents: vi.fn().mockResolvedValue({ events: [] }),
  },
}));

import SystemPage from "@/app/system/page";

const HEALTHY = {
  status: "healthy",
  components: {
    api: "ok",
    database: "ok",
    redis: "ok",
    workers: {
      status: "ok",
      active: 2,
      healthy: 2,
      list: [
        { worker_id: "worker-1", last_heartbeat_age_seconds: 1.234, healthy: true },
        { worker_id: "worker-2", last_heartbeat_age_seconds: 2.5, healthy: true },
      ],
    },
    queue: { status: "ok", depth: 3 },
  },
};

const DEGRADED = {
  status: "degraded",
  components: {
    api: "ok",
    database: "ok",
    redis: "unavailable",
    workers: {
      status: "none_active",
      active: 0,
      healthy: 0,
      list: [] as {
        worker_id: string;
        last_heartbeat_age_seconds: number;
        healthy: boolean;
      }[],
    },
    queue: { status: "ok", depth: 0 },
  },
};

describe("SystemPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    systemStatusMock.mockReset();
  });

  it("renders component health cards and the overall badge", async () => {
    systemStatusMock.mockResolvedValue(HEALTHY);
    render(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    });

    expect(screen.getByText("API")).toBeInTheDocument();
    expect(screen.getByText("Database")).toBeInTheDocument();
    expect(screen.getByText("Redis")).toBeInTheDocument();
    expect(screen.getAllByText("healthy")).toHaveLength(7);
    expect(screen.getByText(/2\/2/)).toBeInTheDocument();
    expect(screen.getByText(/3 · pending jobs/)).toBeInTheDocument(); // queue depth
  });

  it("renders the worker registry with heartbeat ages and status badges", async () => {
    systemStatusMock.mockResolvedValue(HEALTHY);
    render(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText("worker-1")).toBeInTheDocument();
    });

    expect(screen.getByText("worker-2")).toBeInTheDocument();
    expect(screen.getByText("1.2s ago")).toBeInTheDocument();
    expect(screen.getByText("2.5s ago")).toBeInTheDocument();
    expect(screen.getAllByText("healthy")).toHaveLength(7);
  });

  it("shows an empty state when no workers have heartbeated", async () => {
    systemStatusMock.mockResolvedValue(DEGRADED);
    render(<SystemPage />);

    await waitFor(() => {
      expect(
        screen.getByText(/No workers have heartbeated recently/)
      ).toBeInTheDocument();
    });
  });

  it("shows a DEGRADED badge when a dependency is down", async () => {
    systemStatusMock.mockResolvedValue(DEGRADED);
    render(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText("DEGRADED")).toBeInTheDocument();
    });

    expect(screen.getAllByText("unavailable")).toHaveLength(2); // Redis + workers
    expect(screen.getAllByText("healthy")).toHaveLength(3); // API + database + queue
  });

  it("reloads status when Refresh is clicked", async () => {
    systemStatusMock.mockResolvedValue(HEALTHY);
    const user = userEvent.setup();
    render(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    });
    expect(systemStatusMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /Refresh/ }));

    await waitFor(() => {
      expect(systemStatusMock).toHaveBeenCalledTimes(2);
    });
  });

  it("surfaces a load failure without fabricating status", async () => {
    systemStatusMock.mockRejectedValue(new Error("system status unavailable"));
    const user = userEvent.setup();
    render(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText("system status unavailable")).toBeInTheDocument();
    });

    expect(screen.queryByText("HEALTHY")).not.toBeInTheDocument();
    // No status means no data: the page must offer recovery instead of
    // showing an indefinite loading indicator.
    expect(screen.queryByText(/Probing components/)).not.toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /Retry/ });
    expect(retry).toBeInTheDocument();

    systemStatusMock.mockResolvedValue(HEALTHY);
    await user.click(retry);

    await waitFor(() => {
      expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    });
  });
});
