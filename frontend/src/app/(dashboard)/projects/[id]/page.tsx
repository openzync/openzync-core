"use client";

import { useEffect } from "react";
import { useRouter, useParams } from "next/navigation";

export default function ProjectRootPage() {
  const router = useRouter();
  const params = useParams();
  const projectId = params.id as string;

  useEffect(() => {
    router.replace(`/projects/${projectId}/sessions`);
  }, [router, projectId]);

  return null;
}
