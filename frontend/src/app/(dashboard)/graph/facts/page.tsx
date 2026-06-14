"use client";

import { useEffect, useState } from "react";
import { Database, AlertCircle } from "lucide-react";

export default function FactsPage() {
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const t = setTimeout(() => setLoading(false), 500);
    return () => clearTimeout(t);
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Facts</h1>
        <p className="text-sm text-surface-400 mt-1">Subject-predicate-object knowledge triples</p>
      </div>
      <div className="card-base p-8 text-center text-surface-500">
        <Database size={40} className="mx-auto mb-3 opacity-50" />
        <p className="text-sm">Fact management page coming soon.</p>
        <p className="text-xs text-surface-600 mt-1">Use the Facts tab on a session page to view extracted facts.</p>
      </div>
    </div>
  );
}
