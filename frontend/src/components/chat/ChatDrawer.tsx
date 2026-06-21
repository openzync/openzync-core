"use client";

import { useRef, useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useChat } from "./ChatProvider";
import { ChatMessage } from "./ChatMessage";
import { ChatInput } from "./ChatInput";

export function ChatDrawer() {
  const { isOpen, closeChat, messages, isLoading, error } = useChat();
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  return (
    <>
      {/* Backdrop */}
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm"
          onClick={closeChat}
        />
      )}

      {/* Drawer */}
      <div
        className={cn(
          "fixed right-0 top-0 z-50 flex h-full w-full flex-col border-l border-surface-800 bg-surface-950 shadow-2xl transition-transform duration-300 sm:w-[420px]",
          isOpen ? "translate-x-0" : "translate-x-full"
        )}
      >
        {/* Header */}
        <div className="flex h-14 items-center justify-between border-b border-surface-800 px-4">
          <div>
            <h2 className="text-sm font-semibold text-[#F2F2F2]">AI Chat</h2>
            <p className="text-[11px] text-surface-400">
              Ask about your OpenZep data
            </p>
          </div>
          <button
            onClick={closeChat}
            className="rounded-md p-1.5 text-surface-400 hover:bg-surface-800 hover:text-[#F2F2F2]"
          >
            <X size={18} />
          </button>
        </div>

        {/* Messages */}
        <div
          ref={scrollRef}
          className="flex-1 overflow-y-auto px-4 py-4 space-y-3"
        >
          {messages.length === 0 && !isLoading && (
            <div className="flex h-full items-center justify-center">
              <p className="text-sm text-surface-500 text-center max-w-[240px]">
                Send a message to get started. I can help you explore your
                OpenZep memory data.
              </p>
            </div>
          )}

          {messages.map((msg) => (
            <ChatMessage key={msg.id} message={msg} />
          ))}

          {isLoading && messages.length > 0 && (
            <div className="flex items-center gap-2 text-xs text-surface-400 pl-10">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500 [animation-delay:0.1s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-500 [animation-delay:0.2s]" />
            </div>
          )}

          {error && (
            <div className="rounded-md border border-error/30 bg-error/10 px-3 py-2 text-xs text-error">
              {error}
            </div>
          )}
        </div>

        {/* Input */}
        <div className="border-t border-surface-800 p-4">
          <ChatInput />
        </div>
      </div>
    </>
  );
}
