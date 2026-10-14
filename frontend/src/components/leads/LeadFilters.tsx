"use client";

import { Search, X } from "lucide-react";
import { LEAD_STATUSES, LEAD_STATUS_TEXT, type LeadFilterState, type LeadSort } from "@/lib/leads";

/** Search, one tab per status (each with the number of leads it would show) and sort. All handled by the server. */
export function LeadFilters({
  value,
  searchText,
  counts,
  total,
  onSearchText,
  onChange,
  onClear,
}: {
  value: LeadFilterState;
  searchText: string;
  counts: Record<string, number> | null;
  total: number | null;
  onSearchText: (q: string) => void;
  onChange: (patch: Partial<LeadFilterState>) => void;
  onClear: () => void;
}) {
  const all = counts ? Object.values(counts).reduce((a, b) => a + b, 0) : total;
  const tabs: { id: "" | (typeof LEAD_STATUSES)[number]; label: string; n: number | null | undefined }[] = [
    { id: "", label: "All", n: all },
    ...LEAD_STATUSES.map((s) => ({ id: s, label: LEAD_STATUS_TEXT[s], n: counts?.[s] })),
  ];
  const dirty = searchText.trim() !== "" || value.status !== "" || value.sort !== "newest";

  return (
    <div role="search" aria-label="Filter leads">
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-ink-soft)]" strokeWidth={1.75} aria-hidden />
          <input
            value={searchText}
            onChange={(e) => onSearchText(e.target.value)}
            maxLength={100}
            placeholder="Search name, phone, email or reason"
            aria-label="Search leads"
            className="w-full rounded-full border border-[var(--color-line)] bg-transparent py-2.5 pl-10 pr-4 text-[14px] outline-none focus:border-[var(--color-ink)]"
          />
        </div>
        <label className="flex flex-shrink-0 items-center gap-2 text-[13px] text-[var(--color-ink-soft)]">
          Sort
          <select
            value={value.sort}
            onChange={(e) => onChange({ sort: e.target.value as LeadSort })}
            className="rounded-full border border-[var(--color-line)] bg-transparent px-3.5 py-2 text-[13.5px] text-[var(--color-ink)] outline-none focus:border-[var(--color-ink)]"
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </div>

      <div role="tablist" aria-label="Lead status" className="mt-3 flex gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none]">
        {tabs.map((t) => (
          <button
            key={t.id || "all"}
            role="tab"
            aria-selected={value.status === t.id}
            onClick={() => onChange({ status: t.id })}
            className={`flex-shrink-0 whitespace-nowrap rounded-full border px-3.5 py-1.5 text-[13px] font-medium ${
              value.status === t.id ? "border-[var(--color-ink)] bg-[var(--color-ink)] text-[var(--color-paper)]" : "border-[var(--color-line)] text-[var(--color-ink-soft)]"
            }`}
          >
            {t.label}
            {t.n != null && <span className="ml-1.5 tabular-nums opacity-70">{t.n.toLocaleString()}</span>}
          </button>
        ))}
      </div>

      {dirty && (
        <button type="button" onClick={onClear} className="mt-2 inline-flex items-center gap-1 text-[13px] font-medium underline underline-offset-2">
          <X className="h-3.5 w-3.5" aria-hidden /> Clear filters
        </button>
      )}
    </div>
  );
}