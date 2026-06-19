"use client";

import { useState, useEffect, useCallback } from "react";

export interface PinnedProject {
  id: string;
  name: string;
}

const STORAGE_KEY = "mg_pinned_projects";
const STORAGE_EVENT = "mg_pinned_projects_changed";
const MAX_PINS = 3;

function loadPinned(): PinnedProject[] {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const parsed = JSON.parse(stored) as PinnedProject[];
      if (Array.isArray(parsed)) return parsed;
    }
  } catch {
    // Corrupt data — ignore
  }
  return [];
}

export function usePinnedProjects() {
  const [pinned, setPinned] = useState<PinnedProject[]>([]);

  // Load from localStorage on mount + listen for cross-component changes
  useEffect(() => {
    setPinned(loadPinned());

    const handler = () => setPinned(loadPinned());
    window.addEventListener(STORAGE_EVENT, handler);
    return () => window.removeEventListener(STORAGE_EVENT, handler);
  }, []);

  const togglePin = useCallback((id: string, name: string) => {
    setPinned((prev) => {
      const exists = prev.find((p) => p.id === id);
      let next: PinnedProject[];
      if (exists) {
        // Unpin
        next = prev.filter((p) => p.id !== id);
      } else if (prev.length >= MAX_PINS) {
        // Already at max — no-op
        return prev;
      } else {
        // Pin
        next = [...prev, { id, name }];
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      window.dispatchEvent(new Event(STORAGE_EVENT));
      return next;
    });
  }, []);

  const isPinned = useCallback(
    (id: string) => pinned.some((p) => p.id === id),
    [pinned],
  );

  const isMaxPinned = pinned.length >= MAX_PINS;

  return { pinned, togglePin, isPinned, isMaxPinned };
}
