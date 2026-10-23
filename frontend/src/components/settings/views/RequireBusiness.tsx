"use client";

import Link from "next/link";
import { useSettings } from "../SettingsContext";
import { SettingsSection } from "../ui";

/** Renders `children(businessId)` only once the business is known; otherwise an honest placeholder. */
export function RequireBusiness({ children }: { children: (businessId: string) => React.ReactNode }) {
  const { businessId, businessState } = useSettings();
  if (businessState === "ready" && businessId) return <>{children(businessId)}</>;
  return (
    <SettingsSection id="needs-business" title="Business &amp; receptionist">
      {businessState === "loading" && <p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p>}
      {businessState === "none" && (
        <p className="text-[14px] text-[var(--color-ink-soft)]">
          Set up your business first. <Link href="/onboarding" className="font-medium underline underline-offset-2">Start setup</Link>
        </p>
      )}
      {businessState === "error" && <p className="text-[14px] text-[#7a2416]">We couldn&apos;t load your business settings. Please refresh in a moment.</p>}
    </SettingsSection>
  );
}
