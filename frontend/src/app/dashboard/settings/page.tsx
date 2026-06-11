"use client";

import { useState, useCallback } from "react";
import {
  Box,
  Button,
  Card,
  CardContent,
  TextField,
  Typography,
  Snackbar,
  Alert,
  CircularProgress,
} from "@mui/material";
import { updateProfile, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

// ─── Types ────────────────────────────────────────────────────────────────────

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function SettingsPage() {
  // Auth is guarded by dashboard layout AuthGuard; user populated on mount.
  const { user } = useAuth();

  // ── Profile form state ──────────────────────────────────────────────────────

  const [profileName, setProfileName] = useState(user?.name ?? "");
  const [profileEmail, setProfileEmail] = useState(user?.email ?? "");
  const [profileSubmitting, setProfileSubmitting] = useState(false);
  const [profileSuccess, setProfileSuccess] = useState(false);
  const [profileError, setProfileError] = useState("");

  // ── Password form state ─────────────────────────────────────────────────────

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordSubmitting, setPasswordSubmitting] = useState(false);
  const [passwordSuccess, setPasswordSuccess] = useState(false);
  const [passwordError, setPasswordError] = useState("");

  // ── Snackbar state ──────────────────────────────────────────────────────────

  const [snackbar, setSnackbar] = useState<SnackbarState>({
    open: false,
    message: "",
    severity: "success",
  });

  const showSnackbar = useCallback(
    (message: string, severity: "success" | "error") => {
      setSnackbar({ open: true, message, severity });
    },
    [],
  );

  // ── Save Profile ────────────────────────────────────────────────────────────
  // note: updateProfile returns the updated DashboardUserResponse.
  // The AuthContext doesn't expose a setUser, so the local `user` remains
  // stale until the next page refresh or token refresh. A future improvement
  // would expose a refreshProfile() method from useAuth.

  const handleSaveProfile = async () => {
    setProfileSubmitting(true);
    setProfileSuccess(false);
    setProfileError("");

    try {
      await updateProfile({
        name: profileName.trim() || null,
        email: profileEmail.trim() || null,
      });
      setProfileSuccess(true);
      showSnackbar("Profile updated successfully", "success");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update profile"
          : "An unexpected error occurred";
      setProfileError(message);
      showSnackbar(message, "error");
    } finally {
      setProfileSubmitting(false);
    }
  };

  // ── Update Password ─────────────────────────────────────────────────────────

  const handleUpdatePassword = async () => {
    if (!currentPassword) {
      setPasswordError("Current password is required");
      return;
    }
    if (!newPassword || newPassword.length < 8) {
      setPasswordError("New password must be at least 8 characters");
      return;
    }

    setPasswordSubmitting(true);
    setPasswordSuccess(false);
    setPasswordError("");

    try {
      await updateProfile({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setPasswordSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
      showSnackbar("Password updated successfully", "success");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update password"
          : "An unexpected error occurred";
      setPasswordError(message);
      showSnackbar(message, "error");
    } finally {
      setPasswordSubmitting(false);
    }
  };

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3, maxWidth: 640 }}>
      <Typography variant="h5" sx={{ fontWeight: 600 }}>
        Settings
      </Typography>

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
              onChange={(e) => {
                setProfileName(e.target.value);
                if (profileError) setProfileError("");
                if (profileSuccess) setProfileSuccess(false);
              }}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Email"
              type="email"
              value={profileEmail}
              onChange={(e) => {
                setProfileEmail(e.target.value);
                if (profileError) setProfileError("");
                if (profileSuccess) setProfileSuccess(false);
              }}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Role"
              value={user?.role ?? "member"}
              disabled
            />
          </Box>

          {profileError && (
            <Typography variant="body2" sx={{ color: "error.main", mt: 2 }}>
              {profileError}
            </Typography>
          )}

          {profileSuccess && (
            <Typography variant="body2" sx={{ color: "success.main", mt: 2 }}>
              Profile saved successfully.
            </Typography>
          )}

          <Box sx={{ display: "flex", justifyContent: "flex-end", mt: 2 }}>
            <Button
              variant="contained"
              onClick={handleSaveProfile}
              disabled={profileSubmitting}
              startIcon={profileSubmitting ? <CircularProgress size={16} color="inherit" /> : undefined}
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
                if (passwordSuccess) setPasswordSuccess(false);
              }}
            />
            <TextField
              label="New Password"
              type="password"
              value={newPassword}
              onChange={(e) => {
                setNewPassword(e.target.value);
                if (passwordError) setPasswordError("");
                if (passwordSuccess) setPasswordSuccess(false);
              }}
              slotProps={{ htmlInput: { minLength: 8 } }}
            />
          </Box>

          {passwordError && (
            <Typography variant="body2" sx={{ color: "error.main", mt: 2 }}>
              {passwordError}
            </Typography>
          )}

          {passwordSuccess && (
            <Typography variant="body2" sx={{ color: "success.main", mt: 2 }}>
              Password updated successfully.
            </Typography>
          )}

          <Box sx={{ display: "flex", justifyContent: "flex-end", mt: 2 }}>
            <Button
              variant="contained"
              onClick={handleUpdatePassword}
              disabled={passwordSubmitting}
              startIcon={passwordSubmitting ? <CircularProgress size={16} color="inherit" /> : undefined}
            >
              {passwordSubmitting ? "Updating..." : "Update Password"}
            </Button>
          </Box>
        </CardContent>
      </Card>

      {/* ── Snackbar ───────────────────────────────────────────────────────── */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={4000}
        onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  );
}
