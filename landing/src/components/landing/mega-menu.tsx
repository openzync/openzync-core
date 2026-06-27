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

  return (
    <nav className="hidden lg:flex items-center gap-0.5">
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
                <div className="absolute top-full left-0 pt-2 z-50">
                  <div className="w-72 rounded-xl border border-surface-700 bg-surface-900 shadow-2xl shadow-black/40 overflow-hidden">
                    {/* Children links */}
                    <div className="p-2 space-y-0.5">
                      {item.children!.map((child) => (
                        <Link
                          key={child.label}
                          href={child.href}
                          className="flex flex-col rounded-lg px-3 py-2.5 hover:bg-surface-800 transition-colors group"
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
