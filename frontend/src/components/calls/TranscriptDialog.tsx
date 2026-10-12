"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { api, describeError, TranscriptSegment } from "@/lib/api";

export function clock(seconds: number | null): string {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  return `${m}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

/** The full conversation as Retell recorded it: who spoke, what they said, and when (only when Retell gave a time). */
export function TranscriptDialog({ orgId, callId, onClose }: { orgId: string; callId: string; onClose: () => void }) {
  const [segments, setSegments] = useState<TranscriptSegment[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .getCallTranscript(orgId, callId)
      .then((t) => live && setSegments(t.segments))
      .catch((err) => live && setError(describeError(err, "We couldn't load this conversation.")));
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      live = false;
      window.removeEventListener("keydown", onKey);
    };
  }, [orgId, callId, onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center" role="dialog" aria-modal="true" aria-label="Conversation">
      <button type="button" aria-label="Close" tabIndex={-1} className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative flex max-h-[85vh] w-full flex-col rounded-t-2xl bg-[var(--color-paper-raised)] p-5 shadow-xl sm:max-w-lg sm:rounded-2xl">
        <div className="flex items-center justify-between">
          <h2 className="text-[16px] font-semibold">Conversation</h2>
          <button onClick={onClose} aria-label="Close" className="rounded-lg p-1.5 hover:bg-[var(--color-line)]">
            <X className="h-5 w-5" strokeWidth={1.75} />
          </button>
        </div>
        <div className="mt-3 min-h-0 flex-1 space-y-3 overflow-y-auto">
          {error && <p className="text-[13.5px] text-[#7a2416]">{error}</p>}
          {!error && segments === null && <p className="text-[13.5px] text-[var(--color-ink-soft)]">Loading…</p>}
          {segments?.length === 0 && <p className="text-[13.5px] text-[var(--color-ink-soft)]">No conversation was recorded for this call.</p>}
          {segments?.map((s, i) => (
            <div key={i} className="text-[13.5px]">
              <div className="flex items-baseline gap-2 text-[12px] text-[var(--color-ink-soft)]">
                <span className="font-semibold text-[var(--color-ink)]">{s.speaker}</span>
                {s.start_seconds != null && <span>{clock(s.start_seconds)}</span>}
              </div>
              <p className="mt-0.5 break-words">{s.text}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
