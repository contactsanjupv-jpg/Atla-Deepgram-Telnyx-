"use client";

import Link from "next/link";
import { Mail, Phone, PhoneCall } from "lucide-react";
import type { LeadRow } from "@/lib/api";
import { formatCallTime } from "@/lib/calls";
import { LEAD_STATUSES, LEAD_STATUS_TEXT, callBlurb, isLeadStatus, leadName, leadPhone, telHref } from "@/lib/leads";

const STATUS_COLORS: Record<string, string> = {
  new: "bg-[var(--color-ring-soft)] text-[#7a2416]",
  contacted: "bg-[#e8e2cf] text-[#5c5330]",
  booked: "bg-[var(--color-ok)]/15 text-[var(--color-ok)]",
  closed: "bg-[var(--color-line)] text-[var(--color-ink-soft)]",
};

/**
 * One lead, with only what was stored: a missing phone, email, reason or note is simply not shown.
 * The status is changed with the existing PATCH endpoint; a failure is shown on the card and the old status stays.
 */
export function LeadCard({
  lead,
  tz,
  saving,
  error,
  onStatus,
}: {
  lead: LeadRow;
  tz: string;
  saving: boolean;
  error: string | null;
  onStatus: (status: string) => void;
}) {
  const tel = telHref(lead.phone);
  const phone = leadPhone(lead.phone);
  const call = callBlurb(lead);
  const when = formatCallTime(lead.created_at, tz);
  const callWhen = formatCallTime(lead.call_started_at, tz);

  return (
    <li className="min-w-0 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5 sm:px-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words text-[15px] font-semibold">{leadName(lead)}</h3>
          {when && <p className="mt-0.5 text-[12.5px] text-[var(--color-ink-soft)]">Captured {when}</p>}
        </div>
        <select
          value={lead.status}
          onChange={(e) => onStatus(e.target.value)}
          disabled={saving}
          aria-label={`Status for ${leadName(lead)}`}
          className={`flex-shrink-0 rounded-full border-0 px-3 py-1.5 text-[12.5px] font-medium outline-none disabled:opacity-60 ${STATUS_COLORS[lead.status] ?? ""}`}
        >
          {!isLeadStatus(lead.status) && <option value={lead.status}>{lead.status}</option>}
          {LEAD_STATUSES.map((s) => (
            <option key={s} value={s}>
              {LEAD_STATUS_TEXT[s]}
            </option>
          ))}
        </select>
      </div>

      {(phone || lead.email) && (
        <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1.5 text-[13.5px]">
          {phone && (
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <Phone className="h-3.5 w-3.5 flex-shrink-0 text-[var(--color-ink-soft)]" strokeWidth={1.75} aria-hidden />
              {tel ? (
                <a href={tel} className="underline-offset-2 hover:underline">
                  {phone}
                </a>
              ) : (
                phone
              )}
            </span>
          )}
          {lead.email && (
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <Mail className="h-3.5 w-3.5 flex-shrink-0 text-[var(--color-ink-soft)]" strokeWidth={1.75} aria-hidden />
              <a href={`mailto:${lead.email}`} className="min-w-0 break-all underline-offset-2 hover:underline">
                {lead.email}
              </a>
            </span>
          )}
        </div>
      )}

      {lead.reason && (
        <p className="mt-2.5 break-words text-[13.5px]">
          <span className="text-[var(--color-ink-soft)]">Reason: </span>
          {lead.reason}
        </p>
      )}
      {lead.summary && (
        <p className="mt-1.5 break-words text-[13px] text-[var(--color-ink-soft)]">
          <span className="font-medium text-[var(--color-ink)]">Notes: </span>
          {lead.summary}
        </p>
      )}

      {call && lead.call_id && (
        <Link href={`/dashboard/calls?call=${encodeURIComponent(lead.call_id)}`} className="mt-2.5 inline-flex flex-wrap items-center gap-x-1.5 text-[13px] font-medium underline-offset-2 hover:underline">
          <PhoneCall className="h-3.5 w-3.5" strokeWidth={1.75} aria-hidden />
          View the call
          <span className="font-normal text-[var(--color-ink-soft)]">
            · {call}
            {callWhen ? ` · ${callWhen}` : ""}
          </span>
        </Link>
      )}

      {error && (
        <p role="alert" className="mt-2 text-[13px] text-[#7a2416]">
          {error}
        </p>
      )}
    </li>
  );
}
