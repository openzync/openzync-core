import type { LucideIcon } from "lucide-react";
import { TrendingUp, TrendingDown } from "lucide-react";
import { cn } from "@/lib/utils";

interface StatCardProps {
  label: string;
  value: number | string | null | undefined;
  icon: LucideIcon;
  color: string;
  loading?: boolean;
  onClick?: () => void;
  trend?: "up" | "down" | null;
}

/**
 * Standard KPI stat card.
 * Replaces duplicated stat-card markup across overview, analytics, monitoring, etc.
 */
export function StatCard({
  label,
  value,
  icon: Icon,
  color,
  loading = false,
  onClick,
  trend,
}: StatCardProps) {
  const Component = onClick ? "button" : "div";
  return (
    <Component
      onClick={onClick}
      className={cn("stat-card", onClick && "cursor-pointer")}
    >
      <div className="flex items-center gap-3">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-500/10">
          <Icon size={22} className={color} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-xs text-surface-400 truncate">{label}</div>
          {loading ? (
            <div className="h-6 w-16 mt-1 rounded bg-surface-800 animate-pulse" />
          ) : (
            <div className="flex items-center gap-1.5 mt-0.5">
              <span className="text-xl font-semibold">
                {value != null ? value : "—"}
              </span>
              {trend === "up" && <TrendingUp size={14} className="text-success shrink-0" />}
              {trend === "down" && <TrendingDown size={14} className="text-success shrink-0" />}
            </div>
          )}
        </div>
      </div>
    </Component>
  );
}
