"use client";

import { Box, Typography } from "@mui/material";

export default function GraphPage() {
  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Knowledge Graph
      </Typography>
      <Typography variant="subtitle1" sx={{ mb: 3 }}>
        Explore your knowledge graph
      </Typography>
      <Typography variant="body1" color="text.secondary">
        The knowledge graph visualisation is coming soon. You can interact with
        it via the API in the meantime.
      </Typography>
    </Box>
  );
}
