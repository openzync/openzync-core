"use client";

import type { ReactNode } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  CircularProgress,
} from "@mui/material";

interface FormDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  onSubmit: () => void;
  submitting?: boolean;
  submitLabel?: string;
  cancelLabel?: string;
  maxWidth?: "xs" | "sm" | "md" | "lg";
  fullWidth?: boolean;
}

export default function FormDialog({
  open,
  onClose,
  title,
  children,
  onSubmit,
  submitting = false,
  submitLabel = "Save",
  cancelLabel = "Cancel",
  maxWidth = "sm",
  fullWidth = true,
}: FormDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!submitting) onClose();
      }}
      maxWidth={maxWidth}
      fullWidth={fullWidth}
    >
      <DialogTitle sx={{ fontWeight: 600, pb: 1 }}>{title}</DialogTitle>
      <DialogContent sx={{ pt: "8px !important" }}>
        {children}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={submitting} sx={{ opacity: 0.7 }}>
          {cancelLabel}
        </Button>
        <Button
          variant="contained"
          onClick={onSubmit}
          disabled={submitting}
          startIcon={submitting ? <CircularProgress size={16} color="inherit" /> : undefined}
        >
          {submitting ? `${submitLabel}...` : submitLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
