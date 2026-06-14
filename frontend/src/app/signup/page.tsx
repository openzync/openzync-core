"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
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
  LinearProgress,
} from "@mui/material";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { useAuth } from "@/lib/auth/useAuth";
import { ApiError } from "@/lib/api/client";

function getPasswordStrength(pw: string): {
  score: number;
  label: string;
  color: "error" | "warning" | "success";
} {
  let score = 0;
  if (pw.length >= 8) score += 1;
  if (pw.length >= 12) score += 1;
  if (/[A-Z]/.test(pw)) score += 1;
  if (/[0-9]/.test(pw)) score += 1;
  if (/[^A-Za-z0-9]/.test(pw)) score += 1;

  if (score <= 1) return { score: 25, label: "Weak", color: "error" };
  if (score <= 2) return { score: 50, label: "Fair", color: "warning" };
  if (score <= 3) return { score: 75, label: "Good", color: "warning" };
  return { score: 100, label: "Strong", color: "success" };
}

export default function SignupPage() {
  const { signup, user } = useAuth();
  const router = useRouter();
  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  if (user) {
    router.replace("/dashboard");
    return null;
  }

  const pwStrength = getPasswordStrength(password);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await signup(email, password, orgName);
      router.replace("/dashboard");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.detail ?? "Signup failed. Please try again.");
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
              "radial-gradient(circle at 75% 50%, rgba(143,175,217,0.08) 0%, transparent 50%), radial-gradient(circle at 25% 30%, rgba(20,72,140,0.12) 0%, transparent 50%)",
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
            Get started with agent memory infrastructure
          </Typography>
          <Box
            sx={{
              mt: 4,
              display: "flex",
              flexDirection: "column",
              gap: 2,
              textAlign: "left",
              maxWidth: 320,
            }}
          >
            {[
              "Persistent, queryable agent memory",
              "Multi-provider LLM support (BYOK)",
              "Knowledge graph with hybrid search",
              "Async enrichment pipeline",
            ].map((feature, i) => (
              <Box key={i} sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
                <Box
                  sx={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    bgcolor: "#8FAFD9",
                    flexShrink: 0,
                  }}
                />
                <Typography variant="body2" sx={{ color: "rgba(242,242,242,0.8)" }}>
                  {feature}
                </Typography>
              </Box>
            ))}
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
              Create your account
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
              Set up your OpenZep organization
            </Typography>

            {error && (
              <Alert severity="error" sx={{ mb: 2, borderRadius: 1.5 }}>
                {error}
              </Alert>
            )}

            <Box component="form" onSubmit={handleSubmit}>
              <TextField
                fullWidth
                label="Organization Name"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                required
                autoFocus
                sx={{ mb: 2 }}
              />
              <TextField
                fullWidth
                label="Email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
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
                autoComplete="new-password"
                sx={{ mb: 1 }}
                slotProps={{
                  htmlInput: { minLength: 8 },
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
              {password && (
                <Box sx={{ mb: 2 }}>
                  <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 0.5 }}>
                    <Typography variant="caption" color="text.secondary">
                      Password strength
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{ color: `${pwStrength.color}.main`, fontWeight: 600 }}
                    >
                      {pwStrength.label}
                    </Typography>
                  </Box>
                  <LinearProgress
                    variant="determinate"
                    value={pwStrength.score}
                    color={pwStrength.color}
                    sx={{ height: 4, borderRadius: 2 }}
                  />
                </Box>
              )}
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
                  "Create Account"
                )}
              </Button>
            </Box>

            <Typography variant="body2" align="center" color="text.secondary">
              Already have an account?{" "}
              <Link
                href="/login"
                style={{
                  color: "#8FAFD9",
                  fontWeight: 600,
                  textDecoration: "none",
                }}
              >
                Sign in
              </Link>
            </Typography>
          </CardContent>
        </Card>
      </Box>
    </Box>
  );
}
