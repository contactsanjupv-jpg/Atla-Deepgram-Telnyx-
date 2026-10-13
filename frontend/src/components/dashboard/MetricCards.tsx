"use client";

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import type { DashboardSummary } from "@/lib/api";
import { formatUsage } from "@/lib/billing";
import { compare, type Delta } from "@/lib/dashboard";

function DeltaLine({ delta }: { delta: Delta }) {
  if (delta.direction === "none") return <p className="mt-1 h-[18px] text-[12.5px] text-[var(--color-ink-soft)]">&nbsp;</p>;
  const Icon = delta.direction === "up" ? ArrowUpRight : delta.direction === "down" ? ArrowDownRight : Minus;
  return (
    <p className="mt-1 flex items-center gap-1 text-[12.5px] text-[var(--color-ink-soft)]">
      <Icon className="h-3.5 w-3.5 flex-shrink-0" strokeWidth={1.75} aria-hidden />
      <span className="min-w-0 truncate">{delta.text}</span>
    </p>
  );
}

function Metric({ label, value, delta }: { label: string; value: string; delta: Delta }) {
  return (
    <div className="min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4 sm:p-5">
      <div className="text-[12.5px] text-[var(--color-ink-soft)]">{label}</div>
      <div className="mt-1 truncate text-[28px] font-bold leading-tight tracking-tight">{value}</div>
      <DeltaLine delta={delta} />
    </div>
  );
}

/** Calls handled, time on calls and new leads for the chosen range, each compared with the period before it. */
export function MetricCards({
  summary,
  loading,
  error,
  compareTo,
  onRetry,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
  error: string | null;
  compareTo: string;
  onRetry: () => void;
}) {
  if (error && !summary) {
    return (
      <div className="rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5" role="alert">
        <p className="text-[13.5px] text-[#7a2416]">{error}</p>
        <button onClick={onRetry} className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">
          Try again
        </button>
      </div>
    );
  }
  if (!summary) {
    return (
      <div className="grid gap-3 sm:grid-cols-3" aria-busy={loading} aria-label="Loading metrics">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-[108px] animate-pulse rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
        ))}
      </div>
    );
  }
  const { totals, previous } = summary;
  return (
    <div className={`grid gap-3 sm:grid-cols-3 ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
      <Metric label="Calls handled" value={totals.calls.toLocaleString()} delta={compare(totals.calls, previous.calls, compareTo)} />
      <Metric label="Time on calls" value={formatUsage(totals.call_seconds)} delta={compare(totals.call_seconds, previous.call_seconds, compareTo)} />
      <Metric label="New leads" value={totals.leads.toLocaleString()} delta={compare(totals.leads, previous.leads, compareTo)} />
    </div>
  );
}