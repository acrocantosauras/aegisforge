"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { apiClient } from "./api";

interface AuthContextType {
  token: string | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    fullName: string
  ) => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const stored = localStorage.getItem("aegisforge_token");
    if (stored) {
      setToken(stored);
    }
    setIsLoading(false);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await apiClient.login(email, password);
    localStorage.setItem("aegisforge_token", result.access_token);
    setToken(result.access_token);
  }, []);

  const register = useCallback(
    async (email: string, password: string, fullName: string) => {
      const result = await apiClient.register(email, password, fullName);
      localStorage.setItem("aegisforge_token", result.access_token);
      setToken(result.access_token);
    },
    []
  );

  const logout = useCallback(() => {
    localStorage.removeItem("aegisforge_token");
    setToken(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        token,
        isLoading,
        login,
        register,
        logout,
        isAuthenticated: !!token,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
