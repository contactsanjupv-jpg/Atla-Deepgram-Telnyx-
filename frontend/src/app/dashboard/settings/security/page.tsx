import type { Metadata } from "next";
import { SecurityView } from "@/components/settings/views/SecurityView";

export const metadata: Metadata = { title: "Security · Settings · Atla" };

export default function Page() {
  return <SecurityView />;
}
