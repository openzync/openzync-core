"use client";

import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";
import { readChatSSE, type ChatEvent } from "@/lib/chat-stream";

// ─── Types ─────────────────────────────────────────────────────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; arguments: Record<string, unknown> }[];
  timestamp: Date;
}

interface ChatContextValue {
  isOpen: boolean;
  messages: ChatMessage[];
  isLoading: boolean;
  error: string | null;
  openChat: () => void;
  closeChat: () => void;
  sendMessage: (text: string) => Promise<void>;
}

// ─── Context ───────────────────────────────────────────────────────────────

const ChatContext = createContext<ChatContextValue | null>(null);

export function useChat() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChat must be used within ChatProvider");
  return ctx;
}

// ─── Token helper ──────────────────────────────────────────────────────────

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("mg_access_token");
}

function getUserId(): string | null {
  if (typeof window === "undefined") return null;
  const token = sessionStorage.getItem("mg_access_token");
  if (!token) return null;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.sub || null;
  } catch {
    return null;
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ─── Provider ──────────────────────────────────────────────────────────────

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const currentAssistantRef = useRef<ChatMessage | null>(null);

  const openChat = useCallback(() => {
    setIsOpen(true);
    setError(null);
  }, []);

  const closeChat = useCallback(() => {
    setIsOpen(false);
  }, []);

  const sendMessage = useCallback(async (text: string) => {
    const token = getToken();
    const userId = getUserId();
    if (!token || !userId) {
      setError("Not authenticated");
      return;
    }

    setError(null);
    setIsLoading(true);

    // Add user message to UI immediately
    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);

    // Create placeholder for assistant response
    const assistantId = `assistant-${Date.now()}`;
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, assistantMsg]);
    currentAssistantRef.current = assistantMsg;

    const url = `${API_BASE}/v1/users/${userId}/chat`;

    try {
      for await (const event of readChatSSE(
        url,
        { session_id: sessionIdRef.current, message: text },
        token
      )) {
        if (event.type === "message_stored" && sessionIdRef.current === null) {
          // Server assigned a session
        }

        if (event.type === "token") {
          const content = event.content as string;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? { ...m, content: m.content + content }
                : m
            )
          );
        }

        if (event.type === "tool_call") {
          const tc = {
            name: event.name as string,
            arguments: event.arguments as Record<string, unknown>,
          };
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    toolCalls: [...(m.toolCalls || []), tc],
                    content:
                      m.content ||
                      `🔧 Using tool: ${tc.name}...`,
                  }
                : m
            )
          );
        }

        if (event.type === "tool_result") {
          // Optionally show results
        }

        if (event.type === "error") {
          setError(event.content as string);
        }

        if (event.type === "done") {
          setIsLoading(false);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Chat failed");
      setIsLoading(false);
    }
  }, []);

  return (
    <ChatContext.Provider
      value={{
        isOpen,
        messages,
        isLoading,
        error,
        openChat,
        closeChat,
        sendMessage,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}
