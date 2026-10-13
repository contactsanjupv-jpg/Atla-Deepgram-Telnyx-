"use client";

import Link from "next/link";
import { CheckCircle2 } from "lucide-react";
import type { AttentionItem } from "@/lib/dashboard";
import { DOT } from "./tone";

/** What the customer can act on right now. An empty list says so plainly instead of inventing busywork. */
export function AttentionList({ items, ready }: { items: AttentionItem[]; ready: boolean }) {
  if (!ready) {
    return <div className="h-[72px] animate-pulse rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" aria-busy="true" aria-label="Loading" />;
  }
  if (items.length === 0) {
    return (
      <div className="flex items-center gap-2.5 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5 text-[13.5px] text-[var(--color-ink-soft)]">
        <CheckCircle2 className="h-4 w-4 flex-shrink-0 text-[var(--color-ok)]" strokeWidth={1.75} aria-hidden />
        Nothing needs your attention right now.
      </div>
    );
  }
  return (
    <section aria-labelledby="attention-h" className="rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]">
      <h2 id="attention-h" className="px-4 pt-4 text-[15px] font-semibold sm:px-5">
        Needs your attention
      </h2>
      <ul className="mt-2 divide-y divide-[var(--color-line)]">
        {items.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-5">
            <div className="flex min-w-0 flex-1 basis-60 items-start gap-2.5">
              <span className={`mt-1.5 h-2 w-2 flex-shrink-0 rounded-full ${DOT[item.tone]}`} aria-hidden />
              <div className="min-w-0">
                <div className="break-words text-[14px] font-medium">{item.title}</div>
                <div className="break-words text-[13px] text-[var(--color-ink-soft)]">{item.detail}</div>
              </div>
            </div>
            <Link href={item.cta.href} className="flex-shrink-0 rounded-full border border-[var(--color-line)] px-3.5 py-1.5 text-[13px] font-medium hover:bg-[var(--color-line)]/50">
              {item.cta.label}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}