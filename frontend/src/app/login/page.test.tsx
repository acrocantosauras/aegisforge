import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LoginPage from "@/app/login/page";

const { pushMock, loginMock, registerMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  loginMock: vi.fn(),
  registerMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    login: loginMock,
    register: registerMock,
    token: null,
    isLoading: false,
  }),
}));

describe("LoginPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    loginMock.mockReset();
    registerMock.mockReset();
  });

  it("renders the sign-in form by default", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign In" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Full Name")).not.toBeInTheDocument();
  });

  it("switches to registration and shows the full-name field", async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: "Register" }));
    expect(screen.getByLabelText("Full Name")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create Account" })).toBeInTheDocument();
  });

  it("submits credentials and navigates to the dashboard", async () => {
    loginMock.mockResolvedValue({ access_token: "t" });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText("Email"), "user@corp.com");
    await user.type(screen.getByLabelText("Password"), "password123");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    await waitFor(() => {
      expect(loginMock).toHaveBeenCalledWith("user@corp.com", "password123");
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("shows an error message on authentication failure", async () => {
    loginMock.mockRejectedValue(new Error("Invalid credentials"));
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText("Email"), "user@corp.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    await waitFor(() => {
      expect(screen.getByText("Invalid credentials")).toBeInTheDocument();
    });
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("registers a new account and navigates", async () => {
    registerMock.mockResolvedValue({ access_token: "t" });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByRole("button", { name: "Register" }));
    await user.type(screen.getByLabelText("Full Name"), "Jane Doe");
    await user.type(screen.getByLabelText("Email"), "jane@corp.com");
    await user.type(screen.getByLabelText("Password"), "password123");
    await user.click(screen.getByRole("button", { name: "Create Account" }));

    await waitFor(() => {
      expect(registerMock).toHaveBeenCalledWith("jane@corp.com", "password123", "Jane Doe");
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
  });
});