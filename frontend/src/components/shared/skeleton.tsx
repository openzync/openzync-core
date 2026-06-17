import { cn } from "@/lib/utils";

interface SkeletonProps {
  className?: string;
  /** How many skeleton rows to render (for tables) */
  rows?: number;
  /** How many columns per row (for table skeletons) */
  cols?: number;
  /** Width variants per column (for table skeletons) */
  colWidths?: string[];
}

/**
 * Shared skeleton loader for tables and cards.
 * Replaces per-page `animate-pulse` divs.
 */
export function Skeleton({ className }: SkeletonProps) {
  return (
    <div className={cn("h-4 rounded bg-surface-800 animate-pulse", className)} />
  );
}

/**
 * Table row skeleton with configurable columns.
 */
export function TableSkeleton({
  rows = 5,
  cols = 4,
  colWidths,
}: SkeletonProps) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
          {Array.from({ length: cols }, (_, j) => (
            <td key={j} className="px-4 py-3">
              <Skeleton
                className={colWidths?.[j] ?? "h-4 w-20"}
              />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
