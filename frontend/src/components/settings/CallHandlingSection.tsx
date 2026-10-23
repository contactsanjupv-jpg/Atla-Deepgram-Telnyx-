"use client";

import { api, CallHandling, describeError } from "@/lib/api";
import { liveBadge, syncBadge } from "@/lib/settingsLogic";
import { useEffect, useState } from "react";
import { SettingsSection } from "./ui";

export function CallHandlingSection({ orgId, businessId, onLoaded }: { orgId: string; businessId: string; onLoaded?: (d: CallHandling) => void }) {
  const [data, setData] = useState<CallHandling | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getCallHandling(orgId, businessId)
      .then((d) => { if (!cancelled) { setData(d); setError(null); onLoaded?.(d); } })
      .catch((e) => !cancelled && setError(describeError(e, "We couldn't check your receptionist's status right now.")));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, businessId]);

  const badge = data ? liveBadge(data) : null;
  const tone = badge?.tone === "ok" ? "bg-[#e3f1e4] text-[#1f5a2a]" : badge?.tone === "warn" ? "bg-[#fbefd5] text-[#7a5200]" : "bg-[var(--color-line)] text-[var(--color-ink-soft)]";

  return (
    <SettingsSection id="overview" title="Call handling" description="Your number and whether your receptionist is taking calls right now.">
      {error && <p className="text-[14px] text-[#7a2416]">{error}</p>}
      {!data && !error && <p className="text-[14px] text-[var(--color-ink-soft)]">Checking status…</p>}
      {data && badge && (
        <>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-4 text-[14.5px] sm:grid-cols-3">
            <div className="min-w-0">
              <dt className="text-[12.5px] text-[var(--color-ink-soft)]">Business number</dt>
              <dd className="break-words font-medium">{data.business_number ?? "No number yet"}</dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[12.5px] text-[var(--color-ink-soft)]">Receptionist</dt>
              <dd><span data-live-state={data.receptionist_state} className={`inline-block rounded-full px-2.5 py-0.5 text-[12.5px] font-medium ${tone}`}>{badge.label}</span></dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[12.5px] text-[var(--color-ink-soft)]">Sync status</dt>
              <dd data-sync-state={data.sync_state} className="font-medium">{syncBadge(data.sync_state)}</dd>
            </div>
          </dl>
          <p className="mt-3 break-words text-[13.5px] leading-snug text-[var(--color-ink-soft)]">
            {data.receptionist_message}
            {!data.business_number && <> {data.number_message}</>}
          </p>
        </>
      )}
    </SettingsSection>
  );
}
