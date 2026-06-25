"use client";
import { RequireAuth } from "../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Save,
  Lock,
  User,
  Mail,
  Shield,
  CheckCircle,
  AlertCircle,
  Eye,
  EyeOff,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE } from "@/lib/api-client";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ProfileResponse {
  id: string;
  name: string | null;
  email: string | null;
  role: string;
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ─── Constants ─────────────────────────────────────────────────────────────────

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Toast ─────────────────────────────────────────────────────────────────────

const TOAST_DURATION = 4000;

function Toast({ toast, onDismiss }: { toast: ToastState; onDismiss: () => void }) {
  useEffect(() => {
    if (!toast.visible) return;
    const timer = setTimeout(onDismiss, TOAST_DURATION);
    return () => clearTimeout(timer);
  }, [toast.visible, onDismiss]);

  if (!toast.visible) return null;

  const isSuccess = toast.type === "success";

  return (
    <div className="fixed bottom-6 right-6 z-[60] animate-slide-up">
      <div
        className={cn(
          "flex items-center gap-3 rounded-lg px-4 py-3 shadow-lg shadow-black/30 border min-w-[280px] max-w-sm",
          isSuccess
            ? "bg-surface-900 border-success/40 text-success"
            : "bg-surface-900 border-error/40 text-error",
        )}
      >
        {isSuccess ? <CheckCircle size={18} /> : <AlertCircle size={18} />}
        <span className="text-sm text-white flex-1">{toast.message}</span>
        <button onClick={onDismiss} className="text-surface-400 hover:text-white shrink-0">
          <X size={14} />
        </button>
      </div>
    </div>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  // ── Profile state ──────────────────────────────────────────────────────────
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  // ── Password state ─────────────────────────────────────────────────────────
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [updatingPassword, setUpdatingPassword] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  // ── Toast state ────────────────────────────────────────────────────────────
  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch profile ──────────────────────────────────────────────────────────

  const fetchProfile = useCallback(async () => {
    setProfileLoading(true);
    try {
      // Use the current user endpoint — if there's a specific profile endpoint, use that.
      // Fallback: GET /v1/users/me or similar. Since we don't have explicit docs,
      // try a generic profile endpoint.
      const res = await fetch(`${API_BASE}/v1/admin/me`, { headers: authHeaders() });
      if (!res.ok) {
        // Fallback: some endpoints might not exist; try settings/profile
        const fallbackRes = await fetch(`${API_BASE}/v1/admin/profile`, { headers: authHeaders() });
        if (!fallbackRes.ok) throw new Error("Failed to load profile");
        const data = await fallbackRes.json();
        processProfile(data);
      } else {
        const data = await res.json();
        processProfile(data);
      }
    } catch {
      // Silently fail — show empty form
      setProfileLoading(false);
    }
  }, []);

  const processProfile = (data: Record<string, unknown>) => {
    const p: ProfileResponse = {
      id: (data.id as string) ?? "",
      name: (data.name as string) ?? null,
      email: (data.email as string) ?? null,
      role: (data.role as string) ?? "admin",
    };
    setProfile(p);
    setName(p.name ?? "");
    setEmail(p.email ?? "");
    setProfileLoading(false);
  };

  useEffect(() => {
    fetchProfile();
  }, [fetchProfile]);

  // ── Save profile ───────────────────────────────────────────────────────────

  const handleSaveProfile = async () => {
    setSavingProfile(true);
    try {
      const payload: Record<string, string> = {};
      if (name.trim()) payload.name = name.trim();
      if (email.trim()) payload.email = email.trim();

      const res = await fetch(`${API_BASE}/v1/admin/profile`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to update profile");
      }

      showToast("Profile updated successfully", "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to update profile", "error");
    } finally {
      setSavingProfile(false);
    }
  };

  // ── Update password ────────────────────────────────────────────────────────

  const handleUpdatePassword = async () => {
    setPasswordError(null);

    if (!currentPassword) {
      setPasswordError("Current password is required");
      return;
    }
    if (!newPassword) {
      setPasswordError("New password is required");
      return;
    }
    if (newPassword.length < 8) {
      setPasswordError("New password must be at least 8 characters");
      return;
    }

    setUpdatingPassword(true);
    try {
      const res = await fetch(`${API_BASE}/v1/admin/password`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to update password");
      }

      setCurrentPassword("");
      setNewPassword("");
      showToast("Password updated successfully", "success");
    } catch (err) {
      setPasswordError(err instanceof Error ? err.message : "Failed to update password");
    } finally {
      setUpdatingPassword(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-sm text-surface-400 mt-1">Manage your profile and organization</p>
      </div>

      {/* ── Profile Card ────────────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <User size={20} className="text-brand-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Profile</h2>
            <p className="text-xs text-surface-400">Your personal information</p>
          </div>
        </div>

        {profileLoading ? (
          <div className="space-y-4">
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-48" />
          </div>
        ) : (
          <div className="space-y-4 max-w-md">
            {/* Name */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">Name</label>
              <input
                className="input-base"
                placeholder="Your name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>

            {/* Email */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">Email</label>
              <input
                className="input-base"
                type="email"
                placeholder="your@email.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>

            {/* Role — disabled */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">Role</label>
              <div className="relative">
                <input
                  className="input-base pr-10 cursor-not-allowed opacity-60"
                  value={profile?.role ?? "admin"}
                  disabled
                  readOnly
                />
                <Shield size={14} className="absolute right-3 top-1/2 -translate-y-1/2 text-surface-500 pointer-events-none" />
              </div>
              <p className="text-xs text-surface-500 mt-1">Role is assigned by your organization administrator.</p>
            </div>

            {/* Save button */}
            <div className="pt-2">
              <Button
                variant="primary"
                onClick={handleSaveProfile}
                disabled={savingProfile}
                className="text-sm"
              >
                <Save size={14} />
                {savingProfile ? "Saving..." : "Save Changes"}
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* ── Change Password Card ─────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-warning/10">
            <Lock size={20} className="text-warning" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Change Password</h2>
            <p className="text-xs text-surface-400">Update your account password</p>
          </div>
        </div>

        <div className="space-y-4 max-w-md">
          {/* Current Password */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Current Password</label>
            <input
              className="input-base"
              type="password"
              placeholder="Enter current password"
              value={currentPassword}
              onChange={(e) => {
                setCurrentPassword(e.target.value);
                if (passwordError) setPasswordError(null);
              }}
            />
          </div>

          {/* New Password */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">New Password</label>
            <div className="relative">
              <input
                className="input-base pr-10"
                type={showNewPassword ? "text" : "password"}
                placeholder="Enter new password (min 8 characters)"
                value={newPassword}
                onChange={(e) => {
                  setNewPassword(e.target.value);
                  if (passwordError) setPasswordError(null);
                }}
              />
              <button
                type="button"
                onClick={() => setShowNewPassword((prev) => !prev)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
              >
                {showNewPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            {newPassword.length > 0 && newPassword.length < 8 && (
              <p className="text-xs text-warning mt-1">
                Password must be at least 8 characters ({newPassword.length}/8)
              </p>
            )}
            {newPassword.length >= 8 && (
              <p className="text-xs text-success mt-1">
                Password meets minimum length requirement
              </p>
            )}
          </div>

          {/* Error */}
          {passwordError && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {passwordError}
            </div>
          )}

          {/* Update button */}
          <div className="pt-2">
            <Button
              variant="primary"
              onClick={handleUpdatePassword}
              disabled={updatingPassword || !currentPassword || !newPassword || newPassword.length < 8}
              className="text-sm"
            >
              <Lock size={14} />
              {updatingPassword ? "Updating..." : "Update Password"}
            </Button>
          </div>
        </div>
      </div>

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
