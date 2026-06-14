"use client";

import { useState } from "react";
import { useRouter, redirect } from "next/navigation";
import Link from "next/link";
import {
  Box,
  Card,
  CardContent,
  TextField,
  Typography,
  Button,
  Alert,
  InputAdornment,
  IconButton,
  CircularProgress,
} from "@mui/material";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { useAuth } from "@/lib/auth/useAuth";
import { ApiError } from "@/lib/api/client";

export default function LoginPage() {
  const { login, user } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  if (user) {
    redirect("/dashboard");
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login(email, password);
      router.replace("/dashboard");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.detail ?? "Invalid email or password.");
      } else {
        setError("Connection error. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Box
      sx={{
        display: "flex",
        minHeight: "100vh",
        bgcolor: "background.default",
      }}
    >
      {/* ── Left Brand Panel ─────────────────────────────────────────────── */}
      <Box
        sx={{
          flex: "1 1 50%",
          display: { xs: "none", md: "flex" },
          flexDirection: "column",
          justifyContent: "center",
          alignItems: "center",
          background: "linear-gradient(135deg, #14488C 0%, #0D1117 100%)",
          position: "relative",
          overflow: "hidden",
          "&::before": {
            content: '""',
            position: "absolute",
            inset: 0,
            backgroundImage:
              "radial-gradient(circle at 25% 50%, rgba(143,175,217,0.08) 0%, transparent 50%), radial-gradient(circle at 75% 30%, rgba(20,72,140,0.12) 0%, transparent 50%)",
          },
        }}
      >
        <Box sx={{ position: "relative", zIndex: 1, textAlign: "center", px: 4 }}>
          <Typography
            variant="h3"
            sx={{
              fontWeight: 800,
              color: "#F2F2F2",
              letterSpacing: "-0.03em",
              mb: 1,
            }}
          >
            OpenZep
          </Typography>
          <Typography
            variant="h6"
            sx={{
              color: "rgba(242,242,242,0.7)",
              fontWeight: 400,
              maxWidth: 360,
            }}
          >
            Persistent Agent Memory Infrastructure
          </Typography>
          <Box
            sx={{
              mt: 4,
              display: "flex",
              gap: 3,
              justifyContent: "center",
              "& > div": {
                textAlign: "center",
              },
            }}
          >
            <Box>
              <Typography variant="h4" sx={{ color: "#8FAFD9", fontWeight: 700 }}>
                10+
              </Typography>
              <Typography variant="caption" sx={{ color: "rgba(242,242,242,0.5)" }}>
                Graph Backends
              </Typography>
            </Box>
            <Box>
              <Typography variant="h4" sx={{ color: "#8FAFD9", fontWeight: 700 }}>
                5
              </Typography>
              <Typography variant="caption" sx={{ color: "rgba(242,242,242,0.5)" }}>
                LLM Providers
              </Typography>
            </Box>
            <Box>
              <Typography variant="h4" sx={{ color: "#8FAFD9", fontWeight: 700 }}>
                ∞
              </Typography>
              <Typography variant="caption" sx={{ color: "rgba(242,242,242,0.5)" }}>
                Scale
              </Typography>
            </Box>
          </Box>
        </Box>
      </Box>

      {/* ── Right Form Panel ─────────────────────────────────────────────── */}
      <Box
        sx={{
          flex: "1 1 50%",
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          p: 4,
        }}
      >
        <Card
          sx={{
            width: 420,
            maxWidth: "100%",
            boxShadow: "0 0 40px rgba(20,72,140,0.15)",
          }}
        >
          <CardContent sx={{ p: 4 }}>
            {/* Mobile brand */}
            <Box sx={{ display: { xs: "block", md: "none" }, mb: 3, textAlign: "center" }}>
              <Typography
                variant="h5"
                sx={{ fontWeight: 800, color: "primary.main", letterSpacing: "-0.02em" }}
              >
                OpenZep
              </Typography>
              <Typography variant="caption" color="text.secondary">
                Agent Memory Infrastructure
              </Typography>
            </Box>

            <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>
              Welcome back
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
              Sign in to your organization dashboard
            </Typography>

            {error && (
              <Alert severity="error" sx={{ mb: 2, borderRadius: 1.5 }}>
                {error}
              </Alert>
            )}

            <Box component="form" onSubmit={handleSubmit}>
              <TextField
                fullWidth
                label="Email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoFocus
                autoComplete="email"
                sx={{ mb: 2 }}
              />
              <TextField
                fullWidth
                label="Password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                sx={{ mb: 3 }}
                slotProps={{
                  input: {
                    endAdornment: (
                      <InputAdornment position="end">
                        <IconButton
                          onClick={() => setShowPassword(!showPassword)}
                          edge="end"
                          size="small"
                        >
                          {showPassword ? (
                            <VisibilityOffIcon fontSize="small" />
                          ) : (
                            <VisibilityIcon fontSize="small" />
                          )}
                        </IconButton>
                      </InputAdornment>
                    ),
                  },
                }}
              />
              <Button
                type="submit"
                variant="contained"
                fullWidth
                size="large"
                disabled={submitting}
                sx={{ mb: 2 }}
              >
                {submitting ? (
                  <CircularProgress size={20} color="inherit" />
                ) : (
                  "Sign In"
                )}
              </Button>
            </Box>

            <Typography variant="body2" align="center" color="text.secondary">
              Don&apos;t have an account?{" "}
              <Link
                href="/signup"
                style={{
                  color: "#8FAFD9",
                  fontWeight: 600,
                  textDecoration: "none",
                }}
              >
                Sign up
              </Link>
            </Typography>
          </CardContent>
        </Card>
      </Box>
    </Box>
  );
}
