"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { siteConfig, navItems } from "@/content/site-config";
import { Button } from "@openzync/design-system";
import { MegaMenu } from "./mega-menu";

/**
 * Landing page navigation bar with mega-menu dropdowns (Plone style).
 * - Desktop: mega-menu with hover/click dropdowns
 * - Mobile (<1024px): accordion drawer
 */
export function Navbar() {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [mobileExpanded, setMobileExpanded] = useState<string | null>(null);
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  // Close mobile menu on route change
  useEffect(() => {
    setMobileOpen(false);
    setMobileExpanded(null);
  }, [pathname]);

  // Track announcement-bar height so the navbar re-renders when it changes.
  // (React does not re-evaluate `style` props when CSS custom properties change.)
  const [topOffset, setTopOffset] = useState("0px");
  useEffect(() => {
    const update = () => {
      const h = getComputedStyle(document.documentElement)
        .getPropertyValue("--announcement-height")
        .trim();
      setTopOffset(h || "0px");
    };
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["style"],
    });
    return () => observer.disconnect();
  }, []);

  // Prevent body scroll when mobile menu is open
  // Uses ref-based save/restore to avoid clobbering other scroll locks
  const prevOverflowRef = useRef<string | null>(null);

  useEffect(() => {
    if (mobileOpen) {
      prevOverflowRef.current = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = prevOverflowRef.current ?? "";
      prevOverflowRef.current = null;
    }

    const handleBeforeUnload = () => {
      document.body.style.overflow = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);

    return () => {
      document.body.style.overflow = prevOverflowRef.current ?? "";
      window.removeEventListener("beforeunload", handleBeforeUnload);
    };
  }, [mobileOpen]);

  return (
    <header
      className={cn(
        "fixed left-0 right-0 z-40 transition-all duration-300",
        scrolled
          ? "bg-surface-950/95 border-b border-surface-800"
          : "bg-transparent",
      )}
      style={{ top: topOffset }}
    >
      <div className="mx-auto max-w-7xl flex h-16 items-center justify-between px-6">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-2">
          <span className="text-brand-500 font-bold text-2xl">O</span>
          <div>
            <div className="text-sm font-semibold text-text-primary leading-tight">
              {siteConfig.name}
            </div>
            <div className="text-[10px] text-surface-400 leading-tight hidden sm:block">
              {siteConfig.tagline}
            </div>
          </div>
        </Link>

        {/* Desktop mega-menu */}
        <MegaMenu items={navItems} />

        {/* Desktop CTA */}
        <div className="hidden lg:flex items-center gap-3">
          <Link
            href={`${siteConfig.appUrl}/login`}
            className="text-sm text-surface-300 hover:text-text-primary transition-colors"
          >
            Sign In
          </Link>
          <Link href={`${siteConfig.appUrl}/signup`}>
            <Button variant="primary" size="md">
              Get Started
            </Button>
          </Link>
        </div>

        {/* Mobile hamburger */}
        <button
          onClick={() => setMobileOpen(true)}
          className="lg:hidden rounded-md p-2 text-surface-400 hover:bg-surface-800 hover:text-text-primary"
          aria-label="Open menu"
        >
          <Menu size={22} />
        </button>
      </div>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile sidebar */}
      <div
        className={cn(
          "fixed inset-y-0 right-0 z-50 w-72 bg-surface-900 border-l border-surface-800 lg:hidden",
          "transition-transform duration-300 ease-in-out overflow-y-auto",
          mobileOpen ? "translate-x-0" : "translate-x-full",
        )}
      >
        <div className="flex items-center justify-between h-16 px-6 border-b border-surface-800">
          <span className="text-sm font-semibold text-text-primary">Menu</span>
          <button
            onClick={() => setMobileOpen(false)}
            className="rounded-md p-2 text-surface-400 hover:bg-surface-800 hover:text-text-primary"
            aria-label="Close menu"
          >
            <X size={20} />
          </button>
        </div>

        <div className="p-4 space-y-1">
          {navItems.map((item) => {
            const hasChildren = item.children && item.children.length > 0;
            const isExpanded = mobileExpanded === item.label;

            return (
              <div key={item.label}>
                <button
                  onClick={() => {
                    if (hasChildren) {
                      setMobileExpanded(isExpanded ? null : item.label);
                    } else if (item.href) {
                      window.location.href = item.href;
                    }
                  }}
                  className={cn(
                    "flex w-full items-center justify-between rounded-md px-3 py-2.5 text-sm font-medium transition-colors",
                    "text-surface-300 hover:text-text-primary hover:bg-surface-800",
                  )}
                >
                  <span>{item.label}</span>
                  {hasChildren && (
                    <ChevronDown
                      size={14}
                      className={cn("transition-transform", isExpanded && "rotate-180")}
                    />
                  )}
                </button>

                {hasChildren && isExpanded && (
                  <div className="ml-3 mt-1 space-y-0.5 border-l border-surface-800 pl-3">
                    {item.children!.map((child) => (
                      <Link
                        key={child.label}
                        href={child.href}
                        className="block rounded-md px-3 py-2 text-sm text-surface-400 hover:text-text-primary hover:bg-surface-800 transition-colors"
                      >
                        {child.label}
                      </Link>
                    ))}
                    {item.highlights && item.highlights.length > 0 && (
                      <>
                        <hr className="my-2 border-surface-800" />
                        {item.highlights.map((h) => (
                          <Link
                            key={h.label}
                            href={h.href}
                            className="block rounded-md px-3 py-2 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
                          >
                            {h.label}
                          </Link>
                        ))}
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          <hr className="my-4 border-surface-800" />
          <Link
            href={`${siteConfig.appUrl}/login`}
            className="flex w-full rounded-md px-3 py-2.5 text-sm text-surface-300 hover:text-text-primary hover:bg-surface-800 transition-colors"
          >
            Sign In
          </Link>
          <Link href={`${siteConfig.appUrl}/signup`} className="block mt-2">
            <Button variant="primary" size="md" className="w-full">
              Get Started
            </Button>
          </Link>
        </div>
      </div>
    </header>
  );
}
