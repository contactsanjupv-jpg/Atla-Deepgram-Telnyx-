import type { Metadata } from "next";
import { OverviewView } from "@/components/settings/views/OverviewView";

export const metadata: Metadata = { title: "Settings · Atla" };

export default function Page() {
  return <OverviewView />;
}
