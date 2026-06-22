"use client";

import { useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Eye, EyeOff, Loader2, LogIn } from "lucide-react";
import { API_BASE } from "@/lib/api-client";

function LoginNotice() {
  const searchParams = useSearchParams();
  const reason = searchParams.get("reason");
  if (reason !== "not-signed-in") return null;
  return (
    <div className="mb-4 rounded-lg border border-accent-300/20 bg-accent-300/5 px-4 py-3">
      <div className="flex items-center gap-2 mb-0.5">
        <LogIn size={16} className="text-accent-300" />
        <p className="font-medium text-accent-300 text-sm">Sign in required</p>
      </div>
      <p className="text-surface-400 text-xs ml-6">
        Please sign in to access the dashboard.
      </p>
    </div>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);

    try {
      const res = await fetch(`${API_BASE}/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail ?? "Invalid email or password.");
      }
      const data = await res.json();
      // Store tokens
      sessionStorage.setItem("mg_access_token", data.access_token);
      sessionStorage.setItem("mg_refresh_token", data.refresh_token);
      router.replace("/overview");
    } catch (err: any) {
      setError(err.message ?? "Connection error. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen">
      {/* Left brand panel */}
      <div className="hidden md:flex flex-1 flex-col items-center justify-center relative overflow-hidden bg-gradient-to-br from-brand-500 to-surface-950">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_25%_50%,rgba(143,175,217,0.08)_0%,transparent_50%),radial-gradient(circle_at_75%_30%,rgba(20,72,140,0.12)_0%,transparent_50%)]" />
        <div className="relative z-10 text-center px-8">
          <h1 className="text-5xl font-extrabold text-[#F2F2F2] tracking-tight mb-2">
            OpenZep
          </h1>
          <p className="text-lg text-surface-300 max-w-sm mx-auto">
            Persistent Agent Memory Infrastructure
          </p>
          <div className="mt-8 flex gap-6 justify-center">
            {[
              { value: "10+", label: "Graph Backends" },
              { value: "5", label: "LLM Providers" },
              { value: "∞", label: "Scale" },
            ].map((stat) => (
              <div key={stat.label} className="text-center">
                <div className="text-3xl font-bold text-accent-300">{stat.value}</div>
                <div className="text-xs text-surface-500 mt-1">{stat.label}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Right form panel */}
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-sm">
          {/* Mobile brand */}
          <div className="md:hidden text-center mb-8">
            <h1 className="text-2xl font-extrabold text-brand-500">OpenZep</h1>
            <p className="text-xs text-surface-400">Agent Memory Infrastructure</p>
          </div>

          <div className="card-base p-6 shadow-glow-sm">
            <h2 className="text-xl font-semibold mb-1">Welcome back</h2>
            <p className="text-sm text-surface-400 mb-6">
              Sign in to your organization dashboard
            </p>

            <Suspense fallback={null}>
              <LoginNotice />
            </Suspense>

            {error && (
              <div className="mb-4 rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Email
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                  autoComplete="email"
                  className="input-base w-full"
                  placeholder="you@example.com"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Password
                </label>
                <div className="relative">
                  <input
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    autoComplete="current-password"
                    className="input-base w-full pr-10"
                    placeholder="Enter your password"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-surface-400 hover:text-[#F2F2F2]"
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="remember"
                  className="rounded border-surface-700 bg-surface-950 text-brand-500 focus:ring-accent-300"
                />
                <label htmlFor="remember" className="text-sm text-surface-400">
                  Remember me
                </label>
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="btn-primary w-full"
              >
                {submitting ? (
                  <Loader2 size={18} className="animate-spin" />
                ) : (
                  "Sign In"
                )}
              </button>
            </form>

            <div className="relative my-6">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t border-surface-800" />
              </div>
              <div className="relative flex justify-center text-xs">
                <span className="bg-surface-900 px-2 text-surface-500">or continue with</span>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <button className="btn-secondary w-full text-xs" disabled>
                GitHub
              </button>
              <button className="btn-secondary w-full text-xs" disabled>
                Google
              </button>
            </div>

            <p className="mt-6 text-center text-sm text-surface-400">
              Don&apos;t have an account?{" "}
              <Link href="/signup" className="text-accent-300 font-medium hover:text-accent-200">
                Sign up
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
