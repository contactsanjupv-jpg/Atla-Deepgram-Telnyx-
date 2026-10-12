"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Plus } from "lucide-react";
import { api, describeError, SubscriptionOut } from "@/lib/api";
import { formatPrice } from "@/lib/billing";
import { openCheckout, paymentsConfigured, transactionIdFrom } from "@/lib/paddle";
import { planHighlights } from "@/lib/planCompare";
import { useCheckoutState } from "@/lib/useCheckoutState";
import { usePlans } from "@/lib/usePlans";
import { WizardActions, ErrorBanner } from "./WizardShell";

// Plan names, allowances, features and prices come from the backend plan catalog (backed by the Paddle
// prices) — nothing about plans is hardcoded in this file.

export function StepPlan({
  orgId,
  onSubscribed,
  onBack,
}: {
  orgId: string;
  onSubscribed: () => void;
  onBack: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [subscription, setSubscription] = useState<SubscriptionOut | null>(null);
  const [busyPlan, setBusyPlan] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { plans, loading: plansLoading, failed: plansFailed, reload: reloadPlans } = usePlans();

  // `entitled` is the server's verdict; the browser never decides who has paid.
  const isActive = subscription !== null && subscription.entitled;

  const refreshSubscription = () =>
    api
      .getSubscription(orgId)
      .then((sub) => {
        setSubscription(sub);
        return sub;
      })
      .catch(() => null);

  useEffect(() => {
    api
      .getSubscription(orgId)
      .then((sub) => setSubscription(sub))
      .catch(() => setError("Couldn't check your plan. Is the backend running?"))
      .finally(() => setLoading(false));
  }, [orgId]);

  // What the server says about a checkout that's already in flight (resume / processing / expired).
  const checkout = useCheckoutState(orgId, !loading && !isActive, () => {
    void refreshSubscription();
  });

  // After Paddle reports payment, the subscription row is written by the webhook (server-to-server). Each poll
  // also asks the server to read the payment back from Paddle, so a lost webhook can't strand the customer.
  useEffect(() => {
    if (!waiting) return;
    let cancelled = false;
    (async () => {
      for (let i = 0; i < 30 && !cancelled; i++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        try {
          await api.getCheckoutState(orgId);
          const sub = await api.getSubscription(orgId);
          if (cancelled) return;
          if (sub && sub.entitled) {
            setSubscription(sub);
            setWaiting(false);
            return;
          }
        } catch {
          // transient error — keep polling
        }
      }
      if (!cancelled) {
        setWaiting(false);
        setError("Your payment was received, but confirmation is taking longer than usual. Please don't pay again — check back in a minute.");
        void checkout.refresh();
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waiting, orgId]);

  async function startCheckout(checkoutUrl: string) {
    const transactionId = transactionIdFrom(checkoutUrl);
    if (!transactionId) throw new Error("checkout unavailable");
    await openCheckout(transactionId, {
      onCompleted: () => {
        setBusyPlan(null);
        setWaiting(true);
      },
      onClosed: () => {
        setBusyPlan(null);
        void checkout.refresh();
      },
    });
  }

  async function handleChoose(plan: string) {
    setError(null);
    if (!paymentsConfigured) {
      setError("Payments aren't configured yet.");
      return;
    }
    setBusyPlan(plan);
    try {
      const { checkout_url } = await api.createCheckoutSession(orgId, plan as "starter" | "growth");
      await startCheckout(checkout_url);
    } catch (err) {
      setBusyPlan(null);
      setError(describeError(err, "We couldn't start checkout. Please try again."));
      void checkout.refresh(); // a 409 means a checkout exists: show the way forward
    }
  }

  async function handleResume() {
    setError(null);
    const url = checkout.state?.checkout_url;
    if (!url) return;
    setBusyPlan(checkout.state?.plan ?? "resume");
    try {
      await startCheckout(url);
    } catch (err) {
      setBusyPlan(null);
      setError(describeError(err, "We couldn't reopen your checkout. Please try again."));
    }
  }

  async function handleCheckAgain() {
    setError(null);
    const next = await checkout.refresh();
    const sub = await refreshSubscription();
    if ((!sub || !sub.entitled) && next?.state !== "processing") setError("Not confirmed yet — try again in a minute.");
  }

  const cs = checkout.state;
  const inFlight = cs !== null && cs.state !== "none" && cs.state !== "expired";
  const highlights = plans ? planHighlights(plans) : [];

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Choose your plan</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        A plan is needed before we can set up your phone number and put your receptionist live.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}

        {loading ? (
          <p className="text-[14px] text-[var(--color-ink-soft)]">Checking your plan…</p>
        ) : isActive ? (
          <div className="flex items-center gap-3 rounded-xl border border-[var(--color-ok)]/30 bg-[var(--color-ok)]/10 px-4 py-3.5">
            <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-[var(--color-ok)]" />
            <div className="text-[15px] font-medium">
              {subscription?.plan_name ? `You're on the ${subscription.plan_name} plan` : "Your plan is active"}
            </div>
          </div>
        ) : waiting || cs?.state === "processing" ? (
          <div className="rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5" aria-live="polite">
            <p className="text-[15px] font-medium">Confirming your payment…</p>
            <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">
              {cs?.message || "Your payment is being confirmed. This usually takes a few seconds. Please don't pay again."}
            </p>
          </div>
        ) : plansFailed ? (
          <div>
            <p className="text-[14px] text-[var(--color-ink-soft)]">We couldn&apos;t load the plans right now.</p>
            <button onClick={reloadPlans} className="mt-3 text-[13.5px] font-medium underline">
              Try again
            </button>
          </div>
        ) : plansLoading || plans === null ? (
          <div className="grid animate-pulse gap-3 sm:grid-cols-2" aria-busy="true" aria-label="Loading plans">
            {[0, 1].map((i) => (
              <div key={i} className="h-48 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
            ))}
          </div>
        ) : (
          <>
            {inFlight && cs && (
              <div className="mb-4 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4" aria-live="polite">
                <p className="text-[15px] font-medium">
                  {cs.state === "open" ? `Your ${String(cs.plan ?? "").replace(/^./, (c) => c.toUpperCase())} checkout is still open` : "We're checking your payment"}
                </p>
                <p className="mt-1 text-[13.5px] leading-relaxed text-[var(--color-ink-soft)]">{cs.message}</p>
                <div className="mt-3 flex flex-wrap gap-3">
                  {cs.state === "open" && cs.checkout_url && (
                    <button
                      onClick={handleResume}
                      disabled={busyPlan !== null}
                      className="rounded-full bg-[var(--color-ink)] px-5 py-2 text-[13.5px] font-medium text-[var(--color-paper)] disabled:opacity-50"
                    >
                      {busyPlan !== null ? "Opening checkout…" : "Resume checkout"}
                    </button>
                  )}
                  {cs.state === "unconfirmed" && (
                    <button onClick={handleCheckAgain} className="rounded-full border border-[var(--color-line)] px-5 py-2 text-[13.5px] font-medium">
                      Check again
                    </button>
                  )}
                </div>
              </div>
            )}
            {cs?.state === "expired" && <p className="mb-3 text-[13.5px] text-[var(--color-ink-soft)]">{cs.message}</p>}

            <div className="grid gap-3 sm:grid-cols-2">
              {highlights.map((h) => {
                const { plan } = h;
                const price = formatPrice(plan.price);
                return (
                  <button
                    key={plan.id}
                    onClick={() => handleChoose(plan.id)}
                    disabled={busyPlan !== null || (cs?.state === "unconfirmed")}
                    className={`relative rounded-xl bg-[var(--color-paper-raised)] p-5 text-left transition-colors hover:border-[var(--color-ink)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-ink)] disabled:opacity-60 ${
                      h.recommended ? "border-2 border-[var(--color-ink)]" : "border border-[var(--color-line)]"
                    }`}
                  >
                    {h.recommended && (
                      <span className="absolute -top-2.5 left-4 rounded-full bg-[var(--color-ink)] px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-[var(--color-paper)]">
                        Recommended
                      </span>
                    )}
                    <div className="text-[15px] font-medium">{plan.name}</div>
                    <div className="mt-1 font-[family-name:var(--font-display)] text-[28px] font-bold">
                      {price ? (
                        <>
                          {price.amount}
                          <span className="text-[14px] font-normal text-[var(--color-ink-soft)]">{price.cadence}</span>
                        </>
                      ) : (
                        <span className="text-[15px] font-normal text-[var(--color-ink-soft)]">Price shown at checkout</span>
                      )}
                    </div>
                    <div className="mt-2 text-[13.5px] font-medium">{plan.included_minutes.toLocaleString()} minutes every month</div>
                    {h.inherits && <p className="mt-3 text-[12.5px] text-[var(--color-ink-soft)]">Everything in {h.inherits}, plus:</p>}
                    <ul className="mt-2 space-y-1.5">
                      {h.added.map((f) => (
                        <li key={f.id} className="flex items-start gap-1.5 text-[13px] text-[var(--color-ink-soft)]">
                          {h.inherits ? (
                            <Plus className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
                          ) : (
                            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
                          )}
                          {f.label}
                        </li>
                      ))}
                    </ul>
                    <div className="mt-3 text-[13.5px] font-medium">{busyPlan === plan.id ? "Opening checkout…" : `Choose ${plan.name}`}</div>
                  </button>
                );
              })}
            </div>
            <p className="mt-3 text-[12.5px] leading-relaxed text-[var(--color-ink-soft)]">{plans[0]?.when_credits_run_out}</p>
          </>
        )}

        {!loading && !isActive && !waiting && error && (
          <button onClick={handleCheckAgain} className="mt-4 text-[13.5px] font-medium underline">
            Already paid? Check again
          </button>
        )}
      </div>

      <WizardActions onBack={onBack} onNext={onSubscribed} nextDisabled={!isActive} />
    </div>
  );
}
