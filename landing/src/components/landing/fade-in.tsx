"use client";

import { useEffect, useRef, useState } from "react";

interface FadeInProps {
  children: React.ReactNode;
  className?: string;
  /** Delay in ms before starting animation (for staggered effects) */
  delay?: number;
  /** Animation variant */
  variant?: "fade-in-up" | "fade-in" | "slide-up";
}

/**
 * Scroll-triggered fade-in wrapper.
 * Children are invisible until scrolled into view, then animate in.
 * Respects prefers-reduced-motion.
 */
export function FadeIn({
  children,
  className = "",
  delay = 0,
  variant = "fade-in-up",
}: FadeInProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.unobserve(el);
        }
      },
      { threshold: 0.05, rootMargin: "0px 0px -40px 0px" },
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const animationClass = isVisible ? `animate-${variant}` : "opacity-0";

  return (
    <div
      ref={ref}
      className={`${className} ${animationClass}`}
      style={{
        transitionDelay: delay > 0 ? `${delay}ms` : undefined,
        animationDelay: delay > 0 ? `${delay}ms` : undefined,
      }}
    >
      {children}
    </div>
  );
}
