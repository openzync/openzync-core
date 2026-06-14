"use client";

import { useEffect, useState } from "react";
import { Shield, AlertCircle, Users as UsersIcon } from "lucide-react";

interface Community {
  id: string;
  name: string;
  summary: string;
  member_count: number;
  created_at: string;
}

export default function CommunitiesPage() {
  const [data, setData] = useState<Community[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    async function fetchCommunities() {
      setLoading(true);
      setError("");
      try {
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        // First get a user
        const userRes = await fetch("http://localhost:8000/v1/users?limit=1", { headers });
        if (!userRes.ok) throw new Error("No users found");
        const userData = await userRes.json();
        const userId = (userData.data?.[0] as any)?.id;
        if (!userId) throw new Error("No user available");

        const res = await fetch(`http://localhost:8000/v1/users/${userId}/graph/communities`, { headers });
        if (!res.ok) throw new Error("Failed to load communities");
        const json = await res.json();
        setData(json.data ?? []);
      } catch (err: any) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    }
    fetchCommunities();
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Communities</h1>
        <p className="text-sm text-surface-400 mt-1">Community clusters from Label Propagation</p>
      </div>

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1,2,3].map(i => <div key={i} className="card-base p-6 h-32 animate-pulse" />)}
        </div>
      ) : error ? (
        <div className="card-base p-6 flex items-center gap-3 text-error text-sm"><AlertCircle size={18} />{error}</div>
      ) : data.length === 0 ? (
        <div className="card-base p-8 text-center text-surface-500">
          <Shield size={40} className="mx-auto mb-3 opacity-50" />
          <p className="text-sm">No communities found. Community detection runs as a scheduled task after graph sync.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {data.map((community) => (
            <div key={community.id} className="card-interactive p-5">
              <div className="flex items-start justify-between mb-2">
                <h3 className="font-semibold text-sm">{community.name}</h3>
                <span className="inline-flex items-center gap-1 text-xs text-surface-400">
                  <UsersIcon size={12} />
                  {community.member_count}
                </span>
              </div>
              <p className="text-sm text-surface-400 line-clamp-3">{community.summary}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
