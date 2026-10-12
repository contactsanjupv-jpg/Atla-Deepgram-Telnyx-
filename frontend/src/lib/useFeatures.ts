"use client";

import { useEffect, useState } from "react";
import { api } from "./api";

/** Which paid features the server says this organization has right now. `null` while loading or unreadable. */
export function useFeatures(orgId: string | undefined): { features: Set<string> | null; plan: string | null } {
  const [data, setData] = useState<{ features: Set<string>; plan: string | null } | null>(null);
  useEffect(() => {
    if (!orgId) return;
    let live = true;
    api
      .getSubscription(orgId)
      .then((sub) => {
        if (live) setData({ features: new Set(sub?.entitled ? sub.features.map((f) => f.id) : []), plan: sub?.plan_name ?? null });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [orgId]);
  return { features: data?.features ?? null, plan: data?.plan ?? null };
}
