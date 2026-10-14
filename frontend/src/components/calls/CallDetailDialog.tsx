"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { X } from "lucide-react";
import { api, describeError, type CallRow } from "@/lib/api";
import { DIRECTION_LABEL, callerLabel, endedBecause, formatCallTime, formatDuration, LEAD_STATUS_LABEL, sentimentView } from "@/lib/calls";
import { CallStatusBadge, SentimentBadge } from "./CallBadges";
import { TranscriptBody } from "./TranscriptDialog";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-[var(--color-ink-soft)]">{label}</dt>
      <dd className="mt-0.5 break-words text-[13.5px]">{children}</dd>
    </div>
  );
}

/**
 * One call: what happened, the summary, the lead it produced and — on Growth — the full conversation.
 * Opens from a row we already have, or from just an id (search results, a shared link) by loading the call.
 */
export function CallDetailDialog({
  orgId,
  callId,
  initial,
  tz,
  canReadConversation,
  planKnown,
  onClose,
}: {
  orgId: string;
  callId: string;
  initial: CallRow | null;
  tz: string;
  canReadConversation: boolean;
  planKnown: boolean;
  onClose: () => void;
}) {
  const [loaded, setLoaded] = useState<{ id: string; call: CallRow | null; error: string | null } | null>(null);

  useEffect(() => {
    if (initial?.id === callId) return;
    let live = true;
    api
      .getCall(orgId, callId)
      .then((call) => live && setLoaded({ id: callId, call, error: null }))
      .catch((err) => live && setLoaded({ id: callId, call: null, error: describeError(err, "We couldn't load this call.") }));
    return () => {
      live = false;
    };
  }, [orgId, callId, initial]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  const fetched = loaded && loaded.id === callId ? loaded : null;
  const call = initial?.id === callId ? initial : (fetched?.call ?? null);
  const end = call ? endedBecause(call.disconnect_reason) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:justify-end" role="dialog" aria-modal="true" aria-label="Call details">
      <button type="button" aria-label="Close" tabIndex={-1} className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative flex max-h-[92vh] w-full flex-col rounded-t-2xl bg-[var(--color-paper-raised)] shadow-xl sm:h-full sm:max-h-none sm:max-w-md sm:rounded-none sm:rounded-l-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-[var(--color-line)] px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-[16px] font-semibold">{call ? callerLabel(call) : "Call details"}</h2>
            {call && <p className="mt-0.5 text-[12.5px] text-[var(--color-ink-soft)]">{formatCallTime(call.started_at, tz) ?? "Time not recorded"}</p>}
          </div>
          <button onClick={onClose} aria-label="Close" className="rounded-lg p-1.5 hover:bg-[var(--color-line)]">
            <X className="h-5 w-5" strokeWidth={1.75} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {!call && !fetched && <p className="text-[13.5px] text-[var(--color-ink-soft)]">Loading…</p>}
          {!call && fetched?.error && <p className="text-[13.5px] text-[#7a2416]">{fetched.error}</p>}
          {call && (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3.5">
                <Fact label="Status">
                  <CallStatusBadge call={call} />
                </Fact>
                <Fact label="Length">{formatDuration(call.duration_seconds)}</Fact>
                <Fact label="Type">{DIRECTION_LABEL[call.direction] ?? call.direction}</Fact>
                <Fact label="Caller mood">{sentimentView(call.sentiment) ? <SentimentBadge sentiment={call.sentiment} /> : "—"}</Fact>
                <Fact label="Started">{formatCallTime(call.started_at, tz) ?? "—"}</Fact>
                <Fact label="Ended">{formatCallTime(call.ended_at, tz) ?? "—"}</Fact>
                {end && <Fact label="How it ended">{end}</Fact>}
              </dl>

              <h3 className="mt-6 text-[13.5px] font-semibold">Summary</h3>
              <p className="mt-1.5 break-words text-[13.5px] text-[var(--color-ink-soft)]">{call.summary || "No summary was recorded for this call."}</p>

              <h3 className="mt-6 text-[13.5px] font-semibold">Lead</h3>
              {call.lead_id ? (
                <p className="mt-1.5 text-[13.5px]">
                  {call.lead_name || "Unnamed lead"} · {LEAD_STATUS_LABEL[call.lead_status ?? ""] ?? call.lead_status}{" "}
                  <Link href="/dashboard/leads" className="font-medium underline underline-offset-2">
                    Open leads
                  </Link>
                </p>
              ) : (
                <p className="mt-1.5 text-[13.5px] text-[var(--color-ink-soft)]">This call didn&apos;t produce a lead.</p>
              )}

              <h3 className="mt-6 text-[13.5px] font-semibold">Conversation</h3>
              <div className="mt-2">
                {canReadConversation ? (
                  <TranscriptBody orgId={orgId} callId={call.id} />
                ) : planKnown ? (
                  <p className="text-[13.5px] text-[var(--color-ink-soft)]">The full conversation, with who said what and when, is part of the Growth plan. The summary above is included on every plan.</p>
                ) : (
                  <p className="text-[13.5px] text-[var(--color-ink-soft)]">Loading…</p>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}