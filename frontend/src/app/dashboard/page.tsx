"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Mic, PhoneCall, Plus } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, AgentOut, CallOut, LeadOut, UsageOut } from "@/lib/api";
import { AnalyticsPanel } from "@/components/credits/AnalyticsPanel";
import { CreditsPanel } from "@/components/credits/CreditsPanel";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

export default function DashboardOverview() {
  const { org, loading, error } = useOrganization();
  const [calls, setCalls] = useState<CallOut[]>([]);
  const [leads, setLeads] = useState<LeadOut[]>([]);
  const [agent, setAgent] = useState<AgentOut | null>(null);
  const [usage, setUsage] = useState<UsageOut | null>(null);
  const [dataLoading, setDataLoading] = useState(true);

  useEffect(() => {
    if (!org) return;
    Promise.all([
        api.listCalls(org.id, { limit: 5 }),
        api.listLeads(org.id, { limit: 5 }),
        api.listAgents(org.id),
        api.getUsage(org.id),
      ])
      .then(([callsData, leadsData, agents, usageData]) => {
      setUsage(usageData);
      setCalls(callsData);
      setLeads(leadsData);
      setAgent(agents[0] ?? null);
      })
      .catch(() => {
        // Non-fatal for the overview — empty state below covers this.
      })
      .finally(() => setDataLoading(false));
  }, [org]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Overview</h1>
      <p className="mt-1 text-[14.5px] text-[var(--color-ink-soft)]">
          {org?.name} · {dataLoading ? "loading…" : `${calls.length} recent calls, ${leads.length} recent leads`}
      </p>

    {org && <CreditsPanel orgId={org.id} isOwner={org.role === "owner"} />}
{org && <AnalyticsPanel orgId={org.id} />}

{!dataLoading && agent && agent.status !== "active" && (
     <div className="mt-6 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-6">
     <div className="text-[12.5px] font-medium uppercase tracking-wide text-[var(--color-ink-soft)]">
          Not live yet
    </div>
     <h2 className="mt-1 font-[family-name:var(--font-display)] text-[20px] font-bold tracking-tight">
          Your receptionist is built and ready
     </h2>
     <p className="mt-1.5 max-w-lg text-[14px] text-[var(--color-ink-soft)]">
         It isn&apos;t answering real calls yet. Talk to it as much as you like, then put it on your business number
         when you&apos;re ready.
     </p>
     <div className="mt-4 flex flex-wrap gap-3">
     <Link
         href="/dashboard/go-live"
         className="rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[13.5px] font-medium text-[var(--color-paper)]"
       >
          Put it on my business number
       </Link>
       <Link
        href="/dashboard/test"
        className="flex items-center gap-1.5 rounded-full border border-[var(--color-line)] px-5 py-2.5 text-[13.5px] font-medium"
       >
        <Mic className="h-3.5 w-3.5" /> Talk to it again
      </Link>
    </div>
  </div>
)}

{!dataLoading && agent && agent.status === "active" && (
  <div className="mt-6 flex items-center gap-2 rounded-xl border border-[var(--color-ok)]/30 bg-[var(--color-ok)]/10 px-4 py-3 text-[14px] font-medium">
    <span className="h-2 w-2 rounded-full bg-[var(--color-ok)]" />
    Your receptionist is live and answering calls
  </div>
)}

{!dataLoading && agent && usage && (
        <p className="mt-4 text-[13.5px] text-[var(--color-ink-soft)]">
          {usage.period_start ? "This billing period" : "So far"}: {usage.calls_count} {usage.calls_count === 1 ? "call" : "calls"} handled ·{" "}
          {Math.round(usage.billable_seconds / 60)} min
        </p>
      )}

      {!dataLoading && !agent && calls.length === 0 && leads.length === 0 && (
        <div className="mt-10 flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
          <PhoneCall className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} />
          <h2 className="mt-4 text-[16px] font-semibold">Set up your receptionist to get started.</h2>
          <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">
            Once your receptionist is active and takes its first call, it&apos;ll show up here.
          </p>
          <Link
            href="/onboarding"
            className="mt-5 flex items-center gap-1.5 rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[13.5px] font-medium text-[var(--color-paper)]"
          >
            <Plus className="h-3.5 w-3.5" />
            Set up your receptionist
          </Link>
        </div>
      )}

      {!dataLoading && (calls.length > 0 || leads.length > 0) && (
        <div className="mt-8 grid gap-6 md:grid-cols-2">
          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-[15px] font-semibold">Recent calls</h2>
              <Link href="/dashboard/calls" className="text-[13px] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                View all →
              </Link>
            </div>
            <div className="space-y-2">
              {calls.map((c) => (
                <div key={c.id} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3">
                  <div className="flex items-center justify-between text-[13.5px]">
                    <span className="font-medium">
                         {c.direction === "test" ? "Test call" : c.caller_number || "Unknown caller"}
                    </span>
                    <span className="text-[var(--color-ink-soft)]">{c.status || "—"}</span>
                  </div>
                  {c.summary && <p className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{c.summary}</p>}
                </div>
              ))}
            </div>
          </div>

          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-[15px] font-semibold">Recent leads</h2>
              <Link href="/dashboard/leads" className="text-[13px] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                View all →
              </Link>
            </div>
            <div className="space-y-2">
              {leads.map((l) => (
                <div key={l.id} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3">
                  <div className="flex items-center justify-between text-[13.5px]">
                    <span className="font-medium">{l.name || "Unnamed lead"}</span>
                    <span className="text-[var(--color-ink-soft)]">{l.status}</span>
                  </div>
                  {l.reason && <p className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{l.reason}</p>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </DashboardShell>
  );
}