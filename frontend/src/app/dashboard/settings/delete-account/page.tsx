import type { Metadata } from "next";
import { DeleteAccountView } from "@/components/settings/views/DeleteAccountView";

export const metadata: Metadata = { title: "Delete account · Settings · Atla" };

export default function Page() {
  return <DeleteAccountView />;
}
