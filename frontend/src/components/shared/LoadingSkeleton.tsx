"use client";

import { Box, Card, Skeleton } from "@mui/material";

interface LoadingSkeletonProps {
  variant?: "card" | "table" | "chart" | "detail";
  count?: number;
}

export default function LoadingSkeleton({
  variant = "card",
  count = 1,
}: LoadingSkeletonProps) {
  const items = Array.from({ length: count }, (_, i) => i);

  if (variant === "table") {
    return (
      <Card>
        <Box sx={{ p: 2 }}>
          <Skeleton variant="rectangular" height={48} sx={{ mb: 1, borderRadius: 1 }} />
          {items.map((i) => (
            <Skeleton
              key={i}
              variant="rectangular"
              height={52}
              sx={{ mb: 0.5, borderRadius: 0 }}
            />
          ))}
        </Box>
      </Card>
    );
  }

  if (variant === "chart") {
    return (
      <Card>
        <Box sx={{ p: 3 }}>
          <Skeleton variant="rectangular" width="30%" height={24} sx={{ mb: 2, borderRadius: 1 }} />
          <Skeleton variant="rectangular" width="100%" height={400} sx={{ borderRadius: 1 }} />
        </Box>
      </Card>
    );
  }

  if (variant === "detail") {
    return (
      <Box>
        <Skeleton variant="rectangular" width={300} height={32} sx={{ mb: 2, borderRadius: 1 }} />
        <Card sx={{ p: 3 }}>
          <Box sx={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {items.map((i) => (
              <Box key={i}>
                <Skeleton width={80} height={16} sx={{ mb: 0.5 }} />
                <Skeleton width={160} height={24} />
              </Box>
            ))}
          </Box>
        </Card>
      </Box>
    );
  }

  // variant === "card"
  return (
    <Box sx={{ display: "flex", gap: 3, flexWrap: "wrap" }}>
      {items.map((i) => (
        <Card key={i} sx={{ flex: "1 1 200px", p: 3 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Skeleton variant="circular" width={48} height={48} />
            <Box sx={{ flex: 1 }}>
              <Skeleton width="60%" height={16} sx={{ mb: 0.5 }} />
              <Skeleton width="40%" height={32} />
            </Box>
          </Box>
        </Card>
      ))}
    </Box>
  );
}
