"use client";

import { ProjectProvider } from "@/stores/project-context";

export default function ProjectLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ProjectProvider>{children}</ProjectProvider>;
}
