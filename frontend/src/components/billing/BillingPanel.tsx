"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Check } from "lucide-react";
import { api, describeError, PlanOut, SubscriptionOut, UsageOut } from "@/lib/api";
import { formatDate, formatPrice, formatUsage } from "@/lib/billing";
import { describeSubscription, type Tone } from "@/lib/billingState";
import { cancelNotice, cancelPhase, createCancelController, type CancelOutcome } from "@/lib/cancelFlow";
import { usePlans } from "@/lib/usePlans";

const TONE_STYLES: Record<Tone, string> = {
  ok: "bg-[var(--color-ok)]/12 text-[var(--color-ok)]",
  warn: "bg-[#f3e3b8] text-[#6b4d0a]",
  bad: "bg-[var(--color-ring-soft)] text-[#7a2416]",
  neutral: "bg-[var(--color-line)] text-[var(--color-ink-soft)]",
};

const BTN =
  "rounded-full px-5 py-2.5 text-[13.5px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-ink)] disabled:opacity-50";
const BTN_PRIMARY = `${BTN} bg-[var(--color-ink)] text-[var(--color-paper)] hover:opacity-90`;
const BTN_QUIET = `${BTN} border border-[var(--color-line)] hover:bg-[var(--color-line)]`;

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-6 py-3 text-[14px]">
      <dt className="text-[var(--color-ink-soft)]">{label}</dt>
      <dd className="text-right font-medium">{children}</dd>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="animate-pulse space-y-4" aria-busy="true" aria-label="Loading billing details">
      <div className="h-5 w-40 rounded bg-[var(--color-line)]" />
      <div className="h-4 w-64 rounded bg-[var(--color-line)]" />
      <div className="h-24 rounded-xl bg-[var(--color-line)]" />
    </div>
  );
}

/** Every feature across all plans, with the lowest plan that includes it — so "Available on Growth" is real, not invented. */
function featureRows(plans: PlanOut[] | null, sub: SubscriptionOut) {
  const ids = new Set(sub.features.map((f) => f.id));
  const rows = new Map<string, { id: string; label: string; included: boolean; availableOn: string | null }>();
  for (const f of sub.features) rows.set(f.id, { id: f.id, label: f.label, included: true, availableOn: null });
  for (const plan of plans ?? []) {
    for (const f of plan.features) {
      if (!rows.has(f.id)) rows.set(f.id, { id: f.id, label: f.label, included: false, availableOn: plan.name });
    }
  }
  return [...rows.values()].map((r) => ({ ...r, included: ids.has(r.id) }));
}

export function BillingPanel({ orgId, role }: { orgId: string; role: string }) {
  const { plans } = usePlans();
  const [sub, setSub] = useState<SubscriptionOut | null | undefined>(undefined); // undefined = loading
  const [usage, setUsage] = useState<UsageOut | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [busy, setBusy] = useState<"manage" | "cancel" | "refresh" | null>(null);
  const [cancelOutcome, setCancelOutcome] = useState<CancelOutcome | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const noticeRef = useRef<HTMLDivElement | null>(null);
  const isOwner = role === "owner";

  const load = useCallback(() => {
    return Promise.all([api.getSubscription(orgId), api.getUsage(orgId)])
      .then(([s, u]) => {
        setSub(s);
        setUsage(u);
        setLoadError(false);
        setNowMs(Date.now());
      })
      .catch(() => setLoadError(true));
  }, [orgId]);

  useEffect(() => {
    let live = true;
    Promise.all([api.getSubscription(orgId), api.getUsage(orgId)])
      .then(([s, u]) => {
        if (!live) return;
        setSub(s);
        setUsage(u);
      })
      .catch(() => {
        if (live) setLoadError(true);
      });
    return () => {
      live = false;
    };
  }, [orgId]);

  async function openPaymentUpdate() {
    setMessage(null);
    setBusy("manage");
    try {
      const { update_payment_method_url } = await api.getBillingManage(orgId);
      if (update_payment_method_url) window.open(update_payment_method_url, "_blank", "noopener");
      else setMessage({ text: "Updating your payment method isn't available right now. Please try again shortly.", bad: true });
    } catch (err) {
      setMessage({ text: describeError(err, "We couldn't open your billing details. Please try again."), bad: true });
    } finally {
      setBusy(null);
    }
  }

  // One controller per org: it ignores a second submit while one is in flight and after one was accepted.
  const cancelController = useMemo(
    () =>
      createCancelController({
        cancel: () => api.cancelSubscription(orgId),
        refresh: async () => {
          const s = await api.getSubscription(orgId);
          setSub(s);
          return s;
        },
      }),
    [orgId],
  );

  const accepted = cancelOutcome?.kind === "accepted";
  const phase = cancelPhase(sub ?? null, accepted, nowMs);
  const awaiting = phase === "pending" || phase === "stale";

  // While a request is accepted but unconfirmed (including right after a reload), keep checking the server for a while.
  useEffect(() => {
    if (!awaiting) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    (async () => {
      for (const wait of [5000, 10000, 20000, 30000, 60000]) {
        await new Promise<void>((resolve) => {
          timer = setTimeout(resolve, wait);
        });
        if (stop) return;
        try {
          const s = await api.getSubscription(orgId);
          if (stop) return;
          setSub(s);
          setNowMs(Date.now());
          if (s === null || s.cancel_effective_at) return;
        } catch {
          /* stay in the pending state and try again */
        }
      }
    })();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [awaiting, orgId]);

  async function cancelPlan() {
    setMessage(null);
    setBusy("cancel");
    try {
      const result = await cancelController.submit();
      if (result === null) return;
      setCancelOutcome(result.outcome);
      setNowMs(Date.now());
      if (result.outcome.kind !== "error") setConfirmingCancel(false); // on error the dialog stays open to retry
    } finally {
      setBusy(null);
    }
  }

  async function checkAgain() {
    setBusy("refresh");
    try {
      await load();
    } finally {
      setBusy(null);
    }
  }

  function retryRequest() {
    cancelController.allowRetry();
    setConfirmingCancel(true);
  }

  const notice = cancelNotice(cancelOutcome, sub ?? null, phase, (e) => describeError(e, "We couldn't cancel right now. Please try again."));
  useEffect(() => {
    if (notice) noticeRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [notice?.title]); // eslint-disable-line react-hooks/exhaustive-deps

  const card = "rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-6";

  if (loadError && sub === undefined) {
    return (
      <section className={card}>
        <h2 className="text-[17px] font-semibold">Billing</h2>
        <p className="mt-2 text-[14px] text-[var(--color-ink-soft)]">We couldn&apos;t load your billing details.</p>
        <button onClick={load} className={`${BTN_QUIET} mt-4`}>
          Try again
        </button>
      </section>
    );
  }

  if (sub === undefined) {
    return (
      <section className={card}>
        <Skeleton />
      </section>
    );
  }

  const info = describeSubscription(sub, awaiting);
  const plan = sub ? plans?.find((p) => p.id === sub.plan_id) : undefined;
  const price = formatPrice(plan?.price);
  const periodStart = formatDate(sub?.current_period_start);
  const periodEnd = formatDate(sub?.current_period_end);
  const ending = sub !== null && sub.entitled && Boolean(sub.cancel_effective_at);
  const canManagePayment = sub !== null && ["active", "trialing", "past_due"].includes(sub.status);
  const canCancel = sub !== null && sub.entitled && !sub.cancel_effective_at && phase === "none";
  const needsPlan = sub === null || sub.status === "canceled";
  const showFeatures = sub !== null && sub.entitled;
  const showBilling = sub !== null && sub.status !== "canceled";

  return (
    <section className={card}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[17px] font-semibold">Billing</h2>
          <p className="mt-1 text-[15px] font-medium">{info.headline}</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-[12.5px] font-medium ${TONE_STYLES[info.tone]}`}>{info.label}</span>
      </div>
      <p className="mt-1 text-[14px] leading-relaxed text-[var(--color-ink-soft)]">{info.detail}</p>

      {sub !== null && sub.plan_name && (
        <dl className="mt-5 divide-y divide-[var(--color-line)] border-t border-[var(--color-line)]">
          <Row label="Plan">{sub.plan_name}</Row>
          {price && (
            <Row label="Price">
              {price.amount}
              <span className="font-normal text-[var(--color-ink-soft)]">{price.cadence}</span>
            </Row>
          )}
          {showBilling && periodStart && periodEnd && (
            <Row label="Current billing period">
              {periodStart} – {periodEnd}
            </Row>
          )}
          {sub.entitled && !ending && !awaiting && periodEnd && <Row label="Next billing date">{periodEnd}</Row>}
          {awaiting && periodEnd && <Row label="Current billing period ends">{periodEnd}</Row>}
          {ending && <Row label="Ends on">{formatDate(sub.cancel_effective_at)}</Row>}
        </dl>
      )}

      {showFeatures && sub && (
        <div className="mt-6">
          <h3 className="text-[14px] font-semibold">Included in your plan</h3>
          <ul className="mt-3 space-y-2.5">
            {featureRows(plans, sub).map((f) => (
              <li key={f.id} className={`flex items-start gap-2 text-[14px] ${f.included ? "" : "text-[var(--color-ink-soft)]"}`}>
                {f.included ? (
                  <Check className="mt-0.5 h-4 w-4 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
                ) : (
                  <span className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden />
                )}
                <span>
                  {f.label}
                  {!f.included && f.availableOn && (
                    <span className="ml-2 rounded-full bg-[var(--color-line)] px-2 py-0.5 text-[12px]">Available on {f.availableOn}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-6 border-t border-[var(--color-line)] pt-5">
        <h3 className="text-[14px] font-semibold">Usage</h3>
        {usage && usage.calls_count > 0 ? (
          <dl className="mt-2 divide-y divide-[var(--color-line)]">
            <Row label="Calls">{usage.calls_count}</Row>
            <Row label="Time on calls">{formatUsage(usage.billable_seconds)}</Row>
          </dl>
        ) : (
          <p className="mt-2 text-[14px] text-[var(--color-ink-soft)]">No calls yet. Calls your receptionist answers will show up here.</p>
        )}
        {usage && (
          <p className="mt-2 text-[12.5px] text-[var(--color-ink-soft)]">
            {usage.period_start && usage.period_end
              ? `This billing period (${formatDate(usage.period_start)} – ${formatDate(usage.period_end)}). Test calls aren't counted.`
              : "Since you started. Test calls aren't counted."}
          </p>
        )}
      </div>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        {needsPlan && (
          <Link href="/dashboard/go-live" className={BTN_PRIMARY}>
            Choose a plan
          </Link>
        )}
        {isOwner && canManagePayment && (
          <button onClick={openPaymentUpdate} disabled={busy !== null} className={sub?.status === "past_due" ? BTN_PRIMARY : BTN_QUIET}>
            {busy === "manage" ? "Opening…" : "Update payment method"}
          </button>
        )}
        {isOwner && canCancel && !confirmingCancel && (
          <button onClick={() => setConfirmingCancel(true)} className={BTN_QUIET}>
            Cancel plan
          </button>
        )}
        {!isOwner && sub !== null && <p className="text-[13.5px] text-[var(--color-ink-soft)]">Only the account owner can change billing.</p>}
      </div>

      {isOwner && confirmingCancel && (
        <div className="mt-4 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper)] p-4" role="alertdialog" aria-label="Confirm cancellation">
          <p className="text-[14px] font-medium">Cancel your plan?</p>
          <p className="mt-1 text-[13.5px] leading-relaxed text-[var(--color-ink-soft)]">
            {periodEnd
              ? `Your receptionist keeps answering calls until ${periodEnd}. You won't be charged again.`
              : "Your receptionist keeps answering calls until the end of the period you've paid for. You won't be charged again."}
          </p>
          <div className="mt-3 flex flex-wrap gap-3">
            <button onClick={cancelPlan} disabled={busy !== null} aria-busy={busy === "cancel"} className={BTN_PRIMARY}>
              {busy === "cancel" ? "Cancelling…" : "Yes, cancel my plan"}
            </button>
            <button onClick={() => setConfirmingCancel(false)} disabled={busy !== null} className={BTN_QUIET}>
              Keep my plan
            </button>
          </div>
        </div>
      )}

      {notice && (
        <div
          ref={noticeRef}
          role={notice.tone === "bad" ? "alert" : "status"}
          data-testid="cancel-notice"
          className={`mt-4 rounded-xl p-4 text-[13.5px] leading-relaxed ${TONE_STYLES[notice.tone]}`}
        >
          <p className="text-[14px] font-semibold">{notice.title}</p>
          <p className="mt-1">{notice.body}</p>
          {notice.action && (
            <div className="mt-3 flex flex-wrap gap-3">
              <button onClick={checkAgain} disabled={busy !== null} className={BTN_QUIET}>
                {busy === "refresh" ? "Checking…" : "Check status"}
              </button>
              {notice.action === "retry" && isOwner && !confirmingCancel && (
                <button onClick={retryRequest} disabled={busy !== null} className={BTN_QUIET}>
                  Send request again
                </button>
              )}
            </div>
          )}
        </div>
      )}

      <p aria-live="polite" className={`mt-3 text-[13.5px] ${message?.bad ? "text-[#7a2416]" : "text-[var(--color-ink-soft)]"}`}>
        {message?.text}
      </p>
    </section>
  );
}