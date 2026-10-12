"use client";

import { useEffect, useState } from "react";
import { api, AnalyticsOut, describeError } from "@/lib/api";
import { browserTimeZone, minutesLabel } from "@/lib/credits";
import { useFeatures } from "@/lib/useFeatures";
import { UsageChart } from "./UsageChart";

/** Growth analytics: real 30-day trends and leads still waiting for a follow-up. Starter sees what it adds, with no fake numbers. */
export function AnalyticsPanel({ orgId }: { orgId: string }) {
  const { features } = useFeatures(orgId);
  const allowed = features?.has("analytics") ?? false;
  const [data, setData] = useState<AnalyticsOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) return;
    let live = true;
    api
      .getAnalytics(orgId, 30, browserTimeZone())
      .then((d) => live && setData(d))
      .catch((err) => live && setError(describeError(err, "We couldn't load analytics.")));
    return () => {
      live = false;
    };
  }, [orgId, allowed]);

  if (features === null) return null;
  if (!allowed) {
    return (
      <section className="mt-6 rounded-2xl border border-dashed border-[var(--color-line)] p-5">
        <h2 className="text-[15px] font-semibold">Analytics</h2>
        <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">30-day call, minute and lead trends, and leads waiting for a follow-up, are part of the Growth plan.</p>
      </section>
    );
  }
  return (
    <section className="mt-6 min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5 sm:p-6">
      <h2 className="text-[15px] font-semibold">Analytics · last 30 days</h2>
      {error && <p className="mt-2 text-[13.5px] text-[#7a2416]">{error}</p>}
      {!error && !data && <div className="mt-3 h-24 animate-pulse rounded-lg bg-[var(--color-line)]/50" />}
      {data && (
        <>
          <div className="mt-3 grid grid-cols-3 gap-3 text-[13px]">
            <div><div className="text-[20px] font-bold">{data.total_calls}</div>calls</div>
            <div><div className="text-[20px] font-bold">{minutesLabel(data.total_credit_seconds)}</div>used</div>
            <div><div className="text-[20px] font-bold">{data.total_leads}</div>leads</div>
          </div>
          <div className="mt-5 grid gap-5 sm:grid-cols-2">
            <UsageChart title="Calls per day" points={data.series.map((d) => ({ date: d.date, calls: d.calls, credit_seconds: d.credit_seconds }))} pick={(p) => p.calls} format={String} emptyText="No calls in the last 30 days." />
            <UsageChart title="Leads per day" points={data.series.map((d) => ({ date: d.date, calls: d.leads, credit_seconds: 0 }))} pick={(p) => p.calls} format={String} emptyText="No leads in the last 30 days." />
          </div>
          <p className="mt-4 text-[13.5px]">
            {data.leads_awaiting_follow_up === 0
              ? "No leads are waiting for a follow-up."
              : `${data.leads_awaiting_follow_up} lead${data.leads_awaiting_follow_up === 1 ? " is" : "s are"} waiting for a follow-up${data.oldest_unattended_lead_at ? `, the oldest since ${new Date(data.oldest_unattended_lead_at).toLocaleDateString()}` : ""}.`}
          </p>
        </>
      )}
    </section>
  );
}
