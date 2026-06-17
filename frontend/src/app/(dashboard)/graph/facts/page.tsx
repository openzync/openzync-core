"use client";
import { RequireAuth } from "../../require-auth";

import { Database } from "lucide-react";
import { PageHeader } from "@/components/shared/page-header";

export default function FactsPage() {
  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader title="Facts" description="Subject-predicate-object knowledge triples" />
      <div className="card-base p-8 text-center text-surface-500">
        <Database size={40} className="mx-auto mb-3 opacity-50" />
        <p className="text-sm">Fact management page coming soon.</p>
        <p className="text-xs text-surface-600 mt-1">Use the Facts tab on a session page to view extracted facts.</p>
      </div>
    </div>
  </RequireAuth>
  );
}
