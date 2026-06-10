"use client";

import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, login as apiLogin, signup as apiSignup, refresh as apiRefresh, getProfile, type AuthState } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type DashboardUser = components["schemas"]["DashboardUserResponse"];

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, organizationName: string) => Promise<void>;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

const STORAGE_KEY_REFRESH = "mg_refresh_token";

function getStoredRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return sessionStorage.getItem(STORAGE_KEY_REFRESH);
  } catch {
    return null;
  }
}

function setStoredRefreshToken(token: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (token) {
      sessionStorage.setItem(STORAGE_KEY_REFRESH, token);
    } else {
      sessionStorage.removeItem(STORAGE_KEY_REFRESH);
    }
  } catch {
    // sessionStorage may be unavailable in some contexts
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState<string | null>(getStoredRefreshToken);
  const [user, setUser] = useState<DashboardUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshPromise = useRef<Promise<void> | null>(null);

  // When refresh token changes, persist it
  useEffect(() => {
    setStoredRefreshToken(refreshToken);
  }, [refreshToken]);

  // API client token provider
  useEffect(() => {
    api.setTokenProvider(() => accessToken);
  }, [accessToken]);

  // On unauthorized, force logout
  useEffect(() => {
    api.setOnUnauthorized(() => {
      logout();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Try to restore session on mount
  useEffect(() => {
    if (refreshToken) {
      doRefresh(refreshToken).catch(() => {
        // Refresh failed — clear everything
        setAccessToken(null);
        setRefreshToken(null);
        setUser(null);
      });
    } else {
      setIsLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doRefresh = useCallback(async (token: string) => {
    // Deduplicate concurrent refresh attempts
    if (!refreshPromise.current) {
      refreshPromise.current = (async () => {
        const res = await apiRefresh({ refresh_token: token });
        setAccessToken(res.access_token);
        setRefreshToken(res.refresh_token);
        const profile = await getProfile();
        setUser(profile);
      })().finally(() => {
        refreshPromise.current = null;
      });
    }
    return refreshPromise.current;
  }, []);

  const loginFn = useCallback(async (email: string, password: string) => {
    const res = await apiLogin({ email, password });
    setAccessToken(res.access_token);
    setRefreshToken(res.refresh_token);
    const profile = await getProfile();
    setUser(profile);
  }, []);

  const signupFn = useCallback(
    async (email: string, password: string, organizationName: string) => {
      const res = await apiSignup({ email, password, organization_name: organizationName });
      setAccessToken(res.access_token);
      setRefreshToken(res.refresh_token);
      const profile = await getProfile();
      setUser(profile);
    },
    [],
  );

  const logout = useCallback(() => {
    setAccessToken(null);
    setRefreshToken(null);
    setUser(null);
    setStoredRefreshToken(null);
  }, []);

  const value = useMemo(
    () => ({
      accessToken,
      refreshToken,
      user,
      isLoading,
      login: loginFn,
      signup: signupFn,
      logout,
    }),
    [accessToken, refreshToken, user, isLoading, loginFn, signupFn, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
