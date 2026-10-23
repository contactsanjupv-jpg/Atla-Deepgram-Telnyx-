"use client";

import type { SaveOutcome } from "@/lib/settingsLogic";
import { SAVE_LABEL } from "@/lib/settingsLogic";

// 16px text on small screens stops iOS Safari zooming into focused fields; 44px min height = touch target.
export const INPUT =
  "block w-full min-w-0 min-h-11 rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[16px] outline-none focus:border-[var(--color-ink)] sm:min-h-0 sm:text-[15px]";
export const CARD = "rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5 sm:p-6";
export const BTN =
  "inline-flex min-h-11 items-center justify-center rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[14px] font-medium text-[var(--color-paper)] disabled:opacity-50 sm:min-h-0 sm:text-[13.5px]";
export const BTN_GHOST =
  "inline-flex min-h-10 shrink-0 items-center justify-center rounded-full border border-[var(--color-line)] px-3.5 py-1.5 text-[13px] font-medium hover:bg-[var(--color-line)] disabled:opacity-50 sm:min-h-0";
export const BTN_LINK =
  "inline-flex min-h-10 items-center text-[13px] font-medium text-[var(--color-ink-soft)] underline-offset-2 hover:text-[var(--color-ink)] hover:underline sm:min-h-0";

export function Field({ label, children, className = "" }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={`block min-w-0 ${className}`}>
      <span className="mb-1 block text-[13px] font-medium text-[var(--color-ink-soft)]">{label}</span>
      {children}
    </label>
  );
}

/** A settings card with an anchor id (for the section nav), title and one-line description. */
export function SettingsSection({
  id, title, description, children, tone = "default",
}: { id: string; title: string; description?: string; children: React.ReactNode; tone?: "default" | "danger" }) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-h`}
      className={`${CARD} scroll-mt-32 md:scroll-mt-6 ${tone === "danger" ? "!border-[#e3b9b0]" : ""}`}
    >
      <h2 id={`${id}-h`} className={`text-[17px] font-semibold ${tone === "danger" ? "text-[#7a2416]" : ""}`}>{title}</h2>
      {description && <p className="mt-1 text-[14px] leading-snug text-[var(--color-ink-soft)]">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

/** A labelled group inside a form (e.g. "Basics", "Voice"). */
export function Group({ id, title, hint, children }: { id?: string; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div id={id} className="scroll-mt-32 border-t border-[var(--color-line)] pt-5 first:border-t-0 first:pt-0 md:scroll-mt-6">
      <h3 className="text-[14.5px] font-semibold">{title}</h3>
      {hint && <p className="mt-0.5 text-[13px] text-[var(--color-ink-soft)]">{hint}</p>}
      <div className="mt-3 space-y-3">{children}</div>
    </div>
  );
}

/** Collapsible list group (native <details>: keyboard + screen-reader friendly). Open by default so nothing is hidden. */
export function Collapsible({
  title, count, defaultOpen = true, children,
}: { title: string; count: number; defaultOpen?: boolean; children: React.ReactNode }) {
  return (
    <details open={defaultOpen} className="group rounded-xl border border-[var(--color-line)]">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3.5 py-2 text-[14px] font-medium [&::-webkit-details-marker]:hidden">
        <span>{title} <span className="font-normal text-[var(--color-ink-soft)]">({count})</span></span>
        <span aria-hidden className="text-[var(--color-ink-soft)] transition-transform group-open:rotate-180">▾</span>
      </summary>
      <div className="space-y-2 border-t border-[var(--color-line)] p-3">{children}</div>
    </details>
  );
}

/** Save area that stays reachable: sticks above the mobile tab bar, to the viewport bottom on desktop. */
export function SaveBar({ children }: { children: React.ReactNode }) {
  return (
    <div className="sticky bottom-[calc(4.5rem+env(safe-area-inset-bottom))] z-10 -mx-5 mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-b-2xl border-t border-[var(--color-line)] bg-[var(--color-paper-raised)]/95 px-5 py-3 backdrop-blur sm:-mx-6 sm:px-6 md:bottom-0">
      {children}
    </div>
  );
}

/** Saving / Saved / Sync failed / Changes not applied / Not saved — only ever from a real outcome. */
export function SaveStatus({ outcome }: { outcome: SaveOutcome }) {
  if (outcome.state === "idle" && !outcome.message) return null;
  const bad = outcome.state === "sync_failed" || outcome.state === "not_applied" || outcome.state === "error";
  return (
    <p
      role={bad ? "alert" : "status"}
      data-save-state={outcome.state}
      className={`min-w-0 flex-1 basis-56 break-words text-[13.5px] ${bad ? "text-[#7a2416]" : "text-[var(--color-ink-soft)]"}`}
    >
      {SAVE_LABEL[outcome.state] && <strong className="mr-1.5">{SAVE_LABEL[outcome.state]}.</strong>}
      {outcome.state !== "saving" && (outcome.state === "error" ? (outcome.message ?? "").replace(/^Not saved\.\s*/, "") : outcome.message)}
    </p>
  );
}
