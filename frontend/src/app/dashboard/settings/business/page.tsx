import type { Metadata } from "next";
import { BusinessView } from "@/components/settings/views/BusinessView";

export const metadata: Metadata = { title: "Business · Settings · Atla" };

export default function Page() {
  return <BusinessView />;
}
