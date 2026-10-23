"use client";

import { useSettings } from "../SettingsContext";
import { BusinessProfileSection } from "../BusinessProfileSection";
import { RequireBusiness } from "./RequireBusiness";

export function BusinessView() {
  const { org } = useSettings();
  return <RequireBusiness>{(businessId) => <BusinessProfileSection orgId={org.id} businessId={businessId} />}</RequireBusiness>;
}
