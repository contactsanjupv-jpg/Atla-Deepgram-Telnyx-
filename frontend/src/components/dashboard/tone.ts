export type Tone = "ok" | "warn" | "bad" | "neutral";

export const DOT: Record<Tone, string> = {
  ok: "bg-[var(--color-ok)]",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  neutral: "bg-[var(--color-ink-soft)]/50",
};

export const TEXT: Record<Tone, string> = {
  ok: "text-[var(--color-ok)]",
  warn: "text-amber-700",
  bad: "text-red-600",
  neutral: "text-[var(--color-ink-soft)]",
};