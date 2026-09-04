import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { AuthProvider, useAuth } from "@/lib/auth";

vi.mock("@/lib/api", () => ({
  apiClient: {
    login: vi.fn(),
    register: vi.fn(),
  },
}));

import { apiClient } from "@/lib/api";

const mockedApi = vi.mocked(apiClient);

function renderAuth() {
  return renderHook(() => useAuth(), {
    wrapper: ({ children }) => <AuthProvider>{children}</AuthProvider>,
  });
}

describe("AuthProvider", () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockedApi.login.mockReset();
    mockedApi.register.mockReset();
  });

  it("starts unauthenticated with no stored token", async () => {
    const { result } = renderAuth();
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.token).toBeNull();
  });

  it("restores an existing token from localStorage", async () => {
    window.localStorage.setItem("aegisforge_token", "stored-token");
    const { result } = renderAuth();
    expect(result.current.token).toBe("stored-token");
    expect(result.current.isAuthenticated).toBe(true);
  });

  it("login persists the token and flips auth state", async () => {
    mockedApi.login.mockResolvedValue({
      access_token: "jwt-123",
      token_type: "bearer",
    });
    const { result } = renderAuth();

    await act(async () => {
      await result.current.login("user@corp.com", "password123");
    });

    expect(mockedApi.login).toHaveBeenCalledWith("user@corp.com", "password123");
    expect(result.current.token).toBe("jwt-123");
    expect(result.current.isAuthenticated).toBe(true);
    expect(window.localStorage.getItem("aegisforge_token")).toBe("jwt-123");
  });

  it("login propagates auth failures (bad credentials)", async () => {
    mockedApi.login.mockRejectedValue(new Error("Invalid credentials"));
    const { result } = renderAuth();

    await expect(
      act(async () => {
        await result.current.login("user@corp.com", "wrong-password");
      })
    ).rejects.toThrow("Invalid credentials");

    expect(result.current.isAuthenticated).toBe(false);
    expect(window.localStorage.getItem("aegisforge_token")).toBeNull();
  });

  it("register stores the returned token", async () => {
    mockedApi.register.mockResolvedValue({
      access_token: "reg-token",
      token_type: "bearer",
    });
    const { result } = renderAuth();

    await act(async () => {
      await result.current.register("new@corp.com", "password123", "Jane Doe");
    });

    expect(mockedApi.register).toHaveBeenCalledWith(
      "new@corp.com",
      "password123",
      "Jane Doe"
    );
    expect(result.current.token).toBe("reg-token");
    expect(result.current.isAuthenticated).toBe(true);
  });

  it("logout clears the token", async () => {
    window.localStorage.setItem("aegisforge_token", "jwt-123");
    const { result } = renderAuth();

    act(() => {
      result.current.logout();
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(window.localStorage.getItem("aegisforge_token")).toBeNull();
  });
});