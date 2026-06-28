"use client";

import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import type { NavItem } from "@/content/site-config";

interface MegaMenuProps {
  items: NavItem[];
}

/**
 * Mega-menu navigation for desktop.
 * Each top-level item opens a dropdown with children + optional highlights.
 */
export function MegaMenu({ items }: MegaMenuProps) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleMouseEnter = (index: number) => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setHoveredIndex(index);
    setActiveIndex(index);
  };

  const handleMouseLeave = () => {
    timeoutRef.current = setTimeout(() => {
      setHoveredIndex(null);
      setActiveIndex(null);
    }, 150);
  };

  const handleClick = (index: number) => {
    setActiveIndex(activeIndex === index ? null : index);
  };

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const dropdownRefs = useRef<(HTMLDivElement | null)[]>([]);

  /**
   * Keyboard navigation for mega-menu.
   * ArrowDown: open next dropdown. ArrowRight/Left: move between top-level items.
   * Escape: close current dropdown.
   */
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (activeIndex === null) {
      // No dropdown open — ArrowDown opens first item if it has children
      if (e.key === "ArrowDown") {
        const firstChild = items.findIndex((it) => it.children && it.children.length > 0);
        if (firstChild >= 0) {
          e.preventDefault();
          setActiveIndex(firstChild);
        }
      }
      return;
    }

    const current = items[activeIndex];
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        // Focus the first child link inside the open dropdown
        if (current?.children && current.children.length > 0) {
          const dropdown = dropdownRefs.current[activeIndex];
          if (dropdown) {
            const firstLink = dropdown.querySelector<HTMLAnchorElement>("a");
            firstLink?.focus();
          }
        }
        break;

      case "ArrowRight":
        e.preventDefault();
        setActiveIndex((activeIndex + 1) % items.length);
        break;

      case "ArrowLeft":
        e.preventDefault();
        setActiveIndex((activeIndex - 1 + items.length) % items.length);
        break;

      case "Escape":
        e.preventDefault();
        setActiveIndex(null);
        setHoveredIndex(null);
        break;
    }
  };

  return (
    <nav
      className="hidden lg:flex items-center gap-0.5"
      onKeyDown={handleKeyDown}
    >
      {items.map((item, i) => {
        const isOpen = activeIndex === i;
        const hasChildren = item.children && item.children.length > 0;

        return (
          <div
            key={item.label}
            className="relative"
            onMouseEnter={() => handleMouseEnter(i)}
            onMouseLeave={handleMouseLeave}
          >
            {item.href ? (
              <Link
                href={item.href}
                onFocus={() => !hoveredIndex && setActiveIndex(i)}
                className={cn(
                  "flex items-center gap-1 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isOpen
                    ? "text-brand-300 bg-brand-500/10"
                    : "text-surface-300 hover:text-text-primary hover:bg-surface-800",
                )}
              >
                {item.label}
                {hasChildren && (
                  <ChevronDown
                    size={14}
                    className={cn("transition-transform duration-200", isOpen && "rotate-180")}
                  />
                )}
              </Link>
            ) : (
              <button
                onClick={() => handleClick(i)}
                onFocus={() => !hoveredIndex && setActiveIndex(i)}
                className={cn(
                  "flex items-center gap-1 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isOpen
                    ? "text-brand-300 bg-brand-500/10"
                    : "text-surface-300 hover:text-text-primary hover:bg-surface-800",
                )}
              >
                {item.label}
                <ChevronDown
                  size={14}
                  className={cn("transition-transform duration-200", isOpen && "rotate-180")}
                />
              </button>
            )}

            {/* Dropdown */}
            {hasChildren && isOpen && (
              <>
                <div
                  ref={(el) => { dropdownRefs.current[i] = el; }}
                  className="absolute top-full left-0 pt-2 z-50"
                >
                  <div className="w-72 rounded-xl border border-surface-700 bg-surface-900 shadow-xl overflow-hidden">
                    {/* Children links */}
                    <div className="p-2 space-y-0.5">
                      {item.children!.map((child, ci) => (
                        <Link
                          key={child.label}
                          href={child.href}
                          className="flex flex-col rounded-lg px-3 py-2.5 hover:bg-surface-800 transition-colors group"
                          // Allow Tab within the dropdown, focus first child on ArrowDown
                          tabIndex={0}
                        >
                          <span className="text-sm font-medium text-text-primary group-hover:text-brand-300 transition-colors">
                            {child.label}
                          </span>
                          {child.description && (
                            <span className="text-xs text-surface-500 mt-0.5">
                              {child.description}
                            </span>
                          )}
                        </Link>
                      ))}
                    </div>

                    {/* Highlights */}
                    {item.highlights && item.highlights.length > 0 && (
                      <div className="border-t border-surface-800 p-2 bg-surface-950/50">
                        {item.highlights.map((h) => (
                          <Link
                            key={h.label}
                            href={h.href}
                            className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium text-brand-300 hover:bg-brand-500/10 transition-colors"
                          >
                            <span className="h-1.5 w-1.5 rounded-full bg-brand-500" />
                            {h.label}
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        );
      })}
    </nav>
  );
}
