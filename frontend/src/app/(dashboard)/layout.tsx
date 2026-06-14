"use client";

import { useState, useEffect, useCallback } from "react";
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
  Bell,
  User as UserIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Navigation items ─────────────────────────────────────────────────────────

interface NavSection {
  label: string;
  items: { label: string; href: string; icon: React.ReactNode }[];
}

const NAV_SECTIONS: NavSection[] = [
  {
    label: "Insights",
    items: [
      { label: "Overview", href: "/overview", icon: <LayoutDashboard size={18} /> },
      { label: "Analytics", href: "/analytics", icon: <BarChart3 size={18} /> },
      { label: "Monitoring", href: "/monitoring", icon: <Activity size={18} /> },
    ],
  },
  {
    label: "Data",
    items: [
      { label: "Users", href: "/users", icon: <Users size={18} /> },
      { label: "Sessions", href: "/sessions", icon: <MessageSquare size={18} /> },
      { label: "Memory", href: "/memory", icon: <BrainCircuit size={18} /> },
    ],
  },
  {
    label: "Knowledge Graph",
    items: [
      { label: "Graph Explorer", href: "/graph", icon: <GitBranch size={18} /> },
      { label: "Communities", href: "/graph/communities", icon: <Shield size={18} /> },
      { label: "Facts", href: "/graph/facts", icon: <Database size={18} /> },
    ],
  },
  {
    label: "Configuration",
    items: [
      { label: "API Keys", href: "/settings/api-keys", icon: <Key size={18} /> },
      { label: "Extraction Schemas", href: "/settings/schemas", icon: <FileJson size={18} /> },
      { label: "Classifications", href: "/settings/classifications", icon: <Tags size={18} /> },
      { label: "Extractions", href: "/settings/extractions", icon: <Database size={18} /> },
    ],
  },
  {
    label: "System",
    items: [
      { label: "Audit Log", href: "/audit", icon: <Shield size={18} /> },
      { label: "Settings", href: "/settings", icon: <Settings size={18} /> },
    ],
  },
];

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
        {NAV_SECTIONS.map((section) => (
          <div key={section.label}>
            {!collapsed && (
              <div className="px-2 mb-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-surface-500">
                  {section.label}
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {section.items.map((item) => {
                const active = isActive(item.href);
                return (
                  <button
                    key={item.href}
                    onClick={() => {
                      router.push(item.href);
                      onClose?.();
                    }}
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
        ))}
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

  useEffect(() => { setMounted(true); }, []);

  // Get page title from nav sections
  const pageTitle = (() => {
    for (const section of NAV_SECTIONS) {
      for (const item of section.items) {
        if (item.href === "/overview" ? pathname === "/overview" : pathname.startsWith(item.href)) {
          return item.label;
        }
      }
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

          {/* Breadcrumb */}
          <nav className="hidden sm:flex items-center gap-2 text-sm">
            {pathname.split("/").filter(Boolean).map((segment, i, arr) => (
              <span key={segment} className="flex items-center gap-2">
                {i > 0 && <span className="text-surface-600">/</span>}
                <span className={i === arr.length - 1 ? "text-[#F2F2F2] font-medium" : "text-surface-400"}>
                  {segment.charAt(0).toUpperCase() + segment.slice(1).replace(/-/g, " ")}
                </span>
              </span>
            ))}
          </nav>

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
                    admin@openzep.dev
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
                  <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-error hover:bg-surface-800">
                    <LogOut size={14} />
                    Sign Out
                  </button>
                </div>
              </>
            )}
          </div>
        </header>

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
