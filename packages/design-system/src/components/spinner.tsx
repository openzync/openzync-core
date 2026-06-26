import { cn } from "../cn";

interface SpinnerProps {
  size?: number;
  className?: string;
}

/**
 * Lightweight SVG spinner used for loading states.
 */
export function Spinner({ size = 16, className }: SpinnerProps) {
  return (
    <svg
      className={cn("animate-spin text-current", className)}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}
