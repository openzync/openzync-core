"use client";

import { useState } from "react";
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
import FolderIcon from "@mui/icons-material/Folder";
import SchemaIcon from "@mui/icons-material/Schema";
import ChatIcon from "@mui/icons-material/Chat";
import LogoutIcon from "@mui/icons-material/Logout";
import { useAuth } from "@/lib/auth/useAuth";
import { AuthGuard } from "@/lib/auth/AuthGuard";

const DRAWER_WIDTH = 240;

const NAV_ITEMS = [
  { label: "Overview", path: "/dashboard", icon: <DashboardIcon /> },
  { label: "Users", path: "/dashboard/users", icon: <PeopleIcon /> },
  { label: "Projects", path: "/dashboard/projects", icon: <FolderIcon /> },
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
  const pathname = usePathname();
  const router = useRouter();
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const [mobileOpen, setMobileOpen] = useState(false);
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  const initials = user?.name
    ? user.name
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : user?.email?.[0]?.toUpperCase() ?? "?";

  const drawerContent = (
    <Box>
      <Box sx={{ p: 2.5, pb: 1.5 }}>
        <Typography variant="h6" sx={{ fontWeight: 600, color: "primary.dark" }}>
          OpenZep
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Memory Infrastructure
        </Typography>
      </Box>
      <Divider />
      <List sx={{ px: 1, pt: 1 }}>
        {NAV_ITEMS.map((item) => {
          const isActive =
            item.path === "/dashboard"
              ? pathname === "/dashboard"
              : pathname.startsWith(item.path);
          return (
            <ListItemButton
              key={item.path}
              selected={isActive}
              onClick={() => {
                router.push(item.path);
                setMobileOpen(false);
              }}
              sx={{ borderRadius: 1, mb: 0.5 }}
            >
              <ListItemIcon
                sx={{
                  minWidth: 40,
                  color: isActive ? "primary.main" : "text.secondary",
                }}
              >
                {item.icon}
              </ListItemIcon>
              <ListItemText
                primary={item.label}
                slotProps={{
                  primary: {
                    sx: {
                      fontSize: "0.9rem",
                      fontWeight: isActive ? 500 : 400,
                      color: isActive ? "primary.main" : "text.primary",
                    },
                  },
                }}
              />
            </ListItemButton>
          );
        })}
      </List>
    </Box>
  );

  return (
    <AuthGuard>
      <Box sx={{ display: "flex", minHeight: "100vh" }}>
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

        {!isMobile && (
          <Drawer
            variant="permanent"
            sx={{
              width: DRAWER_WIDTH,
              flexShrink: 0,
              "& .MuiDrawer-paper": { width: DRAWER_WIDTH },
            }}
          >
            {drawerContent}
          </Drawer>
        )}

        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <AppBar position="sticky" sx={{ ml: isMobile ? 0 : `${DRAWER_WIDTH}px` }}>
            <Toolbar>
              {isMobile && (
                <IconButton edge="start" onClick={() => setMobileOpen(true)} sx={{ mr: 1 }}>
                  <MenuIcon />
                </IconButton>
              )}
              <Typography variant="h6" sx={{ flex: 1, fontWeight: 500 }}>
                {NAV_ITEMS.find((item) =>
                  item.path === "/dashboard"
                    ? pathname === "/dashboard"
                    : pathname.startsWith(item.path),
                )?.label ?? "Dashboard"}
              </Typography>

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
              >
                <MenuItem disabled>
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

          <Box sx={{ flex: 1, p: 3, bgcolor: "background.default" }}>
            {children}
          </Box>
        </Box>
      </Box>
    </AuthGuard>
  );
}
