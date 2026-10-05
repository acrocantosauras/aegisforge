import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AgentsPage from "@/app/agents/page";

const { listAgentsMock } = vi.hoisted(() => ({ listAgentsMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
  usePathname: () => "/agents",
}));

vi.mock("@/components/shell/AppShell", () => ({
  default: ({
    children,
    actions,
  }: {
    children: React.ReactNode;
    actions?: React.ReactNode;
  }) => (
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
  apiClient: { listAgents: listAgentsMock },
}));

describe("AgentsPage", () => {
  beforeEach(() => {
    listAgentsMock.mockReset();
  });

  it("renders the registry returned by the API", async () => {
    listAgentsMock.mockResolvedValue({
      agents: [
        {
          agent_type: "research",
          name: "Research Agent",
          description: "Investigates a request using approved tools",
          capabilities: ["tool-use", "knowledge.search"],
        },
      ],
      total: 1,
    });

    render(<AgentsPage />);

    expect(await screen.findByText("Research Agent")).toBeInTheDocument();
    expect(
      screen.getByText("Investigates a request using approved tools")
    ).toBeInTheDocument();
    expect(screen.getByText("knowledge.search")).toBeInTheDocument();
    expect(listAgentsMock).toHaveBeenCalledWith("tok");
  });

  it("shows an empty state when the catalog has no agents", async () => {
    listAgentsMock.mockResolvedValue({ agents: [], total: 0 });

    render(<AgentsPage />);

    expect(await screen.findByText("No agents registered")).toBeInTheDocument();
  });

  it("surfaces a load failure and recovers through Retry", async () => {
    listAgentsMock.mockRejectedValueOnce(new Error("registry unreachable"));
    const user = userEvent.setup();

    render(<AgentsPage />);

    expect(await screen.findByText("registry unreachable")).toBeInTheDocument();

    listAgentsMock.mockResolvedValue({ agents: [], total: 0 });
    await user.click(screen.getByRole("button", { name: /Retry/ }));

    expect(await screen.findByText("No agents registered")).toBeInTheDocument();
    expect(listAgentsMock).toHaveBeenCalledTimes(2);
  });
});
