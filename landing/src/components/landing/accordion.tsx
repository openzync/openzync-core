"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

interface AccordionItem {
  question: string;
  answer: string;
}

interface AccordionProps {
  items: AccordionItem[];
}

/**
 * FAQ accordion component. Click a question to expand/collapse the answer.
 * Only one item open at a time.
 */
export function Accordion({ items }: AccordionProps) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  if (items.length === 0) {
    return (
      <p className="text-center text-surface-500 py-12">
        No FAQ entries yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((item, i) => {
        const isOpen = openIndex === i;
        return (
          <div
            key={i}
            className={cn(
              "rounded-xl border transition-colors overflow-hidden",
              isOpen
                ? "border-brand-500/30 bg-surface-900"
                : "border-surface-800 bg-surface-900/50 hover:border-surface-700",
            )}
          >
            <button
              onClick={() => setOpenIndex(isOpen ? null : i)}
              className="flex w-full items-center justify-between px-6 py-4 text-left active:scale-[0.99] transition-transform duration-100"
            >
              <span className="text-sm font-medium text-text-primary pr-4">
                {item.question}
              </span>
              <ChevronDown
                size={16}
                className={cn(
                  "shrink-0 text-surface-500 transition-transform duration-200",
                  isOpen && "rotate-180",
                )}
              />
            </button>
            {isOpen && (
              <div className="px-6 pb-4">
                <p className="text-sm text-surface-400 leading-relaxed">
                  {item.answer}
                </p>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
