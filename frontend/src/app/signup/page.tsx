"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Eye, EyeOff, Loader2 } from "lucide-react";

function getPasswordStrength(pw: string): {
  score: number;
  label: string;
  color: string;
} {
  let score = 0;
  if (pw.length >= 8) score += 1;
  if (pw.length >= 12) score += 1;
  if (/[A-Z]/.test(pw)) score += 1;
  if (/[0-9]/.test(pw)) score += 1;
  if (/[^A-Za-z0-9]/.test(pw)) score += 1;
  if (score <= 1) return { score: 20, label: "Weak", color: "bg-error" };
  if (score <= 2) return { score: 40, label: "Fair", color: "bg-warning" };
  if (score <= 3) return { score: 60, label: "Good", color: "bg-warning" };
  if (score <= 4) return { score: 80, label: "Strong", color: "bg-success" };
  return { score: 100, label: "Very Strong", color: "bg-success" };
}

export default function SignupPage() {
  const router = useRouter();
  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  const pwStrength = getPasswordStrength(password);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);

    try {
      const res = await fetch("http://localhost:8000/v1/auth/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          password,
          organization_name: orgName,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail ?? "Signup failed. Please try again.");
      }
      const data = await res.json();
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
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_75%_50%,rgba(143,175,217,0.08)_0%,transparent_50%),radial-gradient(circle_at_25%_30%,rgba(20,72,140,0.12)_0%,transparent_50%)]" />
        <div className="relative z-10 text-center px-8">
          <h1 className="text-5xl font-extrabold text-[#F2F2F2] tracking-tight mb-2">
            OpenZep
          </h1>
          <p className="text-lg text-surface-300 max-w-sm mx-auto mb-8">
            Get started with agent memory infrastructure
          </p>
          <div className="text-left max-w-xs mx-auto space-y-3">
            {[
              "Persistent, queryable agent memory",
              "Multi-provider LLM support (BYOK)",
              "Knowledge graph with hybrid search",
              "Async enrichment pipeline",
            ].map((feature, i) => (
              <div key={i} className="flex items-start gap-2.5">
                <div className="mt-1.5 h-2 w-2 rounded-full bg-accent-300 shrink-0" />
                <span className="text-sm text-surface-300">{feature}</span>
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

          <div className="card-base p-6">
            <h2 className="text-xl font-semibold mb-1">Create your account</h2>
            <p className="text-sm text-surface-400 mb-6">
              Set up your OpenZep organization
            </p>

            {error && (
              <div className="mb-4 rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Organization Name
                </label>
                <input
                  type="text"
                  value={orgName}
                  onChange={(e) => setOrgName(e.target.value)}
                  required
                  autoFocus
                  className="input-base w-full"
                  placeholder="My Organization"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Email
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
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
                    minLength={8}
                    autoComplete="new-password"
                    className="input-base w-full pr-10"
                    placeholder="Minimum 8 characters"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-surface-400 hover:text-[#F2F2F2]"
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {password && (
                  <div className="mt-2">
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-surface-500">Password strength</span>
                      <span className="font-medium text-surface-300">{pwStrength.label}</span>
                    </div>
                    <div className="h-1.5 w-full rounded-full bg-surface-800 overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${pwStrength.color}`}
                        style={{ width: `${pwStrength.score}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="btn-primary w-full mt-2"
              >
                {submitting ? (
                  <Loader2 size={18} className="animate-spin" />
                ) : (
                  "Create Account"
                )}
              </button>
            </form>

            <p className="mt-6 text-center text-sm text-surface-400">
              Already have an account?{" "}
              <Link href="/login" className="text-accent-300 font-medium hover:text-accent-200">
                Sign in
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
