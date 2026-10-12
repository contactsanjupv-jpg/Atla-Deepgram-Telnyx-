"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Plus } from "lucide-react";
import { api, CreditEvent, CreditsOut, DailyPoint, describeError } from "@/lib/api";
import { browserTimeZone, creditBanner, creditView, dayLabel, EVENT_LABEL, formatDollars, minutesLabel } from "@/lib/credits";
import { UsageChart } from "./UsageChart";
import { TopupDialog } from "./TopupDialog";

const RANGES = [
  { days: 1, label: "Today" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
] as const;

/** Credits and usage: real balance, real daily usage, a working "Buy more credits". Nothing here is estimated. */
export function CreditsPanel({ orgId, isOwner }: { orgId: string; isOwner: boolean }) {
  const [credits, setCredits] = useState<CreditsOut | null>(null);
  const [events, setEvents] = useState<CreditEvent[]>([]);
  const [points, setPoints] = useState<DailyPoint[]>([]);
  const [days, setDays] = useState<number>(7);
  const [error, setError] = useState<string | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [dialog, setDialog] = useState(false);
  const [waiting, setWaiting] = useState<null | "polling" | "slow">(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchCredits = useCallback(
    () => Promise.all([api.getCredits(orgId), api.getCreditEvents(orgId).catch(() => [] as CreditEvent[])]),
    [orgId],
  );
  const loadCredits = useCallback(async () => {
    const [c, e] = await fetchCredits();
    setCredits(c);
    setEvents(e);
    return { c, e };
  }, [fetchCredits]);

  useEffect(() => {
    let live = true;
    fetchCredits()
      .then(([c, e]) => {
        if (!live) return;
        setCredits(c);
        setEvents(e);
        setError(null);
      })
      .catch((err) => live && setError(describeError(err, "We couldn't load your credits.")))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [fetchCredits]);

  useEffect(() => {
    let live = true;
    api
      .getDailyUsage(orgId, days, browserTimeZone())
      .then((p) => {
        if (!live) return;
        setPoints(p);
        setChartError(null);
      })
      .catch((err) => live && setChartError(describeError(err, "We couldn't load usage.")));
    return () => {
      live = false;
    };
  }, [orgId, days]);

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  // After Paddle says the payment went through, the server adds credits when ITS confirmation arrives. Poll for it.
  function waitForCredits() {
    setDialog(false);
    setWaiting("polling");
    const before = events.filter((e) => e.kind === "topup").length;
    let tries = 0;
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      tries += 1;
      try {
        const { e } = await loadCredits();
        if (e.filter((x) => x.kind === "topup").length > before) {
          clearInterval(pollRef.current!);
          setWaiting(null);
          return;
        }
      } catch {
        /* keep trying */
      }
      if (tries >= 20) {
        clearInterval(pollRef.current!);
        setWaiting("slow");
      }
    }, 3000);
  }

  if (loading) {
    return <div className="mt-6 h-40 animate-pulse rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" aria-label="Loading credits" />;
  }
  if (error || !credits) {
    return (
      <section className="mt-6 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5">
        <h2 className="text-[15px] font-semibold">Credits and usage</h2>
        <p className="mt-1.5 text-[13.5px] text-[#7a2416]">{error ?? "We couldn't load your credits."}</p>
        <button
          onClick={() => {
            setLoading(true);
            loadCredits()
              .then(() => setError(null))
              .catch((err) => setError(describeError(err, "We couldn't load your credits.")))
              .finally(() => setLoading(false));
          }}
          className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium"
        >
          Try again
        </button>
      </section>
    );
  }

  const view = creditView(credits);
  const banner = creditBanner(credits);
  const canBuy = isOwner && credits.entitled;

  return (
    <section className="mt-6 min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5 sm:p-6" aria-labelledby="credits-h">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2 id="credits-h" className="text-[15px] font-semibold">
          Credits and usage
        </h2>
        {canBuy && (
          <button
            onClick={() => setDialog(true)}
            className="flex items-center gap-1.5 rounded-full bg-[var(--color-ink)] px-4 py-2 text-[13px] font-medium text-[var(--color-paper)]"
          >
            <Plus className="h-3.5 w-3.5" /> Buy more credits
          </button>
        )}
      </div>

      {waiting === "polling" && (
        <p role="status" className="mt-3 rounded-lg bg-[var(--color-line)]/50 px-3.5 py-2.5 text-[13.5px]">
          Payment received. Adding your credits…
        </p>
      )}
      {waiting === "slow" && (
        <p role="status" className="mt-3 rounded-lg bg-[var(--color-line)]/50 px-3.5 py-2.5 text-[13.5px]">
          Your payment is confirmed on Paddle&apos;s side and your credits are on the way. This can take a few minutes — refresh this page shortly. You won&apos;t be charged twice.
        </p>
      )}

      {banner && (
        <div
          role="alert"
          className={`mt-4 flex gap-2.5 rounded-xl border px-4 py-3 text-[13.5px] ${banner.tone === "empty" ? "border-[#7a2416]/30 bg-[#7a2416]/5" : "border-[var(--color-line)] bg-[var(--color-line)]/40"}`}
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={1.75} />
          <div className="min-w-0">
            <div className="font-medium">{banner.title}</div>
            <div className="text-[var(--color-ink-soft)]">{banner.body}</div>
            {!isOwner && <div className="mt-1 text-[var(--color-ink-soft)]">Ask the account owner to add credits.</div>}
          </div>
        </div>
      )}

      {view.kind === "unmetered" ? (
        <div className="mt-4 text-[13.5px] text-[var(--color-ink-soft)]">
          {credits.free_test_calls_remaining > 0
            ? `You have ${credits.free_test_calls_remaining} free test call${credits.free_test_calls_remaining === 1 ? "" : "s"} included. Choose a plan to start taking real calls.`
            : "Your free test call has been used. Choose a plan to keep testing and take real calls."}
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <div>
              <span className="text-[28px] font-bold tracking-tight">{view.remaining}</span>
              <span className="ml-1.5 text-[13.5px] text-[var(--color-ink-soft)]">available</span>
            </div>
            <div className="text-[13px] text-[var(--color-ink-soft)]">
              {view.used} of {view.granted} included used
              {view.purchased !== "0 min" && <> · {view.purchased} purchased</>}
              {view.owes && <> · {view.owes} owed</>}
            </div>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--color-line)]" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={view.usedPercent} aria-label="Included minutes used">
            <div className="h-full rounded-full bg-[var(--color-ink)]" style={{ width: `${view.usedPercent}%` }} />
          </div>
          {credits.period_end && (
            <p className="mt-2 text-[12.5px] text-[var(--color-ink-soft)]">Included minutes renew {dayLabel(credits.period_end.slice(0, 10))}.</p>
          )}
          <p className="mt-1 text-[12.5px] text-[var(--color-ink-soft)]">{credits.when_credits_run_out}</p>
        </>
      )}

      <div className="mt-6 flex items-center justify-between gap-3">
        <h3 className="text-[13.5px] font-semibold">Usage</h3>
        <div role="tablist" aria-label="Usage range" className="flex rounded-full border border-[var(--color-line)] p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.days}
              role="tab"
              aria-selected={days === r.days}
              onClick={() => setDays(r.days)}
              className={`rounded-full px-3 py-1 text-[12.5px] font-medium ${days === r.days ? "bg-[var(--color-ink)] text-[var(--color-paper)]" : "text-[var(--color-ink-soft)]"}`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {chartError ? (
        <p className="mt-3 text-[13px] text-[#7a2416]">{chartError}</p>
      ) : (
        <div className="mt-3 grid gap-5 sm:grid-cols-2">
          <UsageChart title="Calls" points={points} pick={(p) => p.calls} format={(v) => String(v)} emptyText="No calls in this period yet." />
          <UsageChart title="Minutes used" points={points} pick={(p) => p.credit_seconds} format={(v) => minutesLabel(v)} emptyText="No minutes used in this period yet." />
        </div>
      )}

      <h3 className="mt-6 text-[13.5px] font-semibold">Credit activity</h3>
      {events.length === 0 ? (
        <p className="mt-2 text-[13px] text-[var(--color-ink-soft)]">Nothing yet. Monthly minutes and purchases will show here.</p>
      ) : (
        <ul className="mt-2 divide-y divide-[var(--color-line)]">
          {events.map((e) => (
            <li key={e.id} className="flex items-baseline justify-between gap-3 py-2 text-[13px]">
              <span className="min-w-0 truncate">
                {EVENT_LABEL[e.kind] ?? e.kind}
                <span className="ml-2 text-[var(--color-ink-soft)]">{new Date(e.occurred_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>
              </span>
              <span className={`shrink-0 font-medium ${e.seconds < 0 ? "text-[#7a2416]" : ""}`}>
                {e.seconds < 0 ? "−" : "+"}
                {minutesLabel(e.seconds)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {canBuy && (
        <p className="mt-3 text-[12px] text-[var(--color-ink-soft)]">
          Minimum top-up {formatDollars(credits.topup_min_minor)}. Credits you buy never expire.
        </p>
      )}

      {dialog && <TopupDialog orgId={orgId} minMinor={credits.topup_min_minor} maxMinor={credits.topup_max_minor} onClose={() => setDialog(false)} onPaid={waitForCredits} />}
    </section>
  );
}
