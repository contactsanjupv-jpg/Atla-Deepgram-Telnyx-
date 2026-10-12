"use client";

import { useEffect, useState } from "react";
import { Phone } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, CallOut, CallSearchHit, describeError } from "@/lib/api";
import { useFeatures } from "@/lib/useFeatures";
import { TranscriptDialog, clock } from "@/components/calls/TranscriptDialog";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function CallsPage() {
  const { org, loading, error } = useOrganization();
  const [calls, setCalls] = useState<CallOut[]>([]);
  const [dataLoading, setDataLoading] = useState(true);
  const [dataError, setDataError] = useState<string | null>(null);
  const { features } = useFeatures(org?.id);
  const hasTranscripts = features?.has("transcripts") ?? false;
  const hasSearch = features?.has("transcript_search") ?? false;
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<CallSearchHit[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  async function runSearch(e: React.FormEvent) {
    e.preventDefault();
    if (!org || q.trim().length < 2) return;
    setSearchError(null);
    try {
      setHits(await api.searchCalls(org.id, q.trim()));
    } catch (err) {
      setHits(null);
      setSearchError(describeError(err, "Search didn't work. Try again."));
    }
  }

  useEffect(() => {
    if (!org) return;
    api
      .listCalls(org.id, { limit: 50 })
      .then(setCalls)
      .catch(() => setDataError("Couldn't load calls."))
      .finally(() => setDataLoading(false));
  }, [org]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Calls</h1>

      {features && !hasTranscripts && (
        <p className="mt-2 text-[13.5px] text-[var(--color-ink-soft)]">Full conversations and search are part of the Growth plan. Summaries are included on every plan.</p>
      )}
      {hasSearch && (
        <form onSubmit={runSearch} className="mt-4 flex gap-2" role="search">
          <input
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              if (!e.target.value.trim()) setHits(null);
            }}
            maxLength={100}
            placeholder="Search what callers said"
            aria-label="Search conversations"
            className="min-w-0 flex-1 rounded-full border border-[var(--color-line)] bg-transparent px-4 py-2 text-[14px] outline-none focus:border-[var(--color-ink)]"
          />
          <button disabled={q.trim().length < 2} className="rounded-full bg-[var(--color-ink)] px-4 py-2 text-[13.5px] font-medium text-[var(--color-paper)] disabled:opacity-50">
            Search
          </button>
        </form>
      )}
      {searchError && <p className="mt-3 text-[13.5px] text-[#7a2416]">{searchError}</p>}
      {hits && (
        <div className="mt-4 space-y-2">
          {hits.length === 0 && <p className="text-[13.5px] text-[var(--color-ink-soft)]">No conversations mention “{q.trim()}”.</p>}
          {hits.map((h) => (
            <button key={h.call_id} onClick={() => setOpen(h.call_id)} className="block w-full min-w-0 rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3 text-left">
              <div className="text-[13.5px] font-medium">
                {h.caller_number || "Unknown caller"}
                {h.started_at && <span className="ml-2 font-normal text-[var(--color-ink-soft)]">{new Date(h.started_at).toLocaleDateString()}</span>}
              </div>
              {h.matches.map((m, i) => (
                <p key={i} className="mt-1 break-words text-[13px] text-[var(--color-ink-soft)]">
                  <span className="font-medium text-[var(--color-ink)]">{m.speaker}</span>
                  {m.start_seconds != null && ` ${clock(m.start_seconds)}`}: {m.snippet}
                </p>
              ))}
            </button>
          ))}
        </div>
      )}

      {dataLoading && <p className="mt-6 text-[14px] text-[var(--color-ink-soft)]">Loading…</p>}
      {dataError && <p className="mt-6 text-[14px] text-[var(--color-ring)]">{dataError}</p>}

      {!dataLoading && !dataError && calls.length === 0 && (
        <div className="mt-10 flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
          <Phone className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} />
          <h2 className="mt-4 text-[16px] font-semibold">No calls yet</h2>
          <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">
            Calls will appear here once your receptionist is active and answering.
          </p>
        </div>
      )}

      {!dataLoading && calls.length > 0 && (
        <div className="relative mt-6 overflow-x-auto rounded-xl border border-[var(--color-line)]">
          <table className="w-full min-w-[640px] text-left text-[13.5px]">
            <thead className="border-b border-[var(--color-line)] bg-[var(--color-paper-raised)] text-[12px] uppercase tracking-wide text-[var(--color-ink-soft)]">
              <tr>
                <th className="px-4 py-3 font-medium">Caller</th>
                <th className="px-4 py-3 font-medium">Direction</th>
                <th className="px-4 py-3 font-medium">Duration</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Summary</th>
                {hasTranscripts && <th className="px-4 py-3 font-medium"><span className="sr-only">Conversation</span></th>}
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} className="border-b border-[var(--color-line)] last:border-0">
                  <td className="px-4 py-3">{c.caller_number || "Unknown"}</td>
                  <td className="px-4 py-3 capitalize">{c.direction}</td>
                  <td className="px-4 py-3">{formatDuration(c.duration_seconds)}</td>
                  <td className="px-4 py-3">{c.status || "—"}</td>
                  <td className="max-w-xs truncate px-4 py-3 text-[var(--color-ink-soft)]">{c.summary || "—"}</td>
                  {hasTranscripts && (
                    <td className="px-4 py-3">
                      <button onClick={() => setOpen(c.id)} className="whitespace-nowrap text-[13px] font-medium underline">View conversation</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {org && open && <TranscriptDialog orgId={org.id} callId={open} onClose={() => setOpen(null)} />}
    </DashboardShell>
  );
}