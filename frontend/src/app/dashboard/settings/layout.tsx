"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api";
import { useOrganization } from "@/lib/useOrganization";
import { activeSettingsView } from "@/lib/settingsLogic";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";
import { SettingsNav } from "@/components/settings/SettingsNav";
import { SettingsContext, type BusinessState } from "@/components/settings/SettingsContext";

/**
 * Shared frame for every settings route: dashboard shell, page header and the section nav.
 * Only the selected route's view is rendered as `children` — nothing else is mounted.
 */
export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  const { org, loading, error } = useOrganization();
  const pathname = usePathname();
  const view = activeSettingsView(pathname);
  const [businessId, setBusinessId] = useState<string | null>(null);
  const [businessState, setBusinessState] = useState<BusinessState>("loading");

  const orgId = org?.id;
  useEffect(() => {
    if (!orgId) return;
    let cancelled = false;
    api
      .getOnboarding(orgId)
      .then((o) => {
        if (cancelled) return;
        setBusinessId(o.business_id);
        setBusinessState(o.business_id ? "ready" : "none");
      })
      .catch(() => !cancelled && setBusinessState("error"));
    return () => {
      cancelled = true;
    };
  }, [orgId]);

  const ctx = useMemo(() => (org ? { org, businessId, businessState } : null), [org, businessId, businessState]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;
  if (!org || !ctx) return null;

  return (
    <DashboardShell org={org}>
      <div className="mx-auto w-full max-w-5xl">
        <header>
          <p className="text-[13px] font-medium text-[var(--color-ink-soft)]">Settings</p>
          <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">{view?.label ?? "Settings"}</h1>
          {view && <p className="mt-1 text-[14.5px] text-[var(--color-ink-soft)]">{view.description}</p>}
        </header>

        <div className="mt-4 lg:mt-6 lg:grid lg:grid-cols-[11rem_minmax(0,1fr)] lg:gap-10">
          <div className="sticky top-[57px] z-20 -mx-4 border-b border-[var(--color-line)] bg-[var(--color-paper)]/95 px-4 backdrop-blur sm:-mx-6 sm:px-6 md:static md:mx-0 md:border-0 md:bg-transparent md:px-0 lg:contents">
            <SettingsNav />
          </div>
          <div id="settings-view" role="region" className="mt-5 min-w-0 lg:mt-0" aria-labelledby="settings-view-h">
            <h2 id="settings-view-h" className="sr-only">{view?.label ?? "Settings"}</h2>
            <SettingsContext.Provider value={ctx}>{children}</SettingsContext.Provider>
          </div>
        </div>
      </div>
    </DashboardShell>
  );
}
