import { cn } from "../cn";
import { Spinner } from "./spinner";

// ─── Variants ─────────────────────────────────────────────────────────────────

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: React.ReactNode;
}

const variantStyles: Record<ButtonVariant, string> = {
  primary:
    "bg-brand-500 text-white hover:bg-brand-600 shadow-sm hover:shadow-[0_0_20px_rgba(20,72,140,0.3)]",
  secondary:
    "bg-transparent text-surface-100 border border-surface-700 hover:bg-surface-800 hover:text-white",
  ghost:
    "bg-transparent text-surface-300 hover:bg-surface-800 hover:text-white",
  danger:
    "bg-error text-white hover:bg-[#d32f2f] shadow-sm hover:shadow-[0_0_20px_rgba(239,83,80,0.3)]",
};

const sizeStyles: Record<ButtonSize, string> = {
  sm: "text-xs px-2.5 py-1.5 rounded-md gap-1.5",
  md: "text-sm px-4 py-2 rounded-md gap-1.5",
  lg: "text-sm px-5 py-2.5 rounded-lg gap-2",
};

/**
 * Shared button component.
 * Matches the dashboard's Button component exactly.
 */
export function Button({
  className,
  variant = "primary",
  size = "md",
  loading = false,
  icon,
  disabled,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center font-medium transition-all duration-150",
        "focus-visible:outline-2 focus-visible:outline-accent-300 focus-visible:outline-offset-2",
        "disabled:pointer-events-none disabled:opacity-50",
        "cursor-pointer",
        variantStyles[variant],
        sizeStyles[size],
        className,
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Spinner size={14} /> : icon ? icon : null}
      {children}
    </button>
  );
}
