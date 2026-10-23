"use client";

import { useSettings } from "../SettingsContext";
import { ReceptionistSection } from "../ReceptionistSection";
import { RequireBusiness } from "./RequireBusiness";

export function ReceptionistView() {
  const { org } = useSettings();
  return <RequireBusiness>{(businessId) => <ReceptionistSection orgId={org.id} businessId={businessId} />}</RequireBusiness>;
}
