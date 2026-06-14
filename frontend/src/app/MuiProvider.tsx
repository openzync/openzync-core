"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { ThemeProvider } from "@mui/material/styles";
import useMediaQuery from "@mui/material/useMediaQuery";
import CssBaseline from "@mui/material/CssBaseline";
import { darkTheme, lightTheme } from "@/theme";

// ─── Theme Context ─────────────────────────────────────────────────────────────

type ThemeMode = "dark" | "light";

interface ThemeContextValue {
  mode: ThemeMode;
  toggleTheme: () => void;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  mode: "dark",
  toggleTheme: () => {},
  setMode: () => {},
});

export const useThemeMode = () => useContext(ThemeContext);

const STORAGE_KEY = "mg_theme_mode";

function getStoredMode(): ThemeMode | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    // localStorage unavailable
  }
  return null;
}

function setStoredMode(mode: ThemeMode) {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // localStorage unavailable
  }
}

// ─── Provider ──────────────────────────────────────────────────────────────────

export function MuiProvider({ children }: { children: React.ReactNode }) {
  const prefersDark = useMediaQuery("(prefers-color-scheme: dark)", {
    noSsr: true,
  });

  const [mode, setMode] = useState<ThemeMode>("dark");

  // Initialise from localStorage or system preference
  useEffect(() => {
    const stored = getStoredMode();
    if (stored) {
      setMode(stored);
    } else {
      setMode(prefersDark ? "dark" : "light");
    }
  }, [prefersDark]);

  const toggleTheme = () => {
    setMode((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      setStoredMode(next);
      return next;
    });
  };

  const setModeWithPersist = (newMode: ThemeMode) => {
    setMode(newMode);
    setStoredMode(newMode);
  };

  const theme = mode === "dark" ? darkTheme : lightTheme;

  const contextValue = useMemo(
    () => ({ mode, toggleTheme, setMode: setModeWithPersist }),
    [mode],
  );

  return (
    <ThemeContext.Provider value={contextValue}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </ThemeProvider>
    </ThemeContext.Provider>
  );
}
