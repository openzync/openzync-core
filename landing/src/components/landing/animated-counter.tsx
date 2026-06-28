"use client";

import { useEffect, useRef, useState } from "react";

interface AnimatedCounterProps {
  value: string;
  /** Delay in ms before starting the animation */
  delay?: number;
}

/**
 * Scroll-triggered animated counter.
 * Parses the numeric prefix from `value` (e.g. "10+" → 10) and animates
 * from 0 → target using requestAnimationFrame. Preserves any suffix ("+", "K").
 */
export function AnimatedCounter({ value, delay = 0 }: AnimatedCounterProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [displayed, setDisplayed] = useState("0");
  const [started, setStarted] = useState(false);
  const parsed = parseNumericValue(value);

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
      { threshold: 0.3 },
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!isVisible || started) return;

    const timer = setTimeout(() => {
      setStarted(true);
      const startTime = performance.now();
      const duration = 800; // ms

      function animate(currentTime: number) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(eased * parsed.numeric);

        setDisplayed(formatDisplay(current, parsed.suffix));

        if (progress < 1) {
          requestAnimationFrame(animate);
        }
      }

      requestAnimationFrame(animate);
    }, delay);

    return () => clearTimeout(timer);
  }, [isVisible, delay, parsed.numeric, parsed.suffix, started]);

  return (
    <span ref={ref} className={isVisible ? "animate-count-up" : "opacity-0"}>
      {displayed}
    </span>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

interface ParsedValue {
  numeric: number;
  suffix: string;
}

function parseNumericValue(value: string): ParsedValue {
  const match = value.match(/^([\d.]+)(.*)$/);
  if (!match) return { numeric: 0, suffix: value };
  return {
    numeric: Number.parseFloat(match[1]) || 0,
    suffix: match[2] || "",
  };
}

function formatDisplay(value: number, suffix: string): string {
  return `${value.toLocaleString()}${suffix}`;
}
