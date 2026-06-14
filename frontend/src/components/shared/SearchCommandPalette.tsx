"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  Dialog,
  DialogContent,
  TextField,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
  Box,
} from "@mui/material";
import DashboardIcon from "@mui/icons-material/Dashboard";
import PeopleIcon from "@mui/icons-material/People";
import VpnKeyIcon from "@mui/icons-material/VpnKey";
import BarChartIcon from "@mui/icons-material/BarChart";
import MonitorHeartIcon from "@mui/icons-material/MonitorHeart";
import SecurityIcon from "@mui/icons-material/Security";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import SchemaIcon from "@mui/icons-material/Schema";
import ChatIcon from "@mui/icons-material/Chat";
import SettingsIcon from "@mui/icons-material/Settings";
import SearchIcon from "@mui/icons-material/Search";

const PAGES = [
  { label: "Overview", path: "/dashboard", icon: <DashboardIcon />, keywords: "home dashboard overview stats" },
  { label: "Users", path: "/dashboard/users", icon: <PeopleIcon />, keywords: "users people accounts" },
  { label: "Sessions", path: "/dashboard/sessions", icon: <ChatIcon />, keywords: "sessions conversations chat" },
  { label: "API Keys", path: "/dashboard/api-keys", icon: <VpnKeyIcon />, keywords: "api keys tokens auth" },
  { label: "Analytics", path: "/dashboard/analytics", icon: <BarChartIcon />, keywords: "analytics charts usage stats" },
  { label: "Monitoring", path: "/dashboard/metrics", icon: <MonitorHeartIcon />, keywords: "monitoring metrics health" },
  { label: "Audit Log", path: "/dashboard/audit", icon: <SecurityIcon />, keywords: "audit log history events" },
  { label: "Graph", path: "/dashboard/graph", icon: <AccountTreeIcon />, keywords: "graph entities knowledge" },
  { label: "Schemas", path: "/dashboard/schemas", icon: <SchemaIcon />, keywords: "schemas extraction entity types" },
  { label: "Settings", path: "/dashboard/settings", icon: <SettingsIcon />, keywords: "settings profile password" },
];

interface SearchCommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export default function SearchCommandPalette({
  open,
  onClose,
}: SearchCommandPaletteProps) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const filtered = query.trim()
    ? PAGES.filter(
        (p) =>
          p.label.toLowerCase().includes(query.toLowerCase()) ||
          p.keywords.toLowerCase().includes(query.toLowerCase()),
      )
    : PAGES;

  // Reset selection when query changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  const handleSelect = useCallback(
    (path: string) => {
      onClose();
      router.push(path);
    },
    [onClose, router],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((prev) => Math.min(prev + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((prev) => Math.max(prev - 1, 0));
    } else if (e.key === "Enter" && filtered[selectedIndex]) {
      handleSelect(filtered[selectedIndex].path);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="sm"
      fullWidth
      slotProps={{
        paper: {
          sx: {
            mt: "10vh",
            borderRadius: 2,
            overflow: "hidden",
          },
        },
      }}
      onTransitionExited={() => setQuery("")}
    >
      <Box sx={{ px: 2, py: 1.5, display: "flex", alignItems: "center", gap: 1.5 }}>
        <SearchIcon sx={{ color: "text.secondary", fontSize: 20 }} />
        <TextField
          autoFocus
          fullWidth
          placeholder="Search pages..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          variant="standard"
          slotProps={{
            input: {
              disableUnderline: true,
              sx: { fontSize: "1rem" },
            },
          }}
        />
        <Typography
          variant="caption"
          sx={{
            color: "text.disabled",
            bgcolor: "rgba(143,175,217,0.08)",
            px: 0.75,
            py: 0.25,
            borderRadius: 0.5,
            fontFamily: "monospace",
            fontSize: "0.7rem",
          }}
        >
          ESC
        </Typography>
      </Box>
      <DialogContent sx={{ p: 0, maxHeight: 360 }}>
        {filtered.length === 0 ? (
          <Box sx={{ p: 4, textAlign: "center" }}>
            <Typography variant="body2" color="text.secondary">
              No pages found
            </Typography>
          </Box>
        ) : (
          <List sx={{ p: 1 }}>
            {filtered.map((page, i) => (
              <ListItemButton
                key={page.path}
                selected={i === selectedIndex}
                onClick={() => handleSelect(page.path)}
                sx={{ borderRadius: 1, mb: 0.25 }}
              >
                <ListItemIcon sx={{ minWidth: 40, color: "text.secondary" }}>
                  {page.icon}
                </ListItemIcon>
                <ListItemText
                  primary={page.label}
                  slotProps={{ primary: { sx: { fontSize: "0.9rem" } } }}
                />
              </ListItemButton>
            ))}
          </List>
        )}
      </DialogContent>
    </Dialog>
  );
}
