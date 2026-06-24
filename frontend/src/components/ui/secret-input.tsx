"use client";

import { Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/utils";

interface SecretInputProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  visible: boolean;
  onToggleVisibility: () => void;
}

export function SecretInput({
  label,
  value,
  onChange,
  placeholder,
  visible,
  onToggleVisibility,
}: SecretInputProps) {
  const isEmpty = !value;

  return (
    <div>
      <label className="block text-sm font-medium text-surface-300 mb-1.5">
        {label}
        {isEmpty && (
          <span className="ml-2 text-[10px] font-medium text-error uppercase tracking-wider">Required</span>
        )}
      </label>
      <div className="relative">
        <input
          className={cn(
            "input-base pr-10 w-full",
            isEmpty && "border-error/40 focus:border-error",
          )}
          type={visible ? "text" : "password"}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          onClick={onToggleVisibility}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
        >
          {visible ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
      </div>
    </div>
  );
}
