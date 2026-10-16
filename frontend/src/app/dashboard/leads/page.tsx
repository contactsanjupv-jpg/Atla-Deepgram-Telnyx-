"use client";

import { useEffect, useMemo, useState } from "react";
import { SearchX, Users } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, describeError } from "@/lib/api";
import { browserTimeZone } from "@/lib/credits";
import { useLoad } from "@/lib/useLoad";
import { DEFAULT_LEAD_FILTERS, PAGE_SIZE, clampOffset, leadsNarrowed, pageInfo, toLeadQuery, type LeadFilterState } from "@/lib/leads";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";
import { LeadFilters } from "@/components/leads/LeadFilters";
import { LeadCard } from "@/components/leads/LeadCard";

const SEARCH_DEBOUNCE_MS = 350;

export default function LeadsPage() {
  const { org, loading, error } = useOrganization();
  const orgId = org?.id;

  const [filters, setFilters] = useState<LeadFilterState>(DEFAULT_LEAD_FILTERS);
  const [searchText, setSearchText] = useState("");
  const [offset, setOffset] = useState(0);
  const [saving, setSaving] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null);
  // Read after mount so the server and browser render the same first frame.
  const [tz, setTz] = useState("UTC");
  useEffect(() => {
    const t = setTimeout(() => setTz(browserTimeZone()), 0);
    return () => clearTimeout(t);
  }, []);

  // Typing applies after a short pause and returns to the first page.
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((f) => (f.q === searchText ? f : { ...f, q: searchText }));
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchText]);

  const query = useMemo(() => toLeadQuery(filters, offset), [filters, offset]);
  const page = useLoad(orgId ? `${orgId}:${JSON.stringify(query)}` : null, () => api.browseLeads(orgId!, query), "We couldn't load your leads.");

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  const data = page.data ?? page.last;
  const stale = page.loading && page.data === null;
  const narrowed = leadsNarrowed(filters);
  const info = data ? pageInfo(data.total, data.offset, data.limit, data.items.length) : null;
  const everything = data ? Object.values(data.counts).reduce((a, b) => a + b, 0) : 0;

  function change(patch: Partial<LeadFilterState>) {
    setFilters((f) => ({ ...f, ...patch }));
    setOffset(0);
  }
  function clear() {
    setFilters(DEFAULT_LEAD_FILTERS);
    setSearchText("");
    setOffset(0);
  }

  async function setStatus(leadId: string, status: string) {
    if (!orgId) return;
    setSaving(leadId);
    setRowError(null);
    try {
      await api.updateLeadStatus(orgId, leadId, status);
      page.retry(); // reload so the list, the tab counts and the filter all agree with the server
    } catch (err) {
      setRowError({ id: leadId, message: describeError(err, "We couldn't update this lead. Its status is unchanged.") });
    } finally {
      setSaving(null);
    }
  }

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Leads</h1>
      <p className="mt-1 text-[14.5px] text-[var(--color-ink-soft)]" aria-live="polite">
        {data ? (data.total === 0 ? (narrowed ? "No leads match." : "No leads yet.") : `${data.total.toLocaleString()} ${narrowed ? "matching " : ""}${data.total === 1 ? "lead" : "leads"}`) : page.error ? "" : "Loading…"}
      </p>

      <div className="mt-5">
        <LeadFilters value={filters} searchText={searchText} counts={data?.counts ?? null} total={data?.total ?? null} onSearchText={setSearchText} onChange={change} onClear={clear} />
      </div>

      <div className="mt-5" aria-busy={page.loading}>
        {page.error && !data && (
          <div role="alert" className="rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5">
            <p className="text-[13.5px] text-[#7a2416]">{page.error}</p>
            <button onClick={page.retry} className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">
              Try again
            </button>
          </div>
        )}

        {!data && !page.error && (
          <div className="space-y-2" aria-label="Loading leads">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="h-[104px] animate-pulse rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
            ))}
          </div>
        )}

        {data && data.total === 0 && everything === 0 && !narrowed && (
          <div className="flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
            <Users className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} aria-hidden />
            <h2 className="mt-4 text-[16px] font-semibold">No leads yet</h2>
            <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">Leads your receptionist captures on calls will show up here.</p>
          </div>
        )}

        {data && data.total === 0 && narrowed && (
          <div className="flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-14 text-center">
            <SearchX className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} aria-hidden />
            <h2 className="mt-4 text-[16px] font-semibold">No leads match</h2>
            <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">Try a different search or another status.</p>
            <button onClick={clear} className="mt-4 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">
              Clear filters
            </button>
          </div>
        )}

        {data && data.total > 0 && data.items.length === 0 && (
          <div className="rounded-xl border border-[var(--color-line)] p-5 text-[13.5px]">
            That page is past the last lead.{" "}
            <button onClick={() => setOffset(clampOffset(offset, data.total, PAGE_SIZE))} className="font-medium underline">
              Go to the last page
            </button>
          </div>
        )}

        {data && data.items.length > 0 && (
          <div className={stale ? "opacity-60 transition-opacity" : ""}>
            <ul className="space-y-2">
              {data.items.map((lead) => (
                <LeadCard key={lead.id} lead={lead} tz={tz} saving={saving === lead.id} error={rowError?.id === lead.id ? rowError.message : null} onStatus={(s) => setStatus(lead.id, s)} />
              ))}
            </ul>
            {page.error && <p className="mt-3 text-[13px] text-[#7a2416]">{page.error}</p>}
            {info && (
              <nav aria-label="Pages" className="mt-4 flex items-center justify-between gap-3">
                <span className="text-[13px] text-[var(--color-ink-soft)]">
                  {info.from.toLocaleString()}–{info.to.toLocaleString()} of {data.total.toLocaleString()}
                </span>
                <div className="flex gap-2">
                  <button disabled={!info.hasPrev || page.loading} onClick={() => setOffset(Math.max(0, data.offset - PAGE_SIZE))} className="rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium disabled:opacity-40">
                    Previous
                  </button>
                  <button disabled={!info.hasNext || page.loading} onClick={() => setOffset(data.offset + PAGE_SIZE)} className="rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium disabled:opacity-40">
                    Next
                  </button>
                </div>
              </nav>
            )}
          </div>
        )}
      </div>
    </DashboardShell>
  );
}