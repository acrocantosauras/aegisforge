import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "@/app/settings/page";

const { pushMock, logoutMock, authState } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  logoutMock: vi.fn(),
  authState: { token: "" as string },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
  usePathname: () => "/settings",
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
  useAuth: () => ({
    get token() {
      return authState.token;
    },
    isAuthenticated: true,
    isLoading: false,
    logout: logoutMock,
  }),
}));

vi.mock("@/lib/api", () => ({ apiClient: {} }));

/** Build an unsigned-but-well-formed JWT so the page can decode its payload. */
function makeJwt(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

describe("SettingsPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    logoutMock.mockReset();
    authState.token = makeJwt({
      sub: "user-abc-123",
      org: "acme-org",
      roles: ["operator"],
      iat: 1700000000,
      exp: 1800000000,
    });
    window.localStorage.setItem("aegisforge_email", "operator@acme.test");
  });

  it("decodes and displays the session claims from the token", async () => {
    render(<SettingsPage />);

    expect(await screen.findByText("operator@acme.test")).toBeInTheDocument();
    expect(screen.getByText(/user-abc-123/)).toBeInTheDocument();
    expect(screen.getByText("acme-org")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    // Token expiry rendered as a real date, not a placeholder.
    expect(screen.getByText(/2027/)).toBeInTheDocument();
  });

  it("signs out by clearing the session and returning to login", async () => {
    window.localStorage.setItem("aegisforge_token", "stale-token");
    const user = userEvent.setup();

    render(<SettingsPage />);

    const signOut = screen.getAllByRole("button", { name: /Sign out/ })[0];
    await user.click(signOut);

    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(pushMock).toHaveBeenCalledWith("/login");
  });

  it("marks non-editable sections as read-only", async () => {
    render(<SettingsPage />);

    expect((await screen.findAllByText("read-only")).length).toBeGreaterThan(0);
  });
});
