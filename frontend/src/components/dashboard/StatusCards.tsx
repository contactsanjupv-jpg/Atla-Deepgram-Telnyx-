"use client";

import Link from "next/link";
import type { AgentOut, PhoneNumberOut, SubscriptionOut } from "@/lib/api";
import { describeSubscription } from "@/lib/billingState";
import { formatPhone, type ReceptionistStatus } from "@/lib/receptionistStatus";
import { DOT, TEXT } from "./tone";

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4 sm:p-5">
      <h2 className="text-[12px] font-medium uppercase tracking-wide text-[var(--color-ink-soft)]">{title}</h2>
      {children}
    </section>
  );
}

function Skeleton() {
  return (
    <div aria-busy="true" aria-label="Loading">
      <div className="mt-3 h-5 w-28 animate-pulse rounded bg-[var(--color-line)]" />
      <div className="mt-2.5 h-3 w-40 max-w-full animate-pulse rounded bg-[var(--color-line)]" />
    </div>
  );
}

const linkClass = "mt-3 inline-block text-[13px] font-medium underline underline-offset-2";

/** Receptionist, business number and plan — each from its own real source, each with its own loading / unknown state. */
export function StatusCards({
  loading,
  status,
  agent,
  agentKnown,
  phone,
  phoneKnown,
  sub,
}: {
  loading: boolean;
  status: ReceptionistStatus | null;
  agent: AgentOut | null;
  agentKnown: boolean;
  phone: PhoneNumberOut | null;
  phoneKnown: boolean;
  sub: SubscriptionOut | null | undefined;
}) {
  const info = sub ? describeSubscription(sub) : null;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <Card title="Receptionist">
        {loading ? (
          <Skeleton />
        ) : !status ? (
          <p className="mt-3 text-[13.5px] text-[var(--color-ink-soft)]">We couldn&apos;t load your receptionist&apos;s status. Refresh to try again.</p>
        ) : (
          <>
            <div className="mt-2.5 flex items-center gap-2">
              <span className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${DOT[status.tone]}`} aria-hidden />
              <span className={`text-[18px] font-semibold ${TEXT[status.tone]}`}>{status.label}</span>
            </div>
            <p className="mt-1 break-words text-[13.5px] text-[var(--color-ink-soft)]">
              {agent ? `${agent.name} · ` : ""}
              {status.detail}
            </p>
            {status.action && (
              <Link href={status.action.href} className={linkClass}>
                {status.action.label} →
              </Link>
            )}
          </>
        )}
      </Card>

      <Card title="Business number">
        {loading ? (
          <Skeleton />
        ) : !phoneKnown ? (
          <p className="mt-3 text-[13.5px] text-[var(--color-ink-soft)]">We couldn&apos;t load your number. Refresh to try again.</p>
        ) : phone ? (
          <>
            <div className="mt-2.5 break-words text-[18px] font-semibold">{formatPhone(phone.number)}</div>
            <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">Callers reach your receptionist here.</p>
          </>
        ) : (
          <>
            <div className="mt-2.5 text-[18px] font-semibold">No number yet</div>
            <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">Your receptionist can&apos;t answer real calls until a number is connected.</p>
            {agentKnown && agent && (
              <Link href="/dashboard/go-live" className={linkClass}>
                Set up your number →
              </Link>
            )}
          </>
        )}
      </Card>

      <Card title="Plan">
        {loading ? (
          <Skeleton />
        ) : sub === undefined ? (
          <p className="mt-3 text-[13.5px] text-[var(--color-ink-soft)]">We couldn&apos;t load your plan details. Refresh to try again.</p>
        ) : sub === null || !info ? (
          <>
            <div className="mt-2.5 text-[18px] font-semibold">No plan yet</div>
            <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">Choose a plan to put your receptionist on real calls.</p>
            <Link href="/dashboard/settings" className={linkClass}>
              See plans →
            </Link>
          </>
        ) : (
          <>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <span className="text-[18px] font-semibold">{sub.plan_name ?? "Your plan"}</span>
              <span className={`inline-flex items-center gap-1.5 text-[12.5px] font-medium ${TEXT[info.tone]}`}>
                <span className={`h-2 w-2 rounded-full ${DOT[info.tone]}`} aria-hidden />
                {info.label}
              </span>
            </div>
            <p className="mt-1 break-words text-[13.5px] text-[var(--color-ink-soft)]">{info.detail}</p>
            <Link href="/dashboard/settings" className={linkClass}>
              Manage plan →
            </Link>
          </>
        )}
      </Card>
    </div>
  );
}