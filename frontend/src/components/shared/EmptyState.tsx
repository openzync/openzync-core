"use client";

import type { ReactNode } from "react";
import { Box, Typography, Button } from "@mui/material";
import InboxIcon from "@mui/icons-material/InboxOutlined";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: {
    label: string;
    onClick: () => void;
  };
}

export default function EmptyState({
  icon,
  title,
  description,
  action,
}: EmptyStateProps) {
  return (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        py: 8,
        px: 3,
        textAlign: "center",
      }}
    >
      <Box
        sx={{
          width: 64,
          height: 64,
          borderRadius: "50%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          mb: 2,
          bgcolor: "rgba(143,175,217,0.1)",
          color: "secondary.main",
        }}
      >
        {icon ?? <InboxIcon sx={{ fontSize: 32 }} />}
      </Box>
      <Typography variant="h6" sx={{ mb: 0.5, fontWeight: 600 }}>
        {title}
      </Typography>
      <Typography
        variant="body2"
        sx={{ color: "text.secondary", maxWidth: 400, mb: action ? 3 : 0 }}
      >
        {description}
      </Typography>
      {action && (
        <Button variant="contained" onClick={action.onClick}>
          {action.label}
        </Button>
      )}
    </Box>
  );
}
