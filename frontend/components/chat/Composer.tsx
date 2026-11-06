"use client";

import { type KeyboardEvent, useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  disabled: boolean;
  placeholder: string;
}

export function Composer({ value, onChange, onSend, onStop, streaming, disabled, placeholder }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // Grow with the text, up to about eight lines.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter is a new line; never send mid-IME composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!streaming && !disabled && value.trim()) onSend();
    }
  }

  return (
    <div className="flex items-end gap-2 rounded-[var(--radius-bubble)] border border-rule bg-white p-2 focus-within:border-brand focus-within:ring-2 focus-within:ring-brand/20">
      <label htmlFor="composer" className="sr-only">
        Message
      </label>
      <textarea
        id="composer"
        ref={ref}
        rows={1}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        maxLength={4000}
        className="max-h-[200px] min-h-[40px] flex-1 resize-none bg-transparent focus-visible:outline-none px-2 py-2 text-[16px] leading-6 outline-none placeholder:text-muted"
      />
      {streaming ? (
        <button
          type="button"
          onClick={onStop}
          data-testid="stop"
          className="flex h-10 items-center gap-2 rounded-[var(--radius-control)] border border-rule px-3 text-sm font-medium hover:border-ink"
        >
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-[2px] bg-ink" />
          Stop
        </button>
      ) : (
        <button
          type="button"
          onClick={onSend}
          disabled={disabled || !value.trim()}
          className="h-10 rounded-[var(--radius-control)] bg-brand px-4 text-sm font-medium text-white disabled:opacity-40"
        >
          Send
        </button>
      )}
    </div>
  );
}
