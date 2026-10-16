"use client";

import { useEffect, useState } from "react";
import { api, describeError, TranscriptSegment } from "@/lib/api";

export function clock(seconds: number | null): string {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  return `${m}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

/** The conversation as Retell recorded it: who spoke, what they said, and when (only when Retell gave a time). */
export function TranscriptBody({ orgId, callId }: { orgId: string; callId: string }) {
  const [state, setState] = useState<{ callId: string; segments: TranscriptSegment[] | null; error: string | null } | null>(null);

  useEffect(() => {
    let live = true;
    api
      .getCallTranscript(orgId, callId)
      .then((t) => live && setState({ callId, segments: t.segments, error: null }))
      .catch((err) => live && setState({ callId, segments: null, error: describeError(err, "We couldn't load this conversation.") }));
    return () => {
      live = false;
    };
  }, [orgId, callId]);

  const current = state && state.callId === callId ? state : null;
  return (
    <div className="space-y-3">
      {current?.error && <p className="text-[13.5px] text-[#7a2416]">{current.error}</p>}
      {!current && <p className="text-[13.5px] text-[var(--color-ink-soft)]">Loading…</p>}
      {current?.segments?.length === 0 && <p className="text-[13.5px] text-[var(--color-ink-soft)]">No conversation was recorded for this call.</p>}
      {current?.segments?.map((s, i) => (
        <div key={i} className="text-[13.5px]">
          <div className="flex items-baseline gap-2 text-[12px] text-[var(--color-ink-soft)]">
            <span className="font-semibold text-[var(--color-ink)]">{s.speaker}</span>
            {s.start_seconds != null && <span>{clock(s.start_seconds)}</span>}
          </div>
          <p className="mt-0.5 break-words">{s.text}</p>
        </div>
      ))}
    </div>
  );
}
