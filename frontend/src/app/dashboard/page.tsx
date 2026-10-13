"use client";

import { useState } from "react";
import { useOrganization } from "@/lib/useOrganization";
import { api } from "@/lib/api";
import { browserTimeZone, dayLabel, minutesLabel } from "@/lib/credits";
import { RANGES, attentionItems, hourLabel, type DashboardRange } from "@/lib/dashboard";
import { useLoad } from "@/lib/useLoad";
import { useShellStatus } from "@/lib/useShellStatus";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";
import { StatusCards } from "@/components/dashboard/StatusCards";
import { AttentionList } from "@/components/dashboard/AttentionList";
import { MetricCards } from "@/components/dashboard/MetricCards";
import { RangeTabs } from "@/components/dashboard/RangeTabs";
import { RecentCalls, WaitingLeads } from "@/components/dashboard/ActivityLists";
import { UsageChart } from "@/components/credits/UsageChart";
import { CreditsPanel } from "@/components/credits/CreditsPanel";
import { AnalyticsPanel } from "@/components/credits/AnalyticsPanel";

export default function DashboardOverview() {
  const { org, loading, error } = useOrganization();
  const orgId = org?.id;
  const [range, setRange] = useState<DashboardRange>("7d");
  const shell = useShellStatus(orgId);

  const summary = useLoad(orgId ? `${orgId}:${range}` : null, () => api.getDashboardSummary(orgId!, range, browserTimeZone()), "We couldn't load your activity.");
  const calls = useLoad(orgId ?? null, () => api.listCalls(orgId!, { limit: 5 }), "We couldn't load your recent calls.");
  const credits = useLoad(orgId ?? null, () => api.getCredits(orgId!), "We couldn't load your credits.");

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  const compareTo = RANGES.find((r) => r.id === range)!.compareTo;
  const awaiting = summary.last; // the follow-up queue doesn't depend on the range
  const items = attentionItems({
    status: shell.status,
    sub: shell.sub,
    agent: shell.agent,
    phone: shell.phone,
    credits: credits.data,
    awaitingLeads: awaiting?.leads_awaiting_follow_up ?? 0,
    oldestAwaitingAt: awaiting?.oldest_unattended_lead_at ?? null,
  });
  const attentionReady = !shell.loading && (awaiting !== null || summary.error !== null);

  const data = summary.data;
  const labelFor = data?.granularity === "hour" ? hourLabel : dayLabel;
  const rangeText = range === "today" ? "today" : range === "7d" ? "in the last 7 days" : "in the last 30 days";

  return (
    <DashboardShell org={org}>
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
        <div className="min-w-0">
          <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Overview</h1>
          <p className="mt-1 truncate text-[14.5px] text-[var(--color-ink-soft)]">{org?.name}</p>
        </div>
        <RangeTabs value={range} onChange={setRange} />
      </div>

      <div className="mt-6 space-y-3">
        <StatusCards
          loading={shell.loading}
          status={shell.status}
          agent={shell.agent}
          agentKnown={shell.agentKnown}
          phone={shell.phone}
          phoneKnown={shell.phoneKnown}
          sub={shell.sub}
        />
        <AttentionList items={items} ready={attentionReady} />
      </div>

      <h2 className="mt-8 text-[15px] font-semibold">Activity</h2>
      <div className="mt-3 space-y-3">
        <MetricCards summary={data} loading={summary.loading} error={summary.error} compareTo={compareTo} onRetry={summary.retry} />

        <section className="min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4 sm:p-5" aria-label="Call activity">
          {data ? (
            <div className="grid gap-5 sm:grid-cols-2">
              <UsageChart
                title={data.granularity === "hour" ? "Calls by hour" : "Calls by day"}
                points={data.series.map((b) => ({ date: b.start, calls: b.calls, credit_seconds: b.call_seconds }))}
                pick={(p) => p.calls}
                format={String}
                emptyText={`No calls ${rangeText} yet.`}
                labelFor={labelFor}
              />
              <UsageChart
                title="Time on calls"
                points={data.series.map((b) => ({ date: b.start, calls: b.calls, credit_seconds: b.call_seconds }))}
                pick={(p) => p.credit_seconds}
                format={minutesLabel}
                emptyText={`No call time ${rangeText} yet.`}
                labelFor={labelFor}
              />
            </div>
          ) : (
            <div className="grid gap-5 sm:grid-cols-2" aria-busy={summary.loading}>
              <div className="h-[110px] animate-pulse rounded-lg bg-[var(--color-line)]/60" />
              <div className="hidden h-[110px] animate-pulse rounded-lg bg-[var(--color-line)]/60 sm:block" />
            </div>
          )}
        </section>
      </div>

      <div className="mt-6 grid gap-3 md:grid-cols-2">
        <RecentCalls calls={calls.data} loading={calls.loading} error={calls.error} onRetry={calls.retry} />
        <WaitingLeads
          leads={awaiting?.awaiting_leads ?? null}
          count={awaiting?.leads_awaiting_follow_up ?? 0}
          loading={summary.last === null && summary.loading}
          error={summary.last === null ? summary.error : null}
          onRetry={summary.retry}
        />
      </div>

      <div id="credits" className="scroll-mt-20">
        {org && <CreditsPanel orgId={org.id} isOwner={org.role === "owner"} showUsage={false} />}
      </div>
      {org && <AnalyticsPanel orgId={org.id} />}
    </DashboardShell>
  );
}