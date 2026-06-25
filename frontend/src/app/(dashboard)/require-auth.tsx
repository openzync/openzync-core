"use client";

import { useEffect, useState, useRef } from "react";
import { AuthLoadingScreen } from "@/components/shared/auth-loading-screen";

const MIN_DISPLAY_MS = 200;
const UNAUTHORIZED_PAUSE_MS = 500;

function isTokenExpired(): boolean {
  const token = sessionStorage.getItem("mg_access_token");
  if (!token) return true;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"loading" | "authorized" | "denied">("loading");
  const redirecting = useRef(false);

  useEffect(() => {
    const token = sessionStorage.getItem("mg_access_token");
    const expired = !token || isTokenExpired();

    if (expired) {
      // Clean up stale tokens
      sessionStorage.removeItem("mg_access_token");
      sessionStorage.removeItem("mg_refresh_token");

      // Show the loading screen briefly for a polished feel, then redirect
      const timer = setTimeout(() => {
        if (!redirecting.current) {
          redirecting.current = true;
          window.location.href = "/login?reason=not-signed-in";
        }
      }, UNAUTHORIZED_PAUSE_MS);

      setState("denied");
      return () => clearTimeout(timer);
    }

    // Valid token — enforce minimum display time so the loading screen
    // doesn't flicker on fast checks (sessionStorage is sync).
    const timer = setTimeout(() => setState("authorized"), MIN_DISPLAY_MS);
    return () => clearTimeout(timer);
  }, []);

  if (state !== "authorized") {
    return <AuthLoadingScreen />;
  }

  return <>{children}</>;
}
