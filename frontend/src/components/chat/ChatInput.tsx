"use client";

import { useState, useRef, useCallback } from "react";
import { Send } from "lucide-react";
import { useChat } from "./ChatProvider";

export function ChatInput() {
  const [text, setText] = useState("");
  const { sendMessage, isLoading } = useChat();
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed || isLoading) return;
    setText("");
    sendMessage(trimmed);
  }, [text, isLoading, sendMessage]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  // Auto-resize textarea
  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  };

  return (
    <div className="flex items-end gap-2">
      <textarea
        ref={textareaRef}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder="Ask about your data..."
        disabled={isLoading}
        rows={1}
        className="flex-1 resize-none rounded-lg border border-surface-700 bg-surface-900 px-3 py-2.5 text-sm text-surface-200 placeholder-surface-500 outline-none transition-colors focus:border-brand-500 disabled:opacity-50"
      />
      <button
        onClick={handleSubmit}
        disabled={!text.trim() || isLoading}
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:opacity-50 disabled:cursor-not-allowed"
        aria-label="Send message"
      >
        <Send size={16} />
      </button>
    </div>
  );
}
