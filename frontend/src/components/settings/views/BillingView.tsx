"use client";

import { BillingPanel } from "@/components/billing/BillingPanel";
import { useSettings } from "../SettingsContext";

export function BillingView() {
  const { org } = useSettings();
  return <BillingPanel orgId={org.id} role={org.role} />;
}
