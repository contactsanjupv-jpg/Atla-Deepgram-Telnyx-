import type { Metadata } from "next";
import { VoiceView } from "@/components/settings/views/VoiceView";

export const metadata: Metadata = { title: "Voice · Settings · Atla" };

export default function Page() {
  return <VoiceView />;
}
