"use client";

import { useState } from "react";
import { SlidersHorizontal, Search, X } from "lucide-react";
import { DEFAULT_FILTERS, PRESETS, activeFilterCount, filterProblem, type CallFilterState } from "@/lib/calls";

const field = "w-full rounded-lg border border-[var(--color-line)] bg-transparent px-3 py-2 text-[13.5px] outline-none focus:border-[var(--color-ink)]";

function Select<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-[12px] text-[var(--color-ink-soft)]">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value as T)} className={field}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * Search plus filters. Every option maps to a real filter on the server. On a phone the filters fold behind
 * one button (with a count) so the list stays in view.
 */
export function CallFilters({
  value,
  searchText,
  onSearchText,
  onChange,
  onClear,
}: {
  value: CallFilterState;
  searchText: string;
  onSearchText: (q: string) => void;
  onChange: (patch: Partial<CallFilterState>) => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const count = activeFilterCount(value);
  const problem = filterProblem(value);
  const dirty = count > 0 || value.sort !== DEFAULT_FILTERS.sort || searchText.trim() !== "";

  return (
    <div role="search" aria-label="Filter calls">
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-ink-soft)]" strokeWidth={1.75} aria-hidden />
          <input
            value={searchText}
            onChange={(e) => onSearchText(e.target.value)}
            maxLength={100}
            placeholder="Search by phone number or summary"
            aria-label="Search calls by phone number or summary"
            className="w-full rounded-full border border-[var(--color-line)] bg-transparent py-2.5 pl-10 pr-4 text-[14px] outline-none focus:border-[var(--color-ink)]"
          />
        </div>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-controls="call-filter-panel"
          className="flex flex-shrink-0 items-center gap-1.5 rounded-full border border-[var(--color-line)] px-4 py-2.5 text-[13.5px] font-medium md:hidden"
        >
          <SlidersHorizontal className="h-4 w-4" strokeWidth={1.75} aria-hidden />
          Filters{count > 0 ? ` (${count})` : ""}
        </button>
      </div>

      <div id="call-filter-panel" className={`${open ? "grid" : "hidden"} mt-3 grid-cols-2 gap-3 md:grid md:grid-cols-4 lg:grid-cols-6`}>
        <Select label="Date" value={value.preset} onChange={(preset) => onChange({ preset })} options={PRESETS.map((p) => ({ value: p.id, label: p.label }))} />
        <Select
          label="Type"
          value={value.direction}
          onChange={(direction) => onChange({ direction })}
          options={[
            { value: "", label: "All types" },
            { value: "inbound", label: "Inbound" },
            { value: "outbound", label: "Outbound" },
            { value: "test", label: "Test (browser)" },
          ]}
        />
        <Select
          label="Status"
          value={value.status}
          onChange={(status) => onChange({ status })}
          options={[
            { value: "", label: "Any status" },
            { value: "ended", label: "Ended" },
            { value: "ongoing", label: "In progress" },
            { value: "registered", label: "Connecting" },
            { value: "error", label: "Failed" },
          ]}
        />
        <Select
          label="Caller mood"
          value={value.sentiment}
          onChange={(sentiment) => onChange({ sentiment })}
          options={[
            { value: "", label: "Any" },
            { value: "positive", label: "Positive" },
            { value: "neutral", label: "Neutral" },
            { value: "negative", label: "Negative" },
            { value: "unknown", label: "Not recorded" },
          ]}
        />
        <Select
          label="Leads"
          value={value.lead}
          onChange={(lead) => onChange({ lead })}
          options={[
            { value: "", label: "All calls" },
            { value: "with", label: "Produced a lead" },
            { value: "without", label: "No lead" },
          ]}
        />
        <Select
          label="Sort by"
          value={value.sort}
          onChange={(sort) => onChange({ sort })}
          options={[
            { value: "newest", label: "Newest first" },
            { value: "oldest", label: "Oldest first" },
            { value: "longest", label: "Longest first" },
          ]}
        />
        {value.preset === "custom" && (
          <>
            <label className="block min-w-0">
              <span className="mb-1 block text-[12px] text-[var(--color-ink-soft)]">From</span>
              <input type="date" value={value.dateFrom} max={value.dateTo || undefined} onChange={(e) => onChange({ dateFrom: e.target.value })} className={field} />
            </label>
            <label className="block min-w-0">
              <span className="mb-1 block text-[12px] text-[var(--color-ink-soft)]">To</span>
              <input type="date" value={value.dateTo} min={value.dateFrom || undefined} onChange={(e) => onChange({ dateTo: e.target.value })} className={field} />
            </label>
          </>
        )}
      </div>

      {problem && (
        <p role="alert" className="mt-2 text-[13px] text-[#7a2416]">
          {problem}
        </p>
      )}
      {dirty && (
        <button type="button" onClick={onClear} className="mt-3 inline-flex items-center gap-1 text-[13px] font-medium underline underline-offset-2">
          <X className="h-3.5 w-3.5" aria-hidden /> Clear filters
        </button>
      )}
    </div>
  );
}
