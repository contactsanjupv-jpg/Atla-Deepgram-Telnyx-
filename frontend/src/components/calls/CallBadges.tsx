"use client";

import type { Tone } from "@/lib/billingState";
import { LEAD_STATUS_LABEL, callStatus, sentimentView } from "@/lib/calls";
import type { CallRow } from "@/lib/api";
import { DOT, TEXT } from "@/components/dashboard/tone";

export function StatusBadge({ label, tone }: { label: string; tone: Tone }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap text-[12.5px] font-medium ${TEXT[tone]}`}>
      <span className={`h-2 w-2 flex-shrink-0 rounded-full ${DOT[tone]}`} aria-hidden />
      {label}
    </span>
  );
}

export function CallStatusBadge({ call }: { call: Pick<CallRow, "status" | "disconnect_reason" | "started_at"> }) {
  const s = callStatus(call);
  return <StatusBadge label={s.label} tone={s.tone} />;
}

export function SentimentBadge({ sentiment }: { sentiment: string | null }) {
  const v = sentimentView(sentiment);
  return v ? <StatusBadge label={v.label} tone={v.tone} /> : null;
}

export function LeadChip({ call }: { call: Pick<CallRow, "lead_id" | "lead_status"> }) {
  if (!call.lead_id) return <span className="text-[var(--color-ink-soft)]">—</span>;
  return (
    <span className="inline-flex whitespace-nowrap rounded-full bg-[var(--color-line)] px-2.5 py-0.5 text-[12px] font-medium">
      Lead · {LEAD_STATUS_LABEL[call.lead_status ?? ""] ?? call.lead_status}
    </span>
  );
}
