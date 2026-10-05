import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import LandingPage from "@/app/page";

const { authState } = vi.hoisted(() => ({
  authState: { isAuthenticated: false, isLoading: false },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
  usePathname: () => "/",
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    get isAuthenticated() {
      return authState.isAuthenticated;
    },
    isLoading: authState.isLoading,
  }),
}));

vi.mock("@/lib/api", () => ({ apiClient: {} }));
vi.mock("@/components/shell/AppShell", () => ({
  BrandMark: () => <span>brand</span>,
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  useSystemStatus: () => null,
  SystemPill: () => null,
}));

describe("LandingPage", () => {
  it("explains the product and links anonymous visitors to sign in", () => {
    authState.isAuthenticated = false;
    render(<LandingPage />);

    expect(
      screen.getByRole("heading", { name: /AI orchestration for complex work/i })
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Multi-Agent/).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /Launch AegisForge/ }).length).toBeGreaterThan(0);
    // Anonymous visitors land on the login page, not the workspace.
    expect(screen.getAllByRole("link", { name: /Launch AegisForge/ })[0]).toHaveAttribute(
      "href",
      "/login"
    );
  });

  it("sends authenticated users straight to the workspace", () => {
    authState.isAuthenticated = true;
    render(<LandingPage />);

    const cta = screen.getAllByRole("link", { name: /Launch AegisForge/ })[0];
    expect(cta).toHaveAttribute("href", "/dashboard");
  });

  it("presents the execution pipeline stages", () => {
    authState.isAuthenticated = false;
    render(<LandingPage />);

    for (const stage of ["REQUEST", "PLANNER", "AGENTS", "SYNTHESIS", "EVALUATION"]) {
      expect(screen.getAllByText(stage).length).toBeGreaterThan(0);
    }
  });
});
