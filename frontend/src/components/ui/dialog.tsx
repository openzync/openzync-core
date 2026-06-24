import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "./button";

// ─── Types ──────────────────────────────────────────────────────────────────

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger?: React.ReactNode;
  title: string;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  /** Override default close button in header. Defaults to true. */
  showClose?: boolean;
  /** Prevent closing on overlay click / Escape. Defaults to false. */
  persistent?: boolean;
  size?: "sm" | "md" | "lg";
}

// ─── Component ──────────────────────────────────────────────────────────────

const sizeMap: Record<string, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-lg",
};

/**
 * Accessible modal dialog built on @radix-ui/react-dialog.
 *
 * - Focus trap (auto-first-focus + cycle)
 * - Escape dismisses (unless `persistent`)
 * - Overlay click dismisses (unless `persistent`)
 * - ARIA labelled-by / described-by
 */
export function Dialog({
  open,
  onOpenChange,
  trigger,
  title,
  description,
  children,
  footer,
  showClose = true,
  persistent = false,
  size = "md",
}: DialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger && (
        <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>
      )}

      <DialogPrimitive.Portal>
        {/* ── Overlay ───────────────────────────────────────────────────── */}
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50",
            "bg-black/60 backdrop-blur-sm",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
          )}
        />

        {/* ── Content ──────────────────────────────────────────────────── */}
        <DialogPrimitive.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2",
            "w-[calc(100%-2rem)] px-0",
            sizeMap[size],
            "rounded-xl border border-surface-700",
            "bg-surface-900 shadow-2xl",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
            "data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%]",
            "data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%]",
            "max-h-[85vh] overflow-y-auto",
            "focus:outline-none",
          )}
          onEscapeKeyDown={persistent ? (e) => e.preventDefault() : undefined}
          onPointerDownOutside={
            persistent ? (e) => e.preventDefault() : undefined
          }
          aria-describedby={description ? "dialog-description" : undefined}
        >
          {/* ── Header ────────────────────────────────────────────────── */}
          <div className="flex items-start justify-between px-6 pt-6 pb-2">
            <div className="flex-1 min-w-0">
              <DialogPrimitive.Title className="text-lg font-semibold text-[--color-text-primary]">
                {title}
              </DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description
                  id="dialog-description"
                  className="text-sm text-surface-400 mt-1"
                >
                  {description}
                </DialogPrimitive.Description>
              )}
            </div>
            {showClose && (
              <DialogPrimitive.Close asChild>
                <button
                  type="button"
                  className={cn(
                    "ml-4 rounded-md p-1.5",
                    "text-surface-400 hover:text-[--color-text-primary] hover:bg-surface-800",
                    "transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-accent-300",
                  )}
                  aria-label="Close dialog"
                >
                  <X size={18} />
                </button>
              </DialogPrimitive.Close>
            )}
          </div>

          {/* ── Body ───────────────────────────────────────────────────── */}
          <div className="px-6 py-3">{children}</div>

          {/* ── Footer ─────────────────────────────────────────────────── */}
          {footer && (
            <div className="flex items-center justify-end gap-3 px-6 pb-6 pt-2">
              {footer}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// ─── Convenience sub-components for declarative usage ───────────────────────

/**
 * Wrapper around `<Button variant="secondary" />` that closes the dialog.
 */
export function DialogCloseButton({
  children = "Cancel",
  ...props
}: React.ComponentProps<typeof Button>) {
  return (
    <DialogPrimitive.Close asChild>
      <Button variant="secondary" {...props}>
        {children}
      </Button>
    </DialogPrimitive.Close>
  );
}
