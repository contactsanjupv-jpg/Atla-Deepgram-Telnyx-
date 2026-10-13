"use client";

import { RANGES, type DashboardRange } from "@/lib/dashboard";

export function RangeTabs({ value, onChange }: { value: DashboardRange; onChange: (r: DashboardRange) => void }) {
  return (
    <div role="tablist" aria-label="Time range" className="inline-flex rounded-full border border-[var(--color-line)] p-0.5">
      {RANGES.map((r) => (
        <button
          key={r.id}
          role="tab"
          aria-selected={value === r.id}
          onClick={() => onChange(r.id)}
          className={`rounded-full px-3.5 py-1.5 text-[13px] font-medium ${value === r.id ? "bg-[var(--color-ink)] text-[var(--color-paper)]" : "text-[var(--color-ink-soft)]"}`}
        >
          {r.label}
        </button>
      ))}
    </div>
  );
}