"use client";

import { useState } from "react";
import { api, describeError, type CallSearchHit } from "@/lib/api";
import { MIN_SEARCH_CHARS, callerLabel, formatCallTime } from "@/lib/calls";
import { clock } from "./TranscriptDialog";

/** Growth: search what callers and the receptionist actually said. Separate from the number/summary filter above. */
export function ConversationSearch({ orgId, tz, onOpen }: { orgId: string; tz: string; onOpen: (callId: string) => void }) {
  const [q, setQ] = useState("");
  const [asked, setAsked] = useState("");
  const [hits, setHits] = useState<CallSearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const term = q.trim();
    if (term.length < MIN_SEARCH_CHARS) return;
    setBusy(true);
    setError(null);
    try {
      setHits(await api.searchCalls(orgId, term));
      setAsked(term);
    } catch (err) {
      setHits(null);
      setError(describeError(err, "Search didn't work. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Search conversations" className="rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4 sm:p-5">
      <h2 className="text-[14px] font-semibold">Search conversations</h2>
      <p className="mt-0.5 text-[12.5px] text-[var(--color-ink-soft)]">Find a call by something that was said, like a service, a name or a price.</p>
      <form onSubmit={run} className="mt-3 flex gap-2">
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            if (!e.target.value.trim()) setHits(null);
          }}
          maxLength={100}
          placeholder="e.g. whitening"
          aria-label="Search what was said"
          className="min-w-0 flex-1 rounded-full border border-[var(--color-line)] bg-transparent px-4 py-2 text-[14px] outline-none focus:border-[var(--color-ink)]"
        />
        <button disabled={busy || q.trim().length < MIN_SEARCH_CHARS} className="rounded-full bg-[var(--color-ink)] px-4 py-2 text-[13.5px] font-medium text-[var(--color-paper)] disabled:opacity-50">
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-3 text-[13.5px] text-[#7a2416]">
          {error}
        </p>
      )}
      {hits && (
        <div className="mt-3 space-y-2">
          {hits.length === 0 && <p className="text-[13.5px] text-[var(--color-ink-soft)]">No conversations mention “{asked}”.</p>}
          {hits.map((h) => (
            <button key={h.call_id} onClick={() => onOpen(h.call_id)} className="block w-full min-w-0 rounded-lg border border-[var(--color-line)] px-4 py-3 text-left">
              <div className="text-[13.5px] font-medium">
                {callerLabel(h)}
                {h.started_at && <span className="ml-2 font-normal text-[var(--color-ink-soft)]">{formatCallTime(h.started_at, tz)}</span>}
              </div>
              {h.matches.map((m, i) => (
                <p key={i} className="mt-1 break-words text-[13px] text-[var(--color-ink-soft)]">
                  <span className="font-medium text-[var(--color-ink)]">{m.speaker}</span>
                  {m.start_seconds != null && ` ${clock(m.start_seconds)}`}: {m.snippet}
                </p>
              ))}
              {h.match_count > h.matches.length && <p className="mt-1 text-[12px] text-[var(--color-ink-soft)]">+{h.match_count - h.matches.length} more in this call</p>}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
