import { type VariantProps, cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

// ─── Variants ─────────────────────────────────────────────────────────────────

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "bg-surface-700 text-surface-300",
        success: "bg-success/10 text-success",
        warning: "bg-warning/10 text-warning",
        error: "bg-error/10 text-error",
        info: "bg-accent-300/10 text-accent-300",
        brand: "bg-brand-500/10 text-brand-300",
      },
      size: {
        sm: "text-[10px] px-1.5 py-0.5",
        md: "text-xs px-2 py-0.5",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
);

// ─── Status code badge ────────────────────────────────────────────────────────

const statusBadgeVariants = cva(
  "inline-flex items-center justify-center rounded px-1.5 py-0.5 text-xs font-mono font-medium min-w-[32px]",
  {
    variants: {
      status: {
        info: "bg-surface-700 text-surface-400",
        success: "bg-success/10 text-success",
        warning: "bg-warning/10 text-warning",
        error: "bg-error/10 text-error",
      },
    },
    defaultVariants: {
      status: "info",
    },
  },
);

/** Map HTTP status code → badge variant */
export function statusCodeVariant(
  code: number | null,
): "info" | "success" | "warning" | "error" {
  if (code === null) return "info";
  if (code < 300) return "success";
  if (code < 500) return "warning";
  return "error";
}

// ─── Actor type badge ─────────────────────────────────────────────────────────

export function actorTypeVariant(
  type: string | null,
): "default" | "brand" | "info" | "success" {
  if (!type || type === "system") return "default";
  if (type === "user") return "brand";
  if (type === "api_key") return "info";
  return "default";
}

export function actorTypeLabel(type: string | null): string {
  if (!type) return "system";
  const map: Record<string, string> = {
    user: "User",
    api_key: "API Key",
    system: "System",
  };
  return map[type] ?? type;
}

// ─── Props ────────────────────────────────────────────────────────────────────

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({
  className,
  variant,
  size,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(badgeVariants({ variant, size }), className)}
      {...props}
    />
  );
}

export function StatusBadge({
  code,
}: {
  code: number | null;
}) {
  const variant = statusCodeVariant(code);
  return (
    <span className={statusBadgeVariants({ status: variant })}>
      {code ?? "—"}
    </span>
  );
}

export function ActorTypeBadge({
  type,
}: {
  type: string | null;
}) {
  return (
    <Badge variant={actorTypeVariant(type)} size="sm">
      {actorTypeLabel(type)}
    </Badge>
  );
}
