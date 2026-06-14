"use client";

import { createTheme, type Theme } from "@mui/material/styles";
import type {} from "@mui/x-data-grid/themeAugmentation";

// ─── Brand Palette ─────────────────────────────────────────────────────────────
const BRAND = {
  primary: "#14488C",
  primaryLight: "#1453A6",
  primaryDark: "#1747A6",
  accent: "#8FAFD9",
  accentLight: "#A9C6E8",
  accentDark: "#6A8DB8",
  textOnDark: "#F2F2F2",
  bgDark: "#0D1117",
  surfaceDark: "#161B22",
  surfaceDarkAlt: "#1A2332",
} as const;

// ─── Shared component overrides (applied to both themes) ──────────────────────

const sharedComponents = {
  MuiButton: {
    defaultProps: { disableElevation: true },
    styleOverrides: {
      root: {
        textTransform: "none",
        fontWeight: 600,
        padding: "8px 20px",
        borderRadius: 6,
      },
      contained: {
        "&:hover": {
          boxShadow: "0 4px 12px rgba(20,72,140,0.3)",
        },
      },
      sizeLarge: {
        padding: "12px 24px",
        fontSize: "1rem",
      },
    },
  },
  MuiCard: {
    styleOverrides: {
      root: {
        borderRadius: 10,
      },
    },
  },
  MuiPaper: {
    styleOverrides: {
      root: {
        backgroundImage: "none",
      },
    },
  },
  MuiTextField: {
    defaultProps: {
      variant: "outlined",
      size: "small",
    },
  },
  MuiDialog: {
    styleOverrides: {
      paper: {
        borderRadius: 10,
      },
    },
  },
  MuiChip: {
    styleOverrides: {
      root: {
        fontWeight: 500,
      },
    },
  },
  MuiTooltip: {
    styleOverrides: {
      tooltip: {
        borderRadius: 6,
        padding: "6px 10px",
        fontSize: "0.75rem",
      },
    },
  },
  MuiToggleButton: {
    styleOverrides: {
      root: {
        textTransform: "none",
        fontWeight: 500,
      },
    },
  },
} as const;

// ─── Dark Theme ────────────────────────────────────────────────────────────────

export const darkTheme: Theme = createTheme({
  palette: {
    mode: "dark",
    primary: {
      main: BRAND.primary,
      light: BRAND.primaryLight,
      dark: BRAND.primaryDark,
      contrastText: BRAND.textOnDark,
    },
    secondary: {
      main: BRAND.accent,
      light: BRAND.accentLight,
      dark: BRAND.accentDark,
      contrastText: "#0D1117",
    },
    background: {
      default: BRAND.bgDark,
      paper: BRAND.surfaceDark,
    },
    text: {
      primary: BRAND.textOnDark,
      secondary: "rgba(242,242,242,0.7)",
      disabled: "rgba(242,242,242,0.38)",
    },
    divider: "rgba(143,175,217,0.12)",
    error: {
      main: "#EF5350",
      light: "#E57373",
      dark: "#D32F2F",
    },
    success: {
      main: "#66BB6A",
      light: "#81C784",
      dark: "#388E3C",
    },
    warning: {
      main: "#FFA726",
      light: "#FFB74D",
      dark: "#F57C00",
    },
    info: {
      main: BRAND.accent,
    },
  },
  typography: {
    fontFamily: '"Roboto", "Helvetica", "Arial", sans-serif',
    h4: { fontWeight: 600, fontSize: "1.5rem", lineHeight: 1.2 },
    h5: { fontWeight: 600, fontSize: "1.25rem", lineHeight: 1.3 },
    h6: { fontWeight: 600, fontSize: "1.1rem", lineHeight: 1.4 },
    subtitle1: { fontWeight: 400, color: "rgba(242,242,242,0.7)" },
    subtitle2: { fontWeight: 500 },
    button: { textTransform: "none", fontWeight: 600 },
    body2: { lineHeight: 1.5 },
  },
  shape: { borderRadius: 10 },
  shadows: [
    "none",
    "0 1px 3px rgba(0,0,0,0.3)",
    "0 2px 6px rgba(0,0,0,0.35)",
    "0 4px 12px rgba(0,0,0,0.4)",
    "0 6px 20px rgba(0,0,0,0.45)",
    "0 8px 28px rgba(0,0,0,0.5)",
    "0 0 20px rgba(20,72,140,0.15)",
    "0 0 30px rgba(20,72,140,0.2)",
    ...Array(17).fill("0 1px 3px rgba(0,0,0,0.3)"),
  ] as Theme["shadows"],
  components: {
    ...sharedComponents,
    MuiCssBaseline: {
      styleOverrides: {
        body: {
          scrollbarColor: `${BRAND.surfaceDarkAlt} ${BRAND.bgDark}`,
          "&::-webkit-scrollbar": {
            width: 8,
            height: 8,
          },
          "&::-webkit-scrollbar-track": {
            background: BRAND.bgDark,
          },
          "&::-webkit-scrollbar-thumb": {
            background: BRAND.surfaceDarkAlt,
            borderRadius: 4,
            "&:hover": {
              background: BRAND.primaryLight,
            },
          },
        },
      },
    },
    MuiTableHead: {
      styleOverrides: {
        root: {
          "& .MuiTableCell-head": {
            fontWeight: 600,
            color: "rgba(242,242,242,0.7)",
            backgroundColor: BRAND.surfaceDarkAlt,
          },
        },
      },
    },
    MuiDataGrid: {
      styleOverrides: {
        root: {
          border: "none",
          fontSize: "0.875rem",
          "& .MuiDataGrid-cell:focus": {
            outline: "2px solid rgba(143,175,217,0.5)",
            outlineOffset: -2,
          },
          "& .MuiDataGrid-cell:focus-within": {
            outline: "2px solid rgba(143,175,217,0.5)",
            outlineOffset: -2,
          },
        },
        columnHeaders: {
          backgroundColor: BRAND.surfaceDarkAlt,
          borderBottom: "1px solid rgba(143,175,217,0.12)",
        },
        row: {
          "&:nth-of-type(even)": {
            backgroundColor: "rgba(26,35,50,0.4)",
          },
          "&:hover": {
            backgroundColor: "rgba(20,72,140,0.15) !important",
          },
        },
        cell: {
          borderBottom: "1px solid rgba(143,175,217,0.08)",
        },
        footerContainer: {
          borderTop: "1px solid rgba(143,175,217,0.12)",
        },
      },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          borderRight: "1px solid rgba(143,175,217,0.12)",
          backgroundColor: BRAND.surfaceDark,
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          boxShadow: "none",
          borderBottom: "1px solid rgba(143,175,217,0.08)",
          backgroundColor: "rgba(13,17,23,0.8)",
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          color: BRAND.textOnDark,
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: {
          border: "1px solid rgba(143,175,217,0.12)",
          boxShadow: "0 8px 28px rgba(0,0,0,0.5)",
        },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          borderRadius: 8,
        },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: {
          textTransform: "none",
          fontWeight: 500,
          "&.Mui-selected": {
            fontWeight: 600,
          },
        },
      },
    },
  },
});

// ─── Light Theme ───────────────────────────────────────────────────────────────

export const lightTheme: Theme = createTheme({
  palette: {
    mode: "light",
    primary: {
      main: BRAND.primary,
      light: BRAND.primaryLight,
      dark: BRAND.primaryDark,
      contrastText: "#FFFFFF",
    },
    secondary: {
      main: BRAND.accentDark,
      light: BRAND.accent,
      dark: "#4A6D96",
      contrastText: "#FFFFFF",
    },
    background: {
      default: "#F5F7FA",
      paper: "#FFFFFF",
    },
    text: {
      primary: "#1A1A2E",
      secondary: "#5A5D7A",
      disabled: "rgba(0,0,0,0.38)",
    },
    divider: "rgba(20,72,140,0.12)",
    error: {
      main: "#D32F2F",
      light: "#EF5350",
      dark: "#B71C1C",
    },
    success: {
      main: "#388E3C",
      light: "#66BB6A",
      dark: "#2E7D32",
    },
    warning: {
      main: "#F57C00",
      light: "#FFA726",
      dark: "#E65100",
    },
    info: {
      main: BRAND.primary,
    },
  },
  typography: {
    fontFamily: '"Roboto", "Helvetica", "Arial", sans-serif',
    h4: { fontWeight: 600, fontSize: "1.5rem", lineHeight: 1.2 },
    h5: { fontWeight: 600, fontSize: "1.25rem", lineHeight: 1.3 },
    h6: { fontWeight: 600, fontSize: "1.1rem", lineHeight: 1.4 },
    subtitle1: { fontWeight: 400, color: "#5A5D7A" },
    subtitle2: { fontWeight: 500 },
    button: { textTransform: "none", fontWeight: 600 },
    body2: { lineHeight: 1.5 },
  },
  shape: { borderRadius: 10 },
  components: {
    ...sharedComponents,
    MuiTableHead: {
      styleOverrides: {
        root: {
          "& .MuiTableCell-head": {
            fontWeight: 600,
            color: "#5A5D7A",
            backgroundColor: "#F8F9FB",
          },
        },
      },
    },
    MuiDataGrid: {
      styleOverrides: {
        root: {
          border: "none",
          fontSize: "0.875rem",
          "& .MuiDataGrid-cell:focus": {
            outline: "2px solid rgba(20,72,140,0.4)",
            outlineOffset: -2,
          },
          "& .MuiDataGrid-cell:focus-within": {
            outline: "2px solid rgba(20,72,140,0.4)",
            outlineOffset: -2,
          },
        },
        columnHeaders: {
          backgroundColor: "#F8F9FB",
          borderBottom: "1px solid rgba(20,72,140,0.1)",
        },
        row: {
          "&:nth-of-type(even)": {
            backgroundColor: "#F8F9FB",
          },
          "&:hover": {
            backgroundColor: "rgba(20,72,140,0.06) !important",
          },
        },
        cell: {
          borderBottom: "1px solid rgba(20,72,140,0.06)",
        },
      },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          borderRight: "1px solid rgba(20,72,140,0.1)",
          backgroundColor: "#FFFFFF",
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
          backgroundColor: "rgba(255,255,255,0.9)",
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          color: "#1A1A2E",
          borderBottom: "1px solid rgba(20,72,140,0.08)",
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: {
          border: "1px solid rgba(20,72,140,0.1)",
          boxShadow: "0 8px 28px rgba(0,0,0,0.1)",
        },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          borderRadius: 8,
        },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: {
          textTransform: "none",
          fontWeight: 500,
          "&.Mui-selected": {
            fontWeight: 600,
          },
        },
      },
    },
  },
});

// ─── Default export (dark) ─────────────────────────────────────────────────────

const theme = darkTheme;
export default theme;
