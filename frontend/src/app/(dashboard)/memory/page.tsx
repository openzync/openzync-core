"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Upload,
  Search,
  FileText,
  Loader2,
  AlertCircle,
  CheckCircle2,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface UserItem {
  id: string;
  user_id?: string;
  email?: string;
  name?: string;
}

interface IngestResponse {
  job_id?: string;
  episode_count?: number;
  status?: string;
  message?: string;
}

interface ContextResult {
  context?: string;
  results?: unknown[];
  content?: string;
  [key: string]: unknown;
}

interface SearchResultItem {
  type?: string;
  content?: string;
  score?: number;
  id?: string;
  [key: string]: unknown;
}

interface SearchResponse {
  results?: SearchResultItem[];
  items?: SearchResultItem[];
  total?: number;
}

type TabId = "ingest" | "context" | "search";

// ─── Tab configuration ─────────────────────────────────────────────────────────

const TABS: { id: TabId; label: string; icon: React.ReactNode }[] = [
  { id: "ingest", label: "Ingest", icon: <Upload size={16} /> },
  { id: "context", label: "Context", icon: <FileText size={16} /> },
  { id: "search", label: "Search", icon: <Search size={16} /> },
];

const ROLES = ["user", "assistant", "system", "tool"] as const;

// ─── API helpers ───────────────────────────────────────────────────────────────

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = sessionStorage.getItem("mg_access_token");
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function MemoryPage() {
  const [activeTab, setActiveTab] = useState<TabId>("ingest");
  const [users, setUsers] = useState<UserItem[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [usersError, setUsersError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadUsers() {
      setLoadingUsers(true);
      setUsersError(null);
      try {
        const res = await fetch("http://localhost:8000/v1/users?limit=200", {
          headers: getHeaders(),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json: any = await res.json();
        const list: UserItem[] = json.data ?? json.items ?? json.users ?? [];
        // Flatten in case API wraps in a nested field
        const flat = Array.isArray(list) ? list : [];
        if (!cancelled) setUsers(flat);
      } catch (err) {
        if (!cancelled) setUsersError(err instanceof Error ? err.message : "Failed to load users");
      } finally {
        if (!cancelled) setLoadingUsers(false);
      }
    }
    loadUsers();
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Memory</h1>
        <p className="text-sm text-surface-400 mt-1">
          Ingest messages, query context, and search across memory
        </p>
      </div>

      {/* Tab navigation */}
      <div className="flex gap-1 rounded-lg bg-surface-950 p-1 border border-surface-800 w-fit">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-medium transition-colors",
              activeTab === tab.id
                ? "bg-brand-500 text-white shadow-sm"
                : "bg-surface-800 text-surface-300 hover:bg-surface-700 hover:text-surface-100",
            )}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === "ingest" && (
        <IngestTab users={users} loadingUsers={loadingUsers} usersError={usersError} />
      )}
      {activeTab === "context" && (
        <ContextTab users={users} loadingUsers={loadingUsers} usersError={usersError} />
      )}
      {activeTab === "search" && (
        <SearchTab users={users} loadingUsers={loadingUsers} usersError={usersError} />
      )}
    </div>
  );
}

// ─── User Selector (shared) ────────────────────────────────────────────────────

function UserSelect({
  users,
  loading,
  error,
  value,
  onChange,
  label,
}: {
  users: UserItem[];
  loading: boolean;
  error: string | null;
  value: string;
  onChange: (val: string) => void;
  label?: string;
}) {
  return (
    <div className="space-y-1.5">
      {label && <label className="text-xs font-medium text-surface-400">{label}</label>}
      {loading ? (
        <div className="h-9 rounded-md bg-surface-800 animate-pulse" />
      ) : error ? (
        <div className="flex items-center gap-2 text-xs text-error h-9">
          <AlertCircle size={14} />
          {error}
        </div>
      ) : (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input-base cursor-pointer appearance-none"
          style={{
            backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238A99AB' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`,
            backgroundRepeat: "no-repeat",
            backgroundPosition: "right 10px center",
            paddingRight: "32px",
          }}
        >
          <option value="" disabled>
            Select a user...
          </option>
          {users.map((u) => {
            const uid = u.id ?? u.user_id ?? "";
            const displayName = u.email ?? u.name ?? uid;
            return (
              <option key={uid} value={uid}>
                {displayName}
              </option>
            );
          })}
        </select>
      )}
    </div>
  );
}

// ─── Ingest Tab ────────────────────────────────────────────────────────────────

function IngestTab({
  users,
  loadingUsers,
  usersError,
}: {
  users: UserItem[];
  loadingUsers: boolean;
  usersError: string | null;
}) {
  const [selectedUser, setSelectedUser] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [messagesText, setMessagesText] = useState("");
  const [selectedRole, setSelectedRole] = useState<string>("user");
  const [ingesting, setIngesting] = useState(false);
  const [result, setResult] = useState<IngestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleIngest = useCallback(async () => {
    if (!selectedUser) {
      setError("Please select a user");
      return;
    }
    if (!messagesText.trim()) {
      setError("Please enter at least one message");
      return;
    }

    setIngesting(true);
    setError(null);
    setResult(null);

    // Parse messages: each line is "role: content" or uses the selected role as default
    const lines = messagesText.trim().split("\n").filter(Boolean);
    const messages = lines.map((line) => {
      // Try to parse "role: content" pattern
      const match = line.match(/^(user|assistant|system|tool):\s*(.*)/i);
      if (match) {
        return { role: match[1].toLowerCase(), content: match[2] };
      }
      return { role: selectedRole, content: line };
    });

    try {
      const body: Record<string, unknown> = { messages };
      if (sessionId.trim()) body["session_id"] = sessionId.trim();

      const res = await fetch(
        `http://localhost:8000/v1/users/${selectedUser}/memory`,
        {
          method: "POST",
          headers: getHeaders(),
          body: JSON.stringify(body),
        },
      );

      const data: IngestResponse = await res.json();

      if (!res.ok) {
        throw new Error(data.message ?? `HTTP ${res.status}`);
      }

      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ingest failed");
    } finally {
      setIngesting(false);
    }
  }, [selectedUser, messagesText, selectedRole, sessionId]);

  return (
    <div className="card-base p-5 space-y-5">
      <h2 className="text-sm font-semibold flex items-center gap-2">
        <Upload size={16} className="text-brand-300" />
        Ingest Messages
      </h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <UserSelect
          users={users}
          loading={loadingUsers}
          error={usersError}
          value={selectedUser}
          onChange={setSelectedUser}
          label="User"
        />
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-surface-400">Session ID (optional)</label>
          <input
            type="text"
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder="uuid or leave empty"
            className="input-base"
          />
        </div>
      </div>

      <div className="space-y-1.5">
        <label className="text-xs font-medium text-surface-400">
          Messages{" "}
          <span className="text-surface-500 font-normal">
            (one per line, format: role: content)
          </span>
        </label>
        <textarea
          value={messagesText}
          onChange={(e) => setMessagesText(e.target.value)}
          placeholder="user: What is the capital of France?&#10;assistant: The capital of France is Paris."
          rows={6}
          className="input-base min-h-[120px] py-2 resize-y leading-relaxed"
          style={{ height: "auto" }}
        />
      </div>

      <div className="flex items-end gap-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-surface-400">Default Role</label>
          <select
            value={selectedRole}
            onChange={(e) => setSelectedRole(e.target.value)}
            className="input-base cursor-pointer w-36 appearance-none"
            style={{
              backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238A99AB' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 10px center",
              paddingRight: "32px",
            }}
          >
            {ROLES.map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={handleIngest}
          disabled={ingesting || !selectedUser}
          className="btn-primary"
        >
          {ingesting ? (
            <>
              <Loader2 size={16} className="animate-spin" />
              Ingesting...
            </>
          ) : (
            <>
              <Upload size={16} />
              Ingest
            </>
          )}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="flex items-start gap-2 rounded-md bg-error/10 border border-error/30 p-3 text-sm text-error">
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Success result */}
      {result && (
        <div className="space-y-3 rounded-md bg-brand-500/5 border border-brand-500/20 p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-brand-300">
            <CheckCircle2 size={16} />
            Ingest Successful
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {result.job_id && (
              <div className="space-y-0.5">
                <div className="text-xs text-surface-500">Job ID</div>
                <div className="text-sm font-mono text-surface-200 truncate" title={result.job_id}>
                  {result.job_id}
                </div>
              </div>
            )}
            {result.episode_count !== undefined && (
              <div className="space-y-0.5">
                <div className="text-xs text-surface-500">Episodes</div>
                <div className="text-sm font-semibold text-surface-200">{result.episode_count}</div>
              </div>
            )}
            {result.status && (
              <div className="space-y-0.5">
                <div className="text-xs text-surface-500">Status</div>
                <div className="text-sm font-medium text-success">{result.status}</div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Context Tab ───────────────────────────────────────────────────────────────

function ContextTab({
  users,
  loadingUsers,
  usersError,
}: {
  users: UserItem[];
  loadingUsers: boolean;
  usersError: string | null;
}) {
  const [selectedUser, setSelectedUser] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [fetching, setFetching] = useState(false);
  const [result, setResult] = useState<ContextResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleGetContext = useCallback(async () => {
    if (!selectedUser) {
      setError("Please select a user");
      return;
    }
    if (!query.trim()) {
      setError("Please enter a query");
      return;
    }

    setFetching(true);
    setError(null);
    setResult(null);

    try {
      const encodedQuery = encodeURIComponent(query.trim());
      const url = `http://localhost:8000/v1/users/${selectedUser}/context?query=${encodedQuery}&limit=${limit}`;

      const res = await fetch(url, { headers: getHeaders() });
      const data: ContextResult = await res.json();

      if (!res.ok) {
        throw new Error((data.message as string) ?? `HTTP ${res.status}`);
      }

      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch context");
    } finally {
      setFetching(false);
    }
  }, [selectedUser, query, limit]);

  return (
    <div className="card-base p-5 space-y-5">
      <h2 className="text-sm font-semibold flex items-center gap-2">
        <FileText size={16} className="text-brand-300" />
        Query Context
      </h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <UserSelect
          users={users}
          loading={loadingUsers}
          error={usersError}
          value={selectedUser}
          onChange={setSelectedUser}
          label="User"
        />
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-surface-400">
            <div className="flex items-center justify-between">
              <span>Limit</span>
              <span className="text-surface-500 font-mono text-[11px]">{limit}</span>
            </div>
          </label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1}
              max={100}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="flex-1 accent-brand-500 h-1.5 cursor-pointer"
              style={{
                appearance: "none",
                height: "6px",
                borderRadius: "3px",
                background: `linear-gradient(to right, #14488C ${limit}%, #303A4E ${limit}%)`,
              }}
            />
            <input
              type="number"
              min={1}
              max={100}
              value={limit}
              onChange={(e) => setLimit(Math.min(100, Math.max(1, Number(e.target.value) || 1)))}
              className="input-base w-16 text-center text-sm"
            />
          </div>
        </div>
      </div>

      <div className="space-y-1.5">
        <label className="text-xs font-medium text-surface-400">Query</label>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. What did the user say about their project?"
          className="input-base"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !fetching) handleGetContext();
          }}
        />
      </div>

      <button
        onClick={handleGetContext}
        disabled={fetching || !selectedUser || !query.trim()}
        className="btn-primary"
      >
        {fetching ? (
          <>
            <Loader2 size={16} className="animate-spin" />
            Fetching...
          </>
        ) : (
          <>
            <FileText size={16} />
            Get Context
          </>
        )}
      </button>

      {/* Error */}
      {error && (
        <div className="flex items-start gap-2 rounded-md bg-error/10 border border-error/30 p-3 text-sm text-error">
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold text-surface-400 uppercase tracking-wider">
            Context Results
          </h3>
          <pre className="rounded-md bg-surface-950 border border-surface-800 p-4 text-sm text-surface-200 font-mono overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

// ─── Search Tab ────────────────────────────────────────────────────────────────

function SearchTab({
  users,
  loadingUsers,
  usersError,
}: {
  users: UserItem[];
  loadingUsers: boolean;
  usersError: string | null;
}) {
  const [selectedUser, setSelectedUser] = useState("");
  const [query, setQuery] = useState("");
  const [searchEpisodes, setSearchEpisodes] = useState(true);
  const [searchFacts, setSearchFacts] = useState(true);
  const [searchEntities, setSearchEntities] = useState(true);
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchResultItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = useCallback(async () => {
    if (!selectedUser) {
      setError("Please select a user");
      return;
    }
    if (!query.trim()) {
      setError("Please enter a query");
      return;
    }

    setSearching(true);
    setError(null);
    setResults(null);

    try {
      // Build type filter
      const selectedTypes: string[] = [];
      if (searchEpisodes) selectedTypes.push("episodes");
      if (searchFacts) selectedTypes.push("facts");
      if (searchEntities) selectedTypes.push("entities");

      const params = new URLSearchParams();
      params.set("query", query.trim());
      if (selectedTypes.length > 0 && selectedTypes.length < 3) {
        params.set("type", selectedTypes.join(","));
      }

      const url = `http://localhost:8000/v1/users/${selectedUser}/search?${params.toString()}`;

      const res = await fetch(url, { headers: getHeaders() });
      const data: SearchResponse = await res.json();

      if (!res.ok) {
        throw new Error((data as unknown as { message?: string }).message ?? `HTTP ${res.status}`);
      }

      const items = data.results ?? data.items ?? [];
      setResults(Array.isArray(items) ? items : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setSearching(false);
    }
  }, [selectedUser, query, searchEpisodes, searchFacts, searchEntities]);

  return (
    <div className="card-base p-5 space-y-5">
      <h2 className="text-sm font-semibold flex items-center gap-2">
        <Search size={16} className="text-brand-300" />
        Search Memory
      </h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <UserSelect
          users={users}
          loading={loadingUsers}
          error={usersError}
          value={selectedUser}
          onChange={setSelectedUser}
          label="User"
        />
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-surface-400">Type Filter</label>
          <div className="flex flex-wrap gap-3 h-9 items-center">
            <label className="flex items-center gap-1.5 cursor-pointer text-sm text-surface-300 hover:text-surface-100 transition-colors">
              <input
                type="checkbox"
                checked={searchEpisodes}
                onChange={(e) => setSearchEpisodes(e.target.checked)}
                className="rounded border-surface-600 bg-surface-800 text-brand-500 focus:ring-brand-500/30 focus:ring-offset-0"
                style={{ accentColor: "#14488C" }}
              />
              Episodes
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer text-sm text-surface-300 hover:text-surface-100 transition-colors">
              <input
                type="checkbox"
                checked={searchFacts}
                onChange={(e) => setSearchFacts(e.target.checked)}
                className="rounded border-surface-600 bg-surface-800 text-brand-500 focus:ring-brand-500/30 focus:ring-offset-0"
                style={{ accentColor: "#14488C" }}
              />
              Facts
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer text-sm text-surface-300 hover:text-surface-100 transition-colors">
              <input
                type="checkbox"
                checked={searchEntities}
                onChange={(e) => setSearchEntities(e.target.checked)}
                className="rounded border-surface-600 bg-surface-800 text-brand-500 focus:ring-brand-500/30 focus:ring-offset-0"
                style={{ accentColor: "#14488C" }}
              />
              Entities
            </label>
          </div>
        </div>
      </div>

      <div className="space-y-1.5">
        <label className="text-xs font-medium text-surface-400">Query</label>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search across episodes, facts, and entities..."
          className="input-base"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !searching) handleSearch();
          }}
        />
      </div>

      <button
        onClick={handleSearch}
        disabled={searching || !selectedUser || !query.trim()}
        className="btn-primary"
      >
        {searching ? (
          <>
            <Loader2 size={16} className="animate-spin" />
            Searching...
          </>
        ) : (
          <>
            <Search size={16} />
            Search
          </>
        )}
      </button>

      {/* Error */}
      {error && (
        <div className="flex items-start gap-2 rounded-md bg-error/10 border border-error/30 p-3 text-sm text-error">
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Results table */}
      {results !== null && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold text-surface-400 uppercase tracking-wider">
            Results{" "}
            <span className="text-surface-500 font-normal normal-case">({results.length})</span>
          </h3>

          {results.length === 0 ? (
            <div className="rounded-md bg-surface-950 border border-surface-800 p-6 text-center text-sm text-surface-500">
              No results found for this query.
            </div>
          ) : (
            <div className="overflow-x-auto rounded-md border border-surface-800">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-surface-950 border-b border-surface-800">
                    <th className="px-3 py-2 text-left text-xs font-medium text-surface-400 uppercase tracking-wider w-24">
                      Type
                    </th>
                    <th className="px-3 py-2 text-left text-xs font-medium text-surface-400 uppercase tracking-wider">
                      Content
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium text-surface-400 uppercase tracking-wider w-20">
                      Score
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-800">
                  {results.map((item, i) => (
                    <tr key={item.id ?? i} className="hover:bg-surface-800/50 transition-colors">
                      <td className="px-3 py-2.5">
                        <span
                          className={cn(
                            "inline-block rounded-full px-2 py-0.5 text-[11px] font-medium capitalize",
                            item.type === "episode" || item.type === "episodes"
                              ? "bg-accent-300/10 text-accent-300"
                              : item.type === "fact" || item.type === "facts"
                                ? "bg-success/10 text-success"
                                : item.type === "entity" || item.type === "entities"
                                  ? "bg-warning/10 text-warning"
                                  : "bg-surface-800 text-surface-400",
                          )}
                        >
                          {item.type ?? "unknown"}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-surface-200 max-w-md truncate">
                        {item.content ?? JSON.stringify(item)}
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        {item.score !== undefined ? (
                          <span
                            className={cn(
                              "font-mono text-xs font-medium",
                              item.score >= 0.7
                                ? "text-success"
                                : item.score >= 0.4
                                  ? "text-warning"
                                  : "text-surface-400",
                            )}
                          >
                            {(item.score * 100).toFixed(0)}%
                          </span>
                        ) : (
                          <span className="text-surface-600">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
