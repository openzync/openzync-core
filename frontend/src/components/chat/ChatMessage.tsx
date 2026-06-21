"use client";

import { cn } from "@/lib/utils";
import type { ChatMessage as ChatMessageType } from "./ChatProvider";

interface ChatMessageProps {
  message: ChatMessageType;
}

export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={cn(
        "flex",
        isUser ? "justify-end" : "justify-start"
      )}
    >
      <div
        className={cn(
          "max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed",
          isUser
            ? "bg-brand-500/20 text-brand-200"
            : "bg-surface-800 text-surface-200"
        )}
      >
        {/* Tool calls indicator */}
        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mb-1.5 space-y-1">
            {message.toolCalls.map((tc, i) => (
              <div
                key={i}
                className="flex items-center gap-1.5 rounded bg-surface-700/50 px-2 py-1 text-[11px] text-surface-300"
              >
                <span className="text-brand-400">🔧</span>
                <span className="font-medium">{tc.name}</span>
                <span className="text-surface-500">
                  (
                  {Object.keys(tc.arguments).length > 0
                    ? `${Object.keys(tc.arguments).length} args`
                    : "no args"}
                  )
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Message content */}
        <div className="whitespace-pre-wrap break-words">
          {message.content || (isUser ? "" : "Thinking...")}
        </div>
      </div>
    </div>
  );
}
