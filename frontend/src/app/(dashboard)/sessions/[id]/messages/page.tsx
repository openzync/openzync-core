"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  MessageSquare,
  User,
  Bot,
  Loader2,
  AlertCircle,
  ChevronUp,
  Clock,
  Hash,
  ArrowUp,
} from "lucide-react";
import { cn } from "@/lib/utils";
import SessionTabs from "../tabs";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface Message {
  id?: string;
  role: string;
  content: string;
  created_at?: string;
  token_count?: number;
  [key: string]: unknown;
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = sessionStorage.getItem("mg_access_token");
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

function formatTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60_000);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;

  const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  if (diffMins < 1440) return timeStr;

  const dateStr = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${dateStr} ${timeStr}`;
}

function isUserMessage(role: string): boolean {
  return role === "user";
}

function isAssistantMessage(role: string): boolean {
  return role === "assistant";
}

function isSystemOrTool(role: string): boolean {
  return role === "system" || role === "tool";
}

// ─── Role Icons ────────────────────────────────────────────────────────────────

function RoleIcon({ role }: { role: string }) {
  if (isUserMessage(role)) return <User size={14} />;
  if (isAssistantMessage(role)) return <Bot size={14} />;
  return <MessageSquare size={14} />;
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function MessagesPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId");

  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadOlderLoading, setLoadOlderLoading] = useState(false);
  const [allLoaded, setAllLoaded] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const initialScrollDone = useRef(false);

  const fetchMessages = useCallback(
    async (limit: number, prepend: boolean = false) => {
      if (!userId || !sessionId) return;

      const setLoadingFn = prepend ? setLoadOlderLoading : setLoading;
      setLoadingFn(true);
      setError(null);

      try {
        const url = `http://localhost:8000/v1/users/${userId}/sessions/${sessionId}/messages?limit=${limit}`;
        const res = await fetch(url, { headers: getHeaders() });

        if (!res.ok) {
          throw new Error(`HTTP ${res.status}${res.statusText ? `: ${res.statusText}` : ""}`);
        }

        const json = await res.json();
        const items: Message[] = json.data ?? [];
        const msgList: Message[] = Array.isArray(items) ? items : [];

        if (prepend) {
          // Prepend older messages — keep existing + new
          setMessages((prev) => {
            const existingIds = new Set(prev.map((m) => m.id ?? m.content));
            const newItems = msgList.filter((m) => !existingIds.has(m.id ?? m.content));
            return [...newItems, ...prev];
          });
          if (msgList.length < limit) setAllLoaded(true);
        } else {
          // Initial load: sort chronologically (oldest first), scroll to bottom
          const sorted = [...msgList].sort(
            (a, b) =>
              new Date(a.created_at ?? 0).getTime() - new Date(b.created_at ?? 0).getTime(),
          );
          setMessages(sorted);
          setAllLoaded(msgList.length < limit);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load messages");
      } finally {
        setLoadingFn(false);
      }
    },
    [userId, sessionId],
  );

  // ── Initial load ──────────────────────────────────────────────────────────

  useEffect(() => {
    if (!userId || !sessionId) {
      setLoading(false);
      return;
    }
    initialScrollDone.current = false;
    fetchMessages(100, false);
  }, [userId, sessionId, fetchMessages]);

  // ── Auto-scroll to bottom on initial load ─────────────────────────────────

  useEffect(() => {
    if (!loading && messages.length > 0 && !initialScrollDone.current) {
      initialScrollDone.current = true;
      // Small delay to let the DOM paint
      setTimeout(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: "instant" });
      }, 50);
    }
  }, [loading, messages.length]);

  // ── Load older ────────────────────────────────────────────────────────────

  const handleLoadOlder = () => {
    if (loadOlderLoading || allLoaded) return;
    // Remember current scroll height so we can maintain position after prepend
    const container = scrollContainerRef.current;
    const prevScrollHeight = container?.scrollHeight ?? 0;

    // Fetch with larger limit to get more messages
    fetchMessages(messages.length + 100, true).then(() => {
      // Restore scroll position after older messages are prepended
      requestAnimationFrame(() => {
        if (container) {
          const newScrollHeight = container.scrollHeight;
          container.scrollTop = newScrollHeight - prevScrollHeight;
        }
      });
    });
  };

  // ── Scroll to bottom ──────────────────────────────────────────────────────

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  // ── Missing userId/sessionId guard ────────────────────────────────────────

  if (!userId) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Messages</h1>
          <p className="text-sm text-surface-400 mt-1">Conversation messages for this session</p>
        </div>
        <div className="card-base p-8 flex flex-col items-center justify-center gap-3 text-surface-500">
          <AlertCircle size={24} />
          <p className="text-sm">No user selected. Provide a <code className="text-surface-300 font-mono text-xs bg-surface-800 px-1.5 py-0.5 rounded">userId</code> query parameter.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SessionTabs sessionId={sessionId} userId={userId ?? ""} activeTab="messages" />
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Messages</h1>
          <p className="text-sm text-surface-400 mt-1">
            Session:{" "}
            <span className="font-mono text-surface-300 text-xs" title={sessionId}>
              {sessionId.slice(0, 8)}…
            </span>
            <span className="mx-2 text-surface-600">·</span>
            User:{" "}
            <span className="font-mono text-surface-300 text-xs" title={userId}>
              {userId.slice(0, 8)}…
            </span>
          </p>
        </div>
        <button
          onClick={scrollToBottom}
          className="btn-ghost text-xs flex items-center gap-1"
          title="Scroll to bottom"
        >
          <ArrowUp size={14} className="rotate-180" />
          Latest
        </button>
      </div>

      {/* Messages container */}
      <div
        ref={scrollContainerRef}
        className="card-base overflow-y-auto"
        style={{ maxHeight: "calc(100vh - 220px)", minHeight: "400px" }}
      >
        {/* Load older */}
        <div className="sticky top-0 z-10 flex justify-center py-3 bg-gradient-to-b from-surface-900 via-surface-900/95 to-transparent">
          {allLoaded ? (
            <span className="text-xs text-surface-500">All messages loaded</span>
          ) : (
            <button
              onClick={handleLoadOlder}
              disabled={loadOlderLoading}
              className="btn-ghost text-xs"
            >
              {loadOlderLoading ? (
                <>
                  <Loader2 size={12} className="animate-spin" />
                  Loading...
                </>
              ) : (
                <>
                  <ChevronUp size={14} />
                  Load older messages
                </>
              )}
            </button>
          )}
        </div>

        {/* Loading state */}
        {loading && (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            <Loader2 size={24} className="animate-spin text-brand-300" />
            <span className="text-sm text-surface-400">Loading messages...</span>
          </div>
        )}

        {/* Error state */}
        {error && !loading && (
          <div className="mx-4 mt-2 mb-4 flex items-start gap-2 rounded-md bg-error/10 border border-error/30 p-3 text-sm text-error">
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && messages.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-surface-500">
            <MessageSquare size={32} />
            <p className="text-sm">No messages in this session yet.</p>
          </div>
        )}

        {/* Message bubbles */}
        {!loading && messages.length > 0 && (
          <div className="px-4 pb-4 space-y-4">
            {messages.map((msg, idx) => {
              const isUser = isUserMessage(msg.role);
              const isAssistant = isAssistantMessage(msg.role);
              const isSysTool = isSystemOrTool(msg.role);

              // System/tool messages: centered subtle style
              if (isSysTool) {
                return (
                  <div key={msg.id ?? idx} className="flex justify-center">
                    <div className="flex items-center gap-1.5 text-xs text-surface-500 bg-surface-800/40 px-3 py-1.5 rounded-full max-w-lg">
                      <RoleIcon role={msg.role} />
                      <span className="capitalize font-medium text-surface-400">{msg.role}</span>
                      <span className="text-surface-600 mx-0.5">·</span>
                      <span className="truncate">{msg.content}</span>
                      {msg.token_count !== undefined && (
                        <>
                          <span className="text-surface-600 mx-0.5">·</span>
                          <span title="Tokens">{msg.token_count} tok</span>
                        </>
                      )}
                    </div>
                  </div>
                );
              }

              return (
                <div
                  key={msg.id ?? idx}
                  className={cn("flex", isUser ? "justify-end" : "justify-start")}
                >
                  <div className={cn("max-w-[75%] space-y-1", isUser ? "items-end" : "items-start")}>
                    {/* Bubble */}
                    <div
                      className={cn(
                        "px-3.5 py-2 text-sm leading-relaxed",
                        isUser
                          ? "bg-brand-500 text-white rounded-lg rounded-br-sm"
                          : "bg-surface-800 border border-surface-700 text-surface-100 rounded-lg rounded-bl-sm",
                      )}
                    >
                      {msg.content}

                      {/* Token count inline for assistant messages */}
                      {isAssistant && msg.token_count !== undefined && (
                        <span className="ml-2 text-[11px] text-surface-500 font-mono inline-flex items-center gap-0.5">
                          <Hash size={10} />
                          {msg.token_count}
                        </span>
                      )}
                    </div>

                    {/* Timestamp + metadata row */}
                    <div
                      className={cn(
                        "flex items-center gap-2 text-[11px] text-surface-500 px-1",
                        isUser ? "justify-end" : "justify-start",
                      )}
                    >
                      {msg.created_at && (
                        <span className="flex items-center gap-0.5">
                          <Clock size={10} />
                          {formatTime(msg.created_at)}
                        </span>
                      )}
                      {isUser && msg.token_count !== undefined && (
                        <span className="flex items-center gap-0.5" title="Tokens">
                          <Hash size={10} />
                          {msg.token_count}
                        </span>
                      )}
                      <span className="capitalize text-surface-600">{msg.role}</span>
                    </div>
                  </div>
                </div>
              );
            })}
            {/* Invisible anchor for auto-scroll */}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>
    </div>
  );
}
