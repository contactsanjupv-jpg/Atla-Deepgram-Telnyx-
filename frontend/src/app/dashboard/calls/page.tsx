"use client";

import { useEffect, useMemo, useState } from "react";
import { Phone, SearchX } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, type CallRow } from "@/lib/api";
import { browserTimeZone } from "@/lib/credits";
import {
  DEFAULT_FILTERS,
  PAGE_SIZE,
  activeFilterCount,
  clampOffset,
  filterProblem,
  formatTotalTime,
  pageInfo,
  toQuery,
  type CallFilterState,
} from "@/lib/calls";
import { useFeatures } from "@/lib/useFeatures";
import { useLoad } from "@/lib/useLoad";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";
import { CallFilters } from "@/components/calls/CallFilters";
import { CallList } from "@/components/calls/CallList";
import { CallDetailDialog } from "@/components/calls/CallDetailDialog";
import { ConversationSearch } from "@/components/calls/ConversationSearch";

const SEARCH_DEBOUNCE_MS = 350;

export default function CallsPage() {
  const { org, loading, error } = useOrganization();
  const orgId = org?.id;
  const { features } = useFeatures(orgId);
  const canReadConversation = features?.has("transcripts") ?? false;
  const canSearchConversations = features?.has("transcript_search") ?? false;

  const [filters, setFilters] = useState<CallFilterState>(DEFAULT_FILTERS);
  const [searchText, setSearchText] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<{ id: string; row: CallRow | null } | null>(null);
  // The timezone is read after mount so server and browser render the same first frame.
  const [tz, setTz] = useState("UTC");
  // A shared link (?call=<id>) opens that call. Both reads happen in a callback, after the first paint.
  useEffect(() => {
    const t = setTimeout(() => {
      setTz(browserTimeZone());
      const id = new URLSearchParams(window.location.search).get("call");
      if (id) setOpen({ id, row: null });
    }, 0);
    return () => clearTimeout(t);
  }, []);

  // Typing in the search box applies after a short pause, and always returns to the first page.
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((f) => (f.q === searchText ? f : { ...f, q: searchText }));
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchText]);

  const problem = filterProblem(filters);
  const query = useMemo(() => toQuery(filters, tz, offset), [filters, tz, offset]);
  const page = useLoad(orgId && !problem ? `${orgId}:${JSON.stringify(query)}` : null, () => api.browseCalls(orgId!, query), "We couldn't load your calls.");

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  const data = page.data ?? page.last;
  const stale = page.loading && page.data === null;
  const narrowed = activeFilterCount(filters) > 0;

  function change(patch: Partial<CallFilterState>) {
    setFilters((f) => ({ ...f, ...patch }));
    setOffset(0);
  }
  function clear() {
    setFilters(DEFAULT_FILTERS);
    setSearchText("");
    setOffset(0);
  }
  function show(id: string, row: CallRow | null) {
    setOpen({ id, row });
    window.history.replaceState(null, "", `?call=${encodeURIComponent(id)}`);
  }
  function hide() {
    setOpen(null);
    window.history.replaceState(null, "", window.location.pathname);
  }

  // A filter change (or fewer calls than before) can leave the offset past the end: step back to the last page.
  const fixedOffset = data ? clampOffset(offset, data.total, PAGE_SIZE) : offset;
  const info = data ? pageInfo(data.total, data.offset, data.limit, data.items.length) : null;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Calls</h1>
      <p className="mt-1 text-[14.5px] text-[var(--color-ink-soft)]" aria-live="polite">
        {data
          ? data.total === 0
            ? narrowed ? "No calls match." : "No calls yet."
            : `${data.total.toLocaleString()} ${narrowed ? "matching " : ""}${data.total === 1 ? "call" : "calls"} · ${formatTotalTime(data.total_seconds)} on the phone · ${data.leads_captured.toLocaleString()} ${data.leads_captured === 1 ? "lead" : "leads"} captured`
          : page.error
            ? ""
            : "Loading…"}
      </p>

      {features && !canReadConversation && (
        <p className="mt-2 text-[13.5px] text-[var(--color-ink-soft)]">Full conversations and conversation search are part of the Growth plan. Summaries are included on every plan.</p>
      )}

      <div className="mt-5">
        <CallFilters value={filters} searchText={searchText} onSearchText={setSearchText} onChange={change} onClear={clear} />
      </div>

      {canSearchConversations && orgId && (
        <div className="mt-5">
          <ConversationSearch orgId={orgId} tz={tz} onOpen={(id) => show(id, null)} />
        </div>
      )}

      <div className="mt-6" aria-busy={page.loading}>
        {page.error && !data && (
          <div role="alert" className="rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5">
            <p className="text-[13.5px] text-[#7a2416]">{page.error}</p>
            <button onClick={page.retry} className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">
              Try again
            </button>
          </div>
        )}

        {!data && !page.error && !problem && (
          <div className="space-y-2" aria-label="Loading calls">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="h-[68px] animate-pulse rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
            ))}
          </div>
        )}

        {data && data.total === 0 && !narrowed && (
          <div className="flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
            <Phone className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} aria-hidden />
            <h2 className="mt-4 text-[16px] font-semibold">No calls yet</h2>
            <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">Calls will appear here once your receptionist is active and answering.</p>
          </div>
        )}

        {data && data.total === 0 && narrowed && (
          <div className="flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-14 text-center">
            <SearchX className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} aria-hidden />
            <h2 className="mt-4 text-[16px] font-semibold">No calls match these filters</h2>
            <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">Try a wider date range or remove a filter.</p>
            <button onClick={clear} className="mt-4 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">
              Clear filters
            </button>
          </div>
        )}

        {data && data.total > 0 && data.items.length === 0 && (
          <div className="rounded-xl border border-[var(--color-line)] p-5 text-[13.5px]">
            That page is past the last call.{" "}
            <button onClick={() => setOffset(fixedOffset)} className="font-medium underline">
              Go to the last page
            </button>
          </div>
        )}

        {data && data.items.length > 0 && (
          <div className={stale ? "opacity-60 transition-opacity" : ""}>
            <CallList calls={data.items} tz={tz} onOpen={(c) => show(c.id, c)} />
            {page.error && <p className="mt-3 text-[13px] text-[#7a2416]">{page.error}</p>}
            {info && (
              <nav aria-label="Pages" className="mt-4 flex items-center justify-between gap-3">
                <span className="text-[13px] text-[var(--color-ink-soft)]">
                  {info.from.toLocaleString()}–{info.to.toLocaleString()} of {data.total.toLocaleString()}
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={!info.hasPrev || page.loading}
                    onClick={() => setOffset(Math.max(0, data.offset - PAGE_SIZE))}
                    className="rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium disabled:opacity-40"
                  >
                    Previous
                  </button>
                  <button
                    disabled={!info.hasNext || page.loading}
                    onClick={() => setOffset(data.offset + PAGE_SIZE)}
                    className="rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
              </nav>
            )}
          </div>
        )}
      </div>

      {orgId && open && (
        <CallDetailDialog
          orgId={orgId}
          callId={open.id}
          initial={open.row}
          tz={tz}
          canReadConversation={canReadConversation}
          planKnown={features !== null}
          onClose={hide}
        />
      )}
    </DashboardShell>
  );
}