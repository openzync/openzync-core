"use client";

import { Fade, Slide } from "@mui/material";
import type { ReactNode } from "react";

interface AnimatedPageProps {
  children: ReactNode;
}

export default function AnimatedPage({ children }: AnimatedPageProps) {
  return (
    <Fade in timeout={300}>
      <Slide direction="up" in timeout={300}>
        <div>{children}</div>
      </Slide>
    </Fade>
  );
}
