"use client";

import Link from "next/link";
import { PhoneCall, Users } from "lucide-react";
import type { AwaitingLead, CallOut } from "@/lib/api";
import { relativeTime } from "@/lib/dashboard";
import { formatPhone } from "@/lib/receptionistStatus";

function Shell({ title, href, children }: { title: string; href: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-4 sm:p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <Link href={href} className="text-[13px] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
          View all →
        </Link>
      </div>
      {children}
    </section>
  );
}

function Rows({ n }: { n: number }) {
  return (
    <div className="mt-3 space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="h-[52px] animate-pulse rounded-lg bg-[var(--color-line)]/60" />
      ))}
    </div>
  );
}

function Empty({ icon: Icon, text }: { icon: typeof PhoneCall; text: string }) {
  return (
    <div className="mt-3 flex flex-col items-center rounded-xl border border-dashed border-[var(--color-line)] px-4 py-8 text-center">
      <Icon className="h-6 w-6 text-[var(--color-ink-soft)]" strokeWidth={1.5} aria-hidden />
      <p className="mt-2 max-w-xs text-[13px] text-[var(--color-ink-soft)]">{text}</p>
    </div>
  );
}

function duration(seconds: number | null): string | null {
  if (seconds == null) return null;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function RecentCalls({ calls, loading, error, onRetry }: { calls: CallOut[] | null; loading: boolean; error: string | null; onRetry: () => void }) {
  return (
    <Shell title="Recent calls" href="/dashboard/calls">
      {loading && !calls ? (
        <Rows n={3} />
      ) : error && !calls ? (
        <div className="mt-3" role="alert">
          <p className="text-[13px] text-[#7a2416]">{error}</p>
          <button onClick={onRetry} className="mt-2 rounded-full border border-[var(--color-line)] px-3.5 py-1 text-[12.5px] font-medium">
            Try again
          </button>
        </div>
      ) : !calls || calls.length === 0 ? (
        <Empty icon={PhoneCall} text="No calls yet. They'll appear here as soon as your receptionist takes one." />
      ) : (
        <ul className="mt-3 space-y-2">
          {calls.map((c) => {
            const when = relativeTime(c.started_at);
            const len = duration(c.duration_seconds);
            return (
              <li key={c.id} className="min-w-0 rounded-lg border border-[var(--color-line)] px-3.5 py-2.5">
                <div className="flex items-baseline justify-between gap-3 text-[13.5px]">
                  <span className="min-w-0 truncate font-medium">{c.direction === "test" ? "Test call" : c.caller_number ? formatPhone(c.caller_number) : "Unknown caller"}</span>
                  <span className="flex-shrink-0 text-[12.5px] text-[var(--color-ink-soft)]">{[when, len].filter(Boolean).join(" · ")}</span>
                </div>
                {c.summary && <p className="mt-0.5 line-clamp-2 break-words text-[13px] text-[var(--color-ink-soft)]">{c.summary}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </Shell>
  );
}

export function WaitingLeads({ leads, count, loading, error, onRetry }: { leads: AwaitingLead[] | null; count: number; loading: boolean; error: string | null; onRetry: () => void }) {
  return (
    <Shell title="Leads waiting for a follow-up" href="/dashboard/leads">
      {loading && !leads ? (
        <Rows n={3} />
      ) : error && !leads ? (
        <div className="mt-3" role="alert">
          <p className="text-[13px] text-[#7a2416]">{error}</p>
          <button onClick={onRetry} className="mt-2 rounded-full border border-[var(--color-line)] px-3.5 py-1 text-[12.5px] font-medium">
            Try again
          </button>
        </div>
      ) : !leads || leads.length === 0 ? (
        <Empty icon={Users} text="No leads are waiting. When a caller leaves their details, they'll show up here." />
      ) : (
        <>
          <ul className="mt-3 space-y-2">
            {leads.map((l) => (
              <li key={l.id} className="min-w-0 rounded-lg border border-[var(--color-line)] px-3.5 py-2.5">
                <div className="flex items-baseline justify-between gap-3 text-[13.5px]">
                  <span className="min-w-0 truncate font-medium">{l.name || "Unnamed lead"}</span>
                  <span className="flex-shrink-0 text-[12.5px] text-[var(--color-ink-soft)]">{relativeTime(l.created_at)}</span>
                </div>
                <p className="mt-0.5 truncate text-[13px] text-[var(--color-ink-soft)]">{[l.phone ? formatPhone(l.phone) : null, l.reason].filter(Boolean).join(" · ") || "No details left"}</p>
              </li>
            ))}
          </ul>
          {count > leads.length && <p className="mt-2 text-[12.5px] text-[var(--color-ink-soft)]">Showing the {leads.length} oldest of {count}.</p>}
        </>
      )}
    </Shell>
  );
}