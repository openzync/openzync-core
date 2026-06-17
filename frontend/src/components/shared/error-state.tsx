import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

/**
 * Standard inline error display.
 * Shows error message + optional retry button.
 */
export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="rounded-md bg-error/10 border border-error/30 px-4 py-3 text-sm text-error flex items-center gap-2">
      <AlertCircle size={14} className="shrink-0" />
      <span className="flex-1">{message}</span>
      {onRetry && (
        <Button variant="ghost" size="sm" onClick={onRetry} className="text-error hover:text-white">
          Retry
        </Button>
      )}
    </div>
  );
}
