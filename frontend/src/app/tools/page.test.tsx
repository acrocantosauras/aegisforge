import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ToolsPage from "@/app/tools/page";

const { listToolsMock, listMCPServersMock } = vi.hoisted(() => ({
  listToolsMock: vi.fn(),
  listMCPServersMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
  usePathname: () => "/tools",
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
  apiClient: {
    listTools: listToolsMock,
    listMCPServers: listMCPServersMock,
  },
}));

describe("ToolsPage", () => {
  beforeEach(() => {
    listToolsMock.mockReset();
    listMCPServersMock.mockReset();
    listToolsMock.mockResolvedValue({
      tools: [
        {
          name: "knowledge.search",
          description: "Search tenant-scoped knowledge",
          version: "1.2.0",
          permission_requirements: ["knowledge.read"],
          timeout_seconds: 15,
          requires_approval: false,
          risk_level: "low",
          read_only: true,
          external_side_effect: false,
          data_sensitivity: "internal",
        },
      ],
      total: 1,
    });
    listMCPServersMock.mockResolvedValue({
      servers: [
        {
          server_id: "mcp.example",
          name: "example",
          description: "Example MCP server",
          version: "0.1.0",
          transport: "stdio",
          enabled: true,
          allowed_tools: ["lookup"],
          risk_level: "medium",
          read_only_default: true,
          timeout_seconds: 30,
          health: {},
          tool_count: 1,
        },
      ],
      total: 1,
    });
  });

  it("renders platform tools with risk and permission metadata", async () => {
    render(<ToolsPage />);

    expect(await screen.findByText("knowledge.search")).toBeInTheDocument();
    expect(screen.getByText("low risk")).toBeInTheDocument();
    expect(screen.getByText("timeout 15s")).toBeInTheDocument();
    expect(screen.getByText(/permissions: knowledge\.read/)).toBeInTheDocument();
  });

  it("switches to the MCP tab and lists configured servers", async () => {
    const user = userEvent.setup();
    render(<ToolsPage />);

    await screen.findByText("knowledge.search");
    await user.click(screen.getByRole("button", { name: /MCP servers \(1\)/ }));

    expect(await screen.findByText("mcp.example")).toBeInTheDocument();
    expect(screen.getByText("stdio")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
  });

  it("shows an error with Retry when both catalogs are unreachable", async () => {
    listToolsMock.mockRejectedValue(new Error("catalog down"));
    listMCPServersMock.mockRejectedValue(new Error("catalog down"));
    const user = userEvent.setup();

    render(<ToolsPage />);

    expect(
      await screen.findByText("Tool and MCP catalogs are unavailable.")
    ).toBeInTheDocument();

    listToolsMock.mockResolvedValue({ tools: [], total: 0 });
    listMCPServersMock.mockResolvedValue({ servers: [], total: 0 });
    await user.click(screen.getByRole("button", { name: /Retry/ }));

    expect(await screen.findByText("No tools registered")).toBeInTheDocument();
  });
});
