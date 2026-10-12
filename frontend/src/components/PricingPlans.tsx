"use client";

import Link from "next/link";
import { Check, Plus } from "lucide-react";
import { formatPrice } from "@/lib/billing";
import { planHighlights, type PlanHighlight } from "@/lib/planCompare";
import { usePlans } from "@/lib/usePlans";

function Minutes({ minutes, extra, inherits }: { minutes: number; extra: number | null; inherits: string | null }) {
  return (
    <div className="mt-5 rounded-xl bg-[var(--color-paper)] px-4 py-3">
      <div className="text-[15px] font-semibold">{minutes.toLocaleString()} minutes every month</div>
      <div className="mt-0.5 text-[12.5px] text-[var(--color-ink-soft)]">
        {extra !== null && inherits ? `${extra.toLocaleString()} more than ${inherits}` : "of receptionist time, included"}
      </div>
    </div>
  );
}

function Card({ h, runOut }: { h: PlanHighlight; runOut: string }) {
  const { plan } = h;
  const price = formatPrice(plan.price);
  return (
    <div
      className={`relative flex flex-col rounded-2xl bg-[var(--color-paper-raised)] p-7 ${
        h.recommended ? "border-2 border-[var(--color-ink)] shadow-sm" : "border border-[var(--color-line)]"
      }`}
    >
      {h.recommended && (
        <span className="absolute -top-3 left-7 rounded-full bg-[var(--color-ink)] px-3 py-1 text-[11.5px] font-medium uppercase tracking-wide text-[var(--color-paper)]">
          Recommended
        </span>
      )}
      <h3 className="text-[16px] font-semibold">{plan.name}</h3>
      <div className="mt-3 flex items-baseline gap-1">
        {price ? (
          <>
            <span className="font-[family-name:var(--font-display)] text-[36px] font-bold">{price.amount}</span>
            <span className="text-[14px] text-[var(--color-ink-soft)]">{price.cadence}</span>
          </>
        ) : (
          <span className="text-[15px] text-[var(--color-ink-soft)]">Price shown at checkout</span>
        )}
      </div>

      <Minutes minutes={plan.included_minutes} extra={h.extraMinutes} inherits={h.inherits} />

      {h.inherits ? (
        <>
          <p className="mt-6 text-[13px] font-medium text-[var(--color-ink-soft)]">Everything in {h.inherits}, plus:</p>
          <ul className="mt-3 flex-1 space-y-3">
            {h.added.map((f) => (
              <li key={f.id} className="flex items-start gap-2 text-[14px] font-medium">
                <Plus className="mt-0.5 h-4 w-4 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
                {f.label}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <ul className="mt-6 flex-1 space-y-2.5">
          {[...plan.features.map((f) => f.label), ...plan.baseline].map((label) => (
            <li key={label} className="flex items-start gap-2 text-[14px]">
              <Check className="mt-0.5 h-4 w-4 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
              {label}
            </li>
          ))}
        </ul>
      )}

      <p className="mt-6 text-[12.5px] leading-relaxed text-[var(--color-ink-soft)]">{runOut}</p>

      <Link
        href="/get-started"
        className={`mt-5 rounded-full px-5 py-3 text-center text-[14px] font-medium transition-transform hover:scale-[1.02] active:scale-[0.98] ${
          h.recommended
            ? "bg-[var(--color-ink)] text-[var(--color-paper)]"
            : "border border-[var(--color-line)] hover:bg-[var(--color-line)]"
        }`}
      >
        Get started
      </Link>
    </div>
  );
}

/** Public plan cards. Names, prices, allowances and features all come from the backend catalog — nothing is hardcoded here. */
export function PricingPlans() {
  const { plans, loading, failed, reload } = usePlans();

  if (failed) {
    return (
      <div className="mx-auto max-w-md text-center">
        <p className="text-[15px] text-[var(--color-ink-soft)]">We couldn&apos;t load our plans right now.</p>
        <button
          onClick={reload}
          className="mt-4 rounded-full border border-[var(--color-line)] px-5 py-2.5 text-[13.5px] font-medium hover:bg-[var(--color-line)]"
        >
          Try again
        </button>
      </div>
    );
  }

  if (loading || plans === null) {
    return (
      <div className="mx-auto grid max-w-3xl animate-pulse gap-6 md:grid-cols-2" aria-busy="true" aria-label="Loading plans">
        {[0, 1].map((i) => (
          <div key={i} className="h-96 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
        ))}
      </div>
    );
  }

  const highlights = planHighlights(plans);
  const runOut = plans[0]?.when_credits_run_out ?? "";
  return (
    <div className="mx-auto grid max-w-3xl gap-8 md:grid-cols-2">
      {highlights.map((h) => (
        <Card key={h.plan.id} h={h} runOut={runOut} />
      ))}
    </div>
  );
}
