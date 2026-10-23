import type { Metadata } from "next";
import { ReceptionistView } from "@/components/settings/views/ReceptionistView";

export const metadata: Metadata = { title: "Receptionist · Settings · Atla" };

export default function Page() {
  return <ReceptionistView />;
}
