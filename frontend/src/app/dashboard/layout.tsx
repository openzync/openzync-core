"use client";

import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  Box,
  Drawer,
  AppBar,
  Toolbar,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
  IconButton,
  Avatar,
  Menu,
  MenuItem,
  Divider,
  useMediaQuery,
  useTheme,
  Tooltip,
} from "@mui/material";
import MenuIcon from "@mui/icons-material/Menu";
import PeopleIcon from "@mui/icons-material/People";
import VpnKeyIcon from "@mui/icons-material/VpnKey";
import DashboardIcon from "@mui/icons-material/Dashboard";
import SettingsIcon from "@mui/icons-material/Settings";
import BarChartIcon from "@mui/icons-material/BarChart";
import MonitorHeartIcon from "@mui/icons-material/MonitorHeart";
import SecurityIcon from "@mui/icons-material/Security";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import SchemaIcon from "@mui/icons-material/Schema";
import ChatIcon from "@mui/icons-material/Chat";
import LogoutIcon from "@mui/icons-material/Logout";
import SearchIcon from "@mui/icons-material/Search";
import DarkModeIcon from "@mui/icons-material/DarkMode";
import LightModeIcon from "@mui/icons-material/LightMode";
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft";
import { useAuth } from "@/lib/auth/useAuth";
import { AuthGuard } from "@/lib/auth/AuthGuard";
import { useThemeMode } from "@/app/MuiProvider";
import SearchCommandPalette from "@/components/shared/SearchCommandPalette";
import AnimatedPage from "@/components/shared/AnimatedPage";

const DRAWER_WIDTH = 240;
const DRAWER_COLLAPSED_WIDTH = 64;

const NAV_ITEMS = [
  { label: "Overview", path: "/dashboard", icon: <DashboardIcon /> },
  { label: "Users", path: "/dashboard/users", icon: <PeopleIcon /> },
  { label: "Sessions", path: "/dashboard/sessions", icon: <ChatIcon /> },
  { label: "API Keys", path: "/dashboard/api-keys", icon: <VpnKeyIcon /> },
  { label: "Analytics", path: "/dashboard/analytics", icon: <BarChartIcon /> },
  { label: "Monitoring", path: "/dashboard/metrics", icon: <MonitorHeartIcon /> },
  { label: "Audit Log", path: "/dashboard/audit", icon: <SecurityIcon /> },
  { label: "Graph", path: "/dashboard/graph", icon: <AccountTreeIcon /> },
  { label: "Schemas", path: "/dashboard/schemas", icon: <SchemaIcon /> },
  { label: "Settings", path: "/dashboard/settings", icon: <SettingsIcon /> },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, logout } = useAuth();
  const { mode, toggleTheme } = useThemeMode();
  const pathname = usePathname();
  const router = useRouter();
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const [mobileOpen, setMobileOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);
  const [searchOpen, setSearchOpen] = useState(false);

  // Global keyboard shortcut for search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen((prev) => !prev);
      }
      if (e.key === "Escape") {
        setSearchOpen(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const initials = user?.name
    ? user.name
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : user?.email?.[0]?.toUpperCase() ?? "?";

  const handleNavigate = useCallback(
    (path: string) => {
      router.push(path);
      setMobileOpen(false);
    },
    [router],
  );

  const isActive = (path: string) =>
    path === "/dashboard"
      ? pathname === "/dashboard"
      : pathname.startsWith(path);

  const drawerContent = (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        transition: "width 0.3s ease",
      }}
    >
      {/* Logo area */}
      <Box
        sx={{
          px: sidebarOpen ? 2.5 : 1,
          py: 2,
          display: "flex",
          alignItems: "center",
          justifyContent: sidebarOpen ? "space-between" : "center",
          minHeight: 64,
        }}
      >
        {sidebarOpen ? (
          <Box>
            <Typography
              variant="h6"
              sx={{
                fontWeight: 700,
                color: "primary.main",
                letterSpacing: "-0.02em",
                lineHeight: 1.2,
              }}
            >
              OpenZep
            </Typography>
            <Typography
              variant="caption"
              sx={{ color: "text.secondary", display: "block", fontSize: "0.65rem" }}
            >
              Memory Infrastructure
            </Typography>
          </Box>
        ) : (
          <Typography
            variant="h6"
            sx={{ fontWeight: 700, color: "primary.main" }}
          >
            O
          </Typography>
        )}
        {!isMobile && sidebarOpen && (
          <IconButton size="small" onClick={() => setSidebarOpen(false)}>
            <ChevronLeftIcon fontSize="small" />
          </IconButton>
        )}
        {!isMobile && !sidebarOpen && (
          <IconButton size="small" onClick={() => setSidebarOpen(true)}>
            <MenuIcon fontSize="small" />
          </IconButton>
        )}
      </Box>

      <Divider />

      {/* Navigation */}
      <List sx={{ px: 1, pt: 1, flex: 1, overflow: "auto" }}>
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.path);
          return (
            <Tooltip
              key={item.path}
              title={!sidebarOpen ? item.label : ""}
              placement="right"
              arrow
            >
              <ListItemButton
                selected={active}
                onClick={() => handleNavigate(item.path)}
                sx={{
                  borderRadius: 1,
                  mb: 0.25,
                  minHeight: 44,
                  justifyContent: sidebarOpen ? "initial" : "center",
                  px: sidebarOpen ? 1.5 : 1,
                  "&.Mui-selected": {
                    bgcolor: "rgba(20,72,140,0.15)",
                    borderLeft: "3px solid",
                    borderColor: "primary.main",
                    "&:hover": { bgcolor: "rgba(20,72,140,0.2)" },
                  },
                }}
              >
                <ListItemIcon
                  sx={{
                    minWidth: sidebarOpen ? 36 : 0,
                    justifyContent: "center",
                    color: active ? "primary.main" : "text.secondary",
                  }}
                >
                  {item.icon}
                </ListItemIcon>
                {sidebarOpen && (
                  <ListItemText
                    primary={item.label}
                    slotProps={{
                      primary: {
                        sx: {
                          fontSize: "0.875rem",
                          fontWeight: active ? 600 : 400,
                          color: active ? "primary.main" : "text.primary",
                        },
                        noWrap: true,
                      },
                    }}
                  />
                )}
              </ListItemButton>
            </Tooltip>
          );
        })}
      </List>

      {/* Bottom section */}
      <Box sx={{ px: sidebarOpen ? 2 : 1, py: 1.5 }}>
        <Divider sx={{ mb: 1 }} />
        <ListItemButton
          onClick={toggleTheme}
          sx={{
            borderRadius: 1,
            justifyContent: sidebarOpen ? "initial" : "center",
            minHeight: 40,
            px: sidebarOpen ? 1.5 : 1,
          }}
        >
          <ListItemIcon
            sx={{
              minWidth: sidebarOpen ? 36 : 0,
              justifyContent: "center",
              color: "text.secondary",
            }}
          >
            {mode === "dark" ? (
              <LightModeIcon fontSize="small" />
            ) : (
              <DarkModeIcon fontSize="small" />
            )}
          </ListItemIcon>
          {sidebarOpen && (
            <ListItemText
              primary={mode === "dark" ? "Light Mode" : "Dark Mode"}
              slotProps={{
                primary: {
                  sx: { fontSize: "0.8125rem", color: "text.secondary" },
                },
              }}
            />
          )}
        </ListItemButton>
      </Box>
    </Box>
  );

  const currentWidth = sidebarOpen ? DRAWER_WIDTH : DRAWER_COLLAPSED_WIDTH;

  return (
    <AuthGuard>
      <SearchCommandPalette open={searchOpen} onClose={() => setSearchOpen(false)} />
      <Box sx={{ display: "flex", minHeight: "100vh" }}>
        {/* ── Mobile drawer ─────────────────────────────────────────────── */}
        {isMobile && (
          <Drawer
            variant="temporary"
            open={mobileOpen}
            onClose={() => setMobileOpen(false)}
            sx={{ "& .MuiDrawer-paper": { width: DRAWER_WIDTH } }}
          >
            {drawerContent}
          </Drawer>
        )}

        {/* ── Desktop sidebar ──────────────────────────────────────────── */}
        {!isMobile && (
          <Drawer
            variant="permanent"
            sx={{
              width: currentWidth,
              flexShrink: 0,
              transition: "width 0.3s ease",
              whiteSpace: "nowrap",
              "& .MuiDrawer-paper": {
                width: currentWidth,
                transition: "width 0.3s ease",
                overflowX: "hidden",
              },
            }}
          >
            {drawerContent}
          </Drawer>
        )}

        {/* ── Main content area ────────────────────────────────────────── */}
        <Box
          sx={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minWidth: 0,
          }}
        >
          <AppBar
            position="sticky"
            sx={{
              ml: isMobile ? 0 : `${currentWidth}px`,
              transition: "margin-left 0.3s ease",
            }}
          >
            <Toolbar>
              {isMobile && (
                <IconButton
                  edge="start"
                  onClick={() => setMobileOpen(true)}
                  sx={{ mr: 1 }}
                >
                  <MenuIcon />
                </IconButton>
              )}

              {/* Page title from current route */}
              <Typography variant="h6" sx={{ flex: 1, fontWeight: 600 }}>
                {NAV_ITEMS.find((item) => isActive(item.path))?.label ?? "Dashboard"}
              </Typography>

              {/* Search */}
              <Tooltip title="Search (Ctrl+K)" arrow>
                <IconButton onClick={() => setSearchOpen(true)} sx={{ mr: 0.5 }}>
                  <SearchIcon />
                </IconButton>
              </Tooltip>

              {/* Theme toggle */}
              <Tooltip title={mode === "dark" ? "Light mode" : "Dark mode"} arrow>
                <IconButton onClick={toggleTheme} sx={{ mr: 0.5 }}>
                  {mode === "dark" ? (
                    <LightModeIcon fontSize="small" />
                  ) : (
                    <DarkModeIcon fontSize="small" />
                  )}
                </IconButton>
              </Tooltip>

              {/* Avatar */}
              <IconButton onClick={(e) => setAnchorEl(e.currentTarget)}>
                <Avatar
                  sx={{
                    width: 34,
                    height: 34,
                    bgcolor: "primary.main",
                    fontSize: "0.85rem",
                    fontWeight: 600,
                  }}
                >
                  {initials}
                </Avatar>
              </IconButton>

              <Menu
                anchorEl={anchorEl}
                open={Boolean(anchorEl)}
                onClose={() => setAnchorEl(null)}
                transformOrigin={{ horizontal: "right", vertical: "top" }}
                anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
                slotProps={{ paper: { sx: { minWidth: 200, mt: 0.5 } } }}
              >
                <MenuItem disabled sx={{ opacity: 0.7 }}>
                  <Typography variant="body2" color="text.secondary">
                    {user?.email}
                  </Typography>
                </MenuItem>
                <Divider />
                <MenuItem
                  onClick={() => {
                    setAnchorEl(null);
                    router.push("/dashboard/settings");
                  }}
                >
                  <ListItemIcon>
                    <SettingsIcon fontSize="small" />
                  </ListItemIcon>
                  Settings
                </MenuItem>
                <Divider />
                <MenuItem
                  onClick={() => {
                    setAnchorEl(null);
                    logout();
                    router.replace("/login");
                  }}
                >
                  <ListItemIcon>
                    <LogoutIcon fontSize="small" />
                  </ListItemIcon>
                  Sign Out
                </MenuItem>
              </Menu>
            </Toolbar>
          </AppBar>

          {/* ── Page content ─────────────────────────────────────────────── */}
          <Box
            sx={{
              flex: 1,
              p: 3,
              bgcolor: "background.default",
              overflow: "auto",
            }}
          >
            <AnimatedPage>{children}</AnimatedPage>
          </Box>
        </Box>
      </Box>
    </AuthGuard>
  );
}
