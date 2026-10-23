"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { activeSettingsView, SETTINGS_VIEWS } from "@/lib/settingsLogic";

/**
 * Real navigation between settings views (each its own route), so back/forward and direct links work.
 * Desktop: sticky vertical list. Mobile: horizontally scrollable pills; the active pill is kept in view.
 */
export function SettingsNav() {
  const pathname = usePathname();
  const active = activeSettingsView(pathname)?.id ?? null;
  const activeRef = useRef<HTMLAnchorElement | null>(null);

  useEffect(() => {
    // Keep the active pill visible in the scrollable mobile row (no effect on the vertical desktop list).
    activeRef.current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [active]);

  return (
    <nav aria-label="Settings" className="min-w-0 lg:sticky lg:top-8 lg:self-start">
      <ul className="-mx-4 flex gap-2 overflow-x-auto px-4 py-2 [scrollbar-width:none] sm:-mx-6 sm:px-6 md:mx-0 md:px-0 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:py-0 [&::-webkit-scrollbar]:hidden">
        {SETTINGS_VIEWS.map((v) => {
          const isActive = v.id === active;
          const danger = v.id === "delete-account";
          return (
            <li key={v.id} className="shrink-0">
              <Link
                ref={isActive ? activeRef : undefined}
                href={v.href}
                aria-current={isActive ? "page" : undefined}
                className={`inline-flex min-h-10 items-center rounded-full border px-3.5 text-[13.5px] font-medium lg:min-h-0 lg:w-full lg:rounded-lg lg:border-0 lg:px-3 lg:py-2 ${
                  isActive
                    ? "border-[var(--color-ink)] bg-[var(--color-ink)] text-[var(--color-paper)]"
                    : `border-[var(--color-line)] bg-[var(--color-paper-raised)] lg:bg-transparent lg:hover:bg-[var(--color-line)] ${
                        danger ? "text-[#7a2416]" : "text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
                      }`
                }`}
              >
                {v.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
