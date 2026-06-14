"use client";

import { useState, useCallback } from "react";
import {
  Box,
  Button,
  Card,
  CardContent,
  TextField,
  Typography,
  CircularProgress,
} from "@mui/material";
import { updateProfile, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import PageHeader from "@/components/shared/PageHeader";
import { useNotification } from "@/components/shared/NotificationProvider";

// ─── Page Component ───────────────────────────────────────────────────────────

export default function SettingsPage() {
  // Auth is guarded by dashboard layout AuthGuard; user populated on mount.
  const { user, refreshProfile } = useAuth();
  const { showNotification } = useNotification();

  // ── Profile form state ──────────────────────────────────────────────────────

  const [profileName, setProfileName] = useState(user?.name ?? "");
  const [profileEmail, setProfileEmail] = useState(user?.email ?? "");
  const [profileSubmitting, setProfileSubmitting] = useState(false);

  // ── Password form state ─────────────────────────────────────────────────────

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordSubmitting, setPasswordSubmitting] = useState(false);
  const [passwordError, setPasswordError] = useState("");

  // ── Save Profile ────────────────────────────────────────────────────────────

  const handleSaveProfile = useCallback(async () => {
    setProfileSubmitting(true);

    try {
      await updateProfile({
        name: profileName.trim() || null,
        email: profileEmail.trim() || null,
      });
      await refreshProfile();
      showNotification("Profile updated successfully", "success");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update profile"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setProfileSubmitting(false);
    }
  }, [profileName, profileEmail, showNotification, refreshProfile]);

  // ── Update Password ─────────────────────────────────────────────────────────

  const handleUpdatePassword = useCallback(async () => {
    if (!currentPassword) {
      setPasswordError("Current password is required");
      return;
    }
    if (!newPassword || newPassword.length < 8) {
      setPasswordError("New password must be at least 8 characters");
      return;
    }

    setPasswordSubmitting(true);
    setPasswordError("");

    try {
      await updateProfile({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      showNotification("Password updated successfully", "success");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update password"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setPasswordSubmitting(false);
    }
  }, [currentPassword, newPassword, showNotification]);

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box
      sx={{ display: "flex", flexDirection: "column", gap: 3, maxWidth: 640 }}
    >
      <PageHeader title="Settings" />

      {/* ── Profile Card ──────────────────────────────────────────────────── */}
      <Card>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            Profile
          </Typography>

          <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <TextField
              label="Name"
              value={profileName}
              onChange={(e) => setProfileName(e.target.value)}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Email"
              type="email"
              value={profileEmail}
              onChange={(e) => setProfileEmail(e.target.value)}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Role"
              value={user?.role ?? "member"}
              disabled
            />
          </Box>

          <Box sx={{ display: "flex", justifyContent: "flex-end", mt: 2 }}>
            <Button
              variant="contained"
              onClick={handleSaveProfile}
              disabled={profileSubmitting}
              startIcon={
                profileSubmitting ? (
                  <CircularProgress size={16} color="inherit" />
                ) : undefined
              }
            >
              {profileSubmitting ? "Saving..." : "Save Changes"}
            </Button>
          </Box>
        </CardContent>
      </Card>

      {/* ── Change Password Card ──────────────────────────────────────────── */}
      <Card>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            Change Password
          </Typography>

          <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <TextField
              label="Current Password"
              type="password"
              value={currentPassword}
              onChange={(e) => {
                setCurrentPassword(e.target.value);
                if (passwordError) setPasswordError("");
              }}
            />
            <TextField
              label="New Password"
              type="password"
              value={newPassword}
              onChange={(e) => {
                setNewPassword(e.target.value);
                if (passwordError) setPasswordError("");
              }}
              slotProps={{ htmlInput: { minLength: 8 } }}
            />
          </Box>

          {passwordError && (
            <Typography variant="body2" sx={{ color: "error.main", mt: 2 }}>
              {passwordError}
            </Typography>
          )}

          <Box sx={{ display: "flex", justifyContent: "flex-end", mt: 2 }}>
            <Button
              variant="contained"
              onClick={handleUpdatePassword}
              disabled={passwordSubmitting}
              startIcon={
                passwordSubmitting ? (
                  <CircularProgress size={16} color="inherit" />
                ) : undefined
              }
            >
              {passwordSubmitting ? "Updating..." : "Update Password"}
            </Button>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
