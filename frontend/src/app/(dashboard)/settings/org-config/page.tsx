"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function OrgConfigRedirect() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/settings/org-config/llm");
  }, [router]);

  return null;
}
