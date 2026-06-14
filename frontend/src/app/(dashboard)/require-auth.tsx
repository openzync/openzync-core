"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";

function isTokenExpired(): boolean {
  const token = sessionStorage.getItem("mg_access_token");
  if (!token) return true;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    // exp is in seconds since epoch, Date.now() is in milliseconds
    return payload.exp * 1000 < Date.now();
  } catch {
    return true; // Malformed token — treat as expired
  }
}

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const redirected = useRef(false);

  useEffect(() => {
    const token = sessionStorage.getItem("mg_access_token");
    if ((!token || isTokenExpired()) && !redirected.current) {
      redirected.current = true;
      // Clean up stale tokens so they don't linger
      sessionStorage.removeItem("mg_access_token");
      sessionStorage.removeItem("mg_refresh_token");
      router.replace("/login?reason=not-signed-in");
    }
  }, [router]);

  return <>{children}</>;
}
