"use client";

import { useState, useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useTheme } from "next-themes";
import {
  LayoutDashboard,
  BarChart3,
  Activity,
  Users,
  MessageSquare,
  BrainCircuit,
  GitBranch,
  Shield,
  Key,
  FileJson,
  Tags,
  Database,
  Settings,
  Search,
  Sun,
  Moon,
  LogOut,
  Menu,
  ChevronLeft,
  ChevronRight,
  User as UserIcon,
  Webhook,
  FileText,
  FileCode,
  SlidersHorizontal,
  FolderKanban,
  MapPin,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE } from "@/lib/api-client";
import { Breadcrumb } from "@/components/breadcrumb";
import { usePinnedProjects } from "@/hooks/use-pinned-projects";

// ─── Helpers ───────────────────────────────────────────────────────────────────

/** Extract project ID from pathname like /projects/<id>/... */
function extractProjectId(pathname: string): string | null {
  const match = pathname.match(/^\/projects\/([^/]+)/);
  return match?.[1] ?? null;
}

/** Check if we are inside a project page */
function isInProject(pathname: string): boolean {
  return pathname.startsWith("/projects/");
}

/** Check if we are on the project list page (not inside a specific project) */
function isOnProjectList(pathname: string): boolean {
  return pathname === "/projects";
}

// ─── Sidebar ───────────────────────────────────────────────────────────────────

function Sidebar({
  collapsed,
  onToggle,
  onClose,
}: {
  collapsed: boolean;
  onToggle: () => void;
  onClose?: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();

  const inProject = isInProject(pathname);
  const onProjectList = isOnProjectList(pathname);
  const projectId = extractProjectId(pathname);
  const { pinned } = usePinnedProjects();

  const isActive = (href: string) => {
    if (href === "/overview") return pathname === "/overview";
    return pathname.startsWith(href);
  };

  return (
    <aside
      className={cn(
        "flex h-full flex-col border-r border-surface-800 bg-surface-900 transition-all duration-300",
        collapsed ? "w-16" : "w-56",
      )}
    >
      {/* Logo */}
      <div className="flex h-14 items-center justify-between px-4 border-b border-surface-800">
        {collapsed ? (
          <Link href="/overview" className="text-brand-500 font-bold text-xl mx-auto">
            O
          </Link>
        ) : (
          <Link href="/overview" className="flex items-center gap-2">
            <span className="text-brand-500 font-bold text-xl">O</span>
            <div>
              <div className="text-sm font-semibold text-[#F2F2F2] leading-tight">OpenZep</div>
              <div className="text-[10px] text-surface-400 leading-tight">Memory Infrastructure</div>
            </div>
          </Link>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-5">
        {/* ── Insights (hidden inside project pages) ── */}
        {!inProject && (
          <div>
            {!collapsed && (
              <div className="px-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                  Insights
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {[
                { label: "Overview", href: "/overview", icon: <LayoutDashboard size={18} /> },
                { label: "Analytics", href: "/analytics", icon: <BarChart3 size={18} /> },
                { label: "Monitoring", href: "/monitoring", icon: <Activity size={18} /> },
              ].map((item) => {
                const active = isActive(item.href);
                return (
                  <button
                    key={item.href}
                    onClick={() => { router.push(item.href); onClose?.(); }}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      active
                        ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                        : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                    )}
                  >
                    <span className={cn("shrink-0", active ? "text-brand-300" : "text-surface-400")}>
                      {item.icon}
                    </span>
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ── Projects / Pinned projects / View all ── */}
        <div>
          {!collapsed && (
            <div className="px-2 mb-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                Projects
              </span>
            </div>
          )}
          <div className="space-y-0.5">
            {/* Pinned projects (always visible when any are pinned) */}
            {pinned.map((p) => {
              const isActiveProject = pathname.startsWith(`/projects/${p.id}`);
              return (
                <button
                  key={p.id}
                  onClick={() => { router.push(`/projects/${p.id}/sessions`); onClose?.(); }}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                    collapsed && "justify-center px-0",
                    isActiveProject
                      ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                      : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                  )}
                >
                  <span className={cn("shrink-0", isActiveProject ? "text-brand-300" : "text-surface-400")}>
                    <MapPin size={18} />
                  </span>
                  {!collapsed && <span className="truncate">{p.name}</span>}
                </button>
              );
            })}

            {/* View all projects */}
            <button
              onClick={() => { router.push("/projects"); onClose?.(); }}
              className={cn(
                "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                collapsed && "justify-center px-0",
                onProjectList
                  ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                  : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
              )}
            >
              <span className="shrink-0 text-surface-400">
                <FolderKanban size={18} />
              </span>
              {!collapsed && <span className="truncate">View all projects</span>}
            </button>

            {/* Project-scoped nav items (only inside a project) */}
            {inProject && (
              <>
                {!collapsed && (
                  <div className="my-1 border-t border-surface-800" />
                )}
                {[
                  { label: "Sessions", href: `/projects/${projectId}/sessions`, icon: <MessageSquare size={18} /> },
                  { label: "Memory", href: `/projects/${projectId}/memory`, icon: <BrainCircuit size={18} /> },
                  { label: "Graph Explorer", href: `/projects/${projectId}/graph`, icon: <GitBranch size={18} /> },
                  { label: "Communities", href: `/projects/${projectId}/graph/communities`, icon: <Shield size={18} /> },
                ].map((item) => {
                  const active = isActive(item.href);
                  return (
                    <button
                      key={item.href}
                      onClick={() => { router.push(item.href); onClose?.(); }}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                        collapsed && "justify-center px-0",
                        active
                          ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                          : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                      )}
                    >
                      <span className={cn("shrink-0", active ? "text-brand-300" : "text-surface-400")}>
                        {item.icon}
                      </span>
                      {!collapsed && <span className="truncate">{item.label}</span>}
                    </button>
                  );
                })}
              </>
            )}
          </div>
        </div>

        {/* ── Project Settings (only visible inside a project) ── */}
        {inProject && (
          <div>
            {!collapsed && (
              <div className="px-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                  Project Settings
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {[
                { label: "Members", href: `/projects/${projectId}/members`, icon: <Users size={18} /> },
                { label: "Settings", href: `/projects/${projectId}/settings`, icon: <Settings size={18} /> },
              ].map((item) => {
                const active = isActive(item.href);
                return (
                  <button
                    key={item.href}
                    onClick={() => { router.push(item.href); onClose?.(); }}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      active
                        ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                        : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                    )}
                  >
                    <span className={cn("shrink-0", active ? "text-brand-300" : "text-surface-400")}>
                      {item.icon}
                    </span>
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ── Administration (hidden inside project pages) ── */}
        {!inProject && (
          <div>
            {!collapsed && (
              <div className="px-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                  Administration
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {[
                { label: "Users", href: "/users", icon: <Users size={18} /> },
                { label: "API Keys", href: "/settings/api-keys", icon: <Key size={18} /> },
                { label: "Extraction Schemas", href: "/settings/schemas", icon: <FileJson size={18} /> },
                { label: "Classifications", href: "/settings/classifications", icon: <Tags size={18} /> },
                { label: "Extractions", href: "/settings/extractions", icon: <Database size={18} /> },
                { label: "Webhooks", href: "/settings/webhooks", icon: <Webhook size={18} /> },
                { label: "Extraction Instructions", href: "/settings/extraction-instructions", icon: <FileText size={18} /> },
                { label: "Prompts", href: "/settings/prompts", icon: <FileCode size={18} /> },
                { label: "Org Config", href: "/settings/org-config", icon: <SlidersHorizontal size={18} /> },
              ].map((item) => {
                const active = isActive(item.href);
                return (
                  <button
                    key={item.href}
                    onClick={() => { router.push(item.href); onClose?.(); }}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      active
                        ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                        : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                    )}
                  >
                    <span className={cn("shrink-0", active ? "text-brand-300" : "text-surface-400")}>
                      {item.icon}
                    </span>
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ── System (hidden inside project pages) ── */}
        {!inProject && (
          <div>
            {!collapsed && (
              <div className="px-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                  System
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {[
                { label: "Audit Log", href: "/audit", icon: <Shield size={18} /> },
                { label: "Settings", href: "/settings", icon: <Settings size={18} /> },
              ].map((item) => {
                const active = isActive(item.href);
                return (
                  <button
                    key={item.href}
                    onClick={() => { router.push(item.href); onClose?.(); }}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      active
                        ? "bg-brand-500/10 text-brand-300 border-l-[3px] border-brand-500"
                        : "text-surface-300 hover:bg-surface-800 hover:text-[#F2F2F2] border-l-[3px] border-transparent",
                    )}
                  >
                    <span className={cn("shrink-0", active ? "text-brand-300" : "text-surface-400")}>
                      {item.icon}
                    </span>
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </nav>

      {/* Bottom section */}
      <div className="border-t border-surface-800 p-2">
        {collapsed ? (
          <button
            onClick={onToggle}
            className="flex w-full items-center justify-center rounded-md p-2 text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]"
          >
            <ChevronRight size={16} />
          </button>
        ) : (
          <button
            onClick={onToggle}
            className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]"
          >
            <ChevronLeft size={16} />
            <span>Collapse</span>
          </button>
        )}
      </div>
    </aside>
  );
}

// ─── Dashboard Layout ─────────────────────────────────────────────────────────

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [currentUserLabel, setCurrentUserLabel] = useState("User");

  const inProject = isInProject(pathname);
  const projectId = extractProjectId(pathname);
  const [projectName, setProjectName] = useState<string | null>(null);

  // Fetch project name when inside a project
  useEffect(() => {
    if (projectId) {
      const token = sessionStorage.getItem("mg_access_token");
      if (!token) return;
      fetch(`${API_BASE}/v1/projects/${projectId}`, {
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => setProjectName(data?.name ?? null))
        .catch(() => setProjectName(null));
    } else {
      setProjectName(null);
    }
  }, [projectId]);

  useEffect(() => {
    setMounted(true);
    // Fetch the current user's email from the API
    const token = sessionStorage.getItem("mg_access_token");
    if (!token) return;
    try {
      const payload = JSON.parse(atob(token.split(".")[1]));
      const userId = payload.sub;
      if (userId) {
        fetch(`${API_BASE}/v1/users/${userId}`, {
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
        })
          .then((r) => (r.ok ? r.json() : null))
          .then((user) => {
            if (user?.email) setCurrentUserLabel(user.email);
            else setCurrentUserLabel(user?.name || userId.slice(0, 8));
          })
          .catch(() => setCurrentUserLabel(userId.slice(0, 8)));
      }
    } catch {
      setCurrentUserLabel("User");
    }
  }, []);

  // Build breadcrumb items from the current pathname
  const breadcrumbItems = (() => {
    // Project pages
    if (pathname.startsWith("/projects/")) {
      const pageLabel = (() => {
        if (pathname.endsWith("/sessions")) return "Sessions";
        if (pathname.endsWith("/memory")) return "Memory";
        if (pathname.includes("/graph/communities")) return "Communities";
        if (pathname.endsWith("/graph")) return "Graph Explorer";
        if (pathname.endsWith("/members")) return "Members";
        if (pathname.endsWith("/settings")) return "Project Settings";
        if (pathname.match(/\/sessions\/[^/]+$/)) return "Session";
        if (pathname.includes("/messages")) return "Messages";
        if (pathname.includes("/facts")) return "Facts";
        if (pathname.includes("/classifications")) return "Classifications";
        if (pathname.includes("/extractions")) return "Extractions";
        return "Project";
      })();
      return [
        { label: "Projects", href: "/projects" },
        ...(projectName ? [{ label: projectName }] : []),
        { label: pageLabel },
      ];
    }

    // Non-project pages
    if (pathname === "/projects") return [{ label: "Projects" }];
    if (pathname === "/overview") return [{ label: "Insights" }, { label: "Overview" }];
    if (pathname === "/analytics") return [{ label: "Insights" }, { label: "Analytics" }];
    if (pathname.startsWith("/monitoring")) return [{ label: "Insights" }, { label: "Monitoring" }];
    if (pathname.startsWith("/users")) return [{ label: "Administration" }, { label: "Users" }];
    if (pathname.startsWith("/audit")) return [{ label: "System" }, { label: "Audit Log" }];
    if (pathname.startsWith("/settings")) {
      if (pathname.includes("/api-keys")) return [{ label: "Administration" }, { label: "API Keys" }];
      if (pathname.includes("/schemas")) return [{ label: "Administration" }, { label: "Extraction Schemas" }];
      if (pathname.includes("/classifications")) return [{ label: "Administration" }, { label: "Classifications" }];
      if (pathname.includes("/extractions")) return [{ label: "Administration" }, { label: "Extractions" }];
      if (pathname.includes("/webhooks")) return [{ label: "Administration" }, { label: "Webhooks" }];
      if (pathname.includes("/extraction-instructions")) return [{ label: "Administration" }, { label: "Extraction Instructions" }];
      if (pathname.includes("/prompts")) return [{ label: "Administration" }, { label: "Prompts" }];
      if (pathname.includes("/org-config")) return [{ label: "Administration" }, { label: "Org Config" }];
      return [{ label: "System" }, { label: "Settings" }];
    }
    return [];
  })();

  // Dynamic page title
  const pageTitle = (() => {
    // Project pages
    if (pathname.startsWith("/projects/")) {
      if (pathname.endsWith("/sessions")) return "Sessions";
      if (pathname.endsWith("/memory")) return "Memory";
      if (pathname.includes("/graph/communities")) return "Communities";
      if (pathname.endsWith("/graph")) return "Graph Explorer";
      if (pathname.endsWith("/members")) return "Members";
      if (pathname.endsWith("/settings")) return "Project Settings";
      if (pathname.match(/\/sessions\/[^/]+$/)) return "Session";
      if (pathname.includes("/messages")) return "Messages";
      if (pathname.includes("/facts")) return "Facts";
      if (pathname.includes("/classifications")) return "Classifications";
      if (pathname.includes("/extractions")) return "Extractions";
      return "Project";
    }
    if (pathname === "/projects") return "Projects";
    if (pathname === "/overview") return "Overview";
    if (pathname === "/analytics") return "Analytics";
    if (pathname.startsWith("/monitoring")) return "Monitoring";
    if (pathname.startsWith("/users")) return "Users";
    if (pathname.startsWith("/audit")) return "Audit Log";
    if (pathname.startsWith("/settings")) {
      if (pathname.includes("/api-keys")) return "API Keys";
      if (pathname.includes("/schemas")) return "Extraction Schemas";
      if (pathname.includes("/classifications")) return "Classifications";
      if (pathname.includes("/extractions")) return "Extractions";
      if (pathname.includes("/webhooks")) return "Webhooks";
      if (pathname.includes("/extraction-instructions")) return "Extraction Instructions";
      if (pathname.includes("/prompts")) return "Prompts";
      if (pathname.includes("/org-config")) return "Org Config";
      return "Settings";
    }
    return "Dashboard";
  })();

  return (
    <div className="flex h-screen overflow-hidden bg-surface-950">
      {/* Mobile sidebar overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile sidebar */}
      <div
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-56 transition-transform duration-300 lg:hidden",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <Sidebar collapsed={false} onToggle={() => {}} onClose={() => setMobileOpen(false)} />
      </div>

      {/* Desktop sidebar */}
      <div className="hidden lg:block">
        <Sidebar
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
        />
      </div>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top bar */}
        <header className="flex h-14 items-center gap-4 border-b border-surface-800 bg-surface-950/80 px-4 backdrop-blur-md">
          {/* Mobile menu toggle */}
          <button
            onClick={() => setMobileOpen(true)}
            className="lg:hidden rounded-md p-1.5 text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]"
          >
            <Menu size={20} />
          </button>

          {/* Page title */}
          <div className="hidden sm:flex items-center gap-2 text-sm">
            <span className="text-[#F2F2F2] font-medium">{pageTitle}</span>
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Search */}
          <button className="rounded-md p-1.5 text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]">
            <Search size={18} />
          </button>

          {/* Theme toggle */}
          <button
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            className="rounded-md p-1.5 text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]"
          >
            {!mounted ? (
              <div className="h-[18px] w-[18px]" />
            ) : theme === "dark" ? (
              <Sun size={18} />
            ) : (
              <Moon size={18} />
            )}
          </button>

          {/* User avatar */}
          <div className="relative">
            <button
              onClick={() => setUserMenuOpen(!userMenuOpen)}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-500 text-xs font-bold text-white"
            >
              U
            </button>
            {userMenuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setUserMenuOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-20 w-48 rounded-lg border border-surface-800 bg-surface-900 p-1 shadow-lg shadow-black/30 animate-slide-up">
                  <div className="px-2 py-1.5 text-sm text-surface-400 border-b border-surface-800 mb-1">
                    {currentUserLabel}
                  </div>
                  <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-surface-200 hover:bg-surface-800">
                    <UserIcon size={14} />
                    Profile
                  </button>
                  <button
                    onClick={() => {
                      setUserMenuOpen(false);
                      router.push("/settings");
                    }}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-surface-200 hover:bg-surface-800"
                  >
                    <Settings size={14} />
                    Settings
                  </button>
                  <hr className="my-1 border-surface-800" />
                  <button
                    onClick={() => {
                      setUserMenuOpen(false);
                      sessionStorage.removeItem("mg_access_token");
                      sessionStorage.removeItem("mg_refresh_token");
                      router.push("/login");
                    }}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-error hover:bg-surface-800"
                  >
                    <LogOut size={14} />
                    Sign Out
                  </button>
                </div>
              </>
            )}
          </div>
        </header>

        {/* Breadcrumb */}
        {breadcrumbItems.length > 0 && (
          <div className="flex items-center px-6 py-2 border-b border-surface-800 bg-surface-950/40">
            <Breadcrumb items={breadcrumbItems} />
          </div>
        )}

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-6">
          <div className="mx-auto max-w-7xl animate-fade-in">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
