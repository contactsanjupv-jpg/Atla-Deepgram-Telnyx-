import type { Metadata } from "next";
import { BillingView } from "@/components/settings/views/BillingView";

export const metadata: Metadata = { title: "Billing · Settings · Atla" };

export default function Page() {
  return <BillingView />;
}
