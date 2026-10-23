"use client";

import Link from "next/link";
import { useState } from "react";
import type { CallHandling } from "@/lib/api";
import { nextSteps, SETTINGS_VIEWS } from "@/lib/settingsLogic";
import { useSettings } from "../SettingsContext";
import { CallHandlingSection } from "../CallHandlingSection";
import { SettingsSection } from "../ui";
import { RequireBusiness } from "./RequireBusiness";

/** Concise overview only: status strip, next steps and links to each category. No forms live here. */
export function OverviewView() {
  const { org } = useSettings();
  const [ch, setCh] = useState<CallHandling | null>(null);
  const steps = nextSteps(ch);

  return (
    <div className="space-y-6">
      <RequireBusiness>
        {(businessId) => (
          <>
            <CallHandlingSection orgId={org.id} businessId={businessId} onLoaded={setCh} />
            {ch && (
              <SettingsSection id="next-steps" title="Next steps">
                {steps.length === 0 ? (
                  <p className="text-[14px] text-[var(--color-ink-soft)]">Nothing needs your attention right now.</p>
                ) : (
                  <ul className="space-y-3">
                    {steps.map((s) => (
                      <li key={s.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-[14px]">
                        <span className="min-w-0 flex-1 basis-56 break-words">{s.text}</span>
                        {s.action && (
                          <Link href={s.action.href} className="inline-flex min-h-10 shrink-0 items-center rounded-full border border-[var(--color-line)] px-3.5 text-[13px] font-medium hover:bg-[var(--color-line)] sm:min-h-0 sm:py-1.5">
                            {s.action.label}
                          </Link>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </SettingsSection>
            )}
          </>
        )}
      </RequireBusiness>

      <SettingsSection id="categories" title="Manage">
        <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {SETTINGS_VIEWS.filter((v) => v.id !== "overview").map((v) => (
            <li key={v.id}>
              <Link href={v.href} className="block min-h-11 rounded-xl border border-[var(--color-line)] px-3.5 py-2.5 hover:bg-[var(--color-line)]">
                <span className={`block text-[14.5px] font-medium ${v.id === "delete-account" ? "text-[#7a2416]" : ""}`}>{v.label}</span>
                <span className="block text-[12.5px] leading-snug text-[var(--color-ink-soft)]">{v.description}</span>
              </Link>
            </li>
          ))}
        </ul>
      </SettingsSection>
    </div>
  );
}
