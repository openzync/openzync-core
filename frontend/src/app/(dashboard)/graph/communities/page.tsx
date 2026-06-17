"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState } from "react";
import { Shield, AlertCircle, Users as UsersIcon } from "lucide-react";
import { get, ApiError, extractList } from "@/lib/api-client";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";

interface Community {
  id: string; name: string; summary: string; member_count: number; created_at: string;
}

export default function CommunitiesPage() {
  const [data, setData] = useState<Community[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchCommunities() {
      setLoading(true); setError(null);
      try {
        // First get a user
        const userData = await get<{ data: { id: string }[] }>("/v1/users?limit=1");
        const users = extractList<{ id: string }>(userData);
        const userId = users[0]?.id;
        if (!userId) throw new Error("No user available");
        const json = await get<{ data: Community[] }>(`/v1/users/${userId}/graph/communities`);
        setData(json.data ?? []);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load communities");
      } finally { setLoading(false); }
    }
    fetchCommunities();
  }, []);

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader title="Communities" description="Community clusters from Label Propagation" />

      {error && <ErrorState message={error} />}

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => <div key={i} className="card-base p-6 h-32 animate-pulse" />)}
        </div>
      ) : data.length === 0 ? (
        <EmptyState icon={Shield} title="No communities found"
          description="Community detection runs as a scheduled task after graph sync." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {data.map((community) => (
            <div key={community.id} className="card-interactive p-5">
              <div className="flex items-start justify-between mb-2">
                <h3 className="font-semibold text-sm">{community.name}</h3>
                <span className="inline-flex items-center gap-1 text-xs text-surface-400">
                  <UsersIcon size={12} />{community.member_count}
                </span>
              </div>
              <p className="text-sm text-surface-400 line-clamp-3">{community.summary}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  </RequireAuth>
  );
}
