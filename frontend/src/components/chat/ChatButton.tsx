"use client";

import { MessageSquare } from "lucide-react";
import { useChat } from "./ChatProvider";

export function ChatButton() {
  const { isOpen, openChat } = useChat();

  if (isOpen) return null;

  return (
    <button
      onClick={openChat}
      className="fixed bottom-6 right-6 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-brand-500 text-white shadow-lg shadow-brand-500/30 transition-all hover:bg-brand-400 hover:scale-105 active:scale-95"
      aria-label="Open chat"
    >
      <MessageSquare size={24} />
    </button>
  );
}
