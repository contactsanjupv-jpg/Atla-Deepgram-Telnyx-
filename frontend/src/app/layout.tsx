import type { Metadata } from "next";
import { connection } from "next/server";
import { Bricolage_Grotesque, Public_Sans } from "next/font/google";
import "./globals.css";

const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  weight: ["500", "600", "700", "800"],
});

const publicSans = Public_Sans({
  variable: "--font-public-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: "Atla — an AI receptionist that answers every call",
  description:
    "Atla answers your business phone, captures every lead, and never misses a call — set up in minutes, no tech knowledge needed.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // The CSP nonce is generated per request (src/proxy.ts), so every page must be rendered per request, never prerendered.
  await connection();
  return (
    <html lang="en">
      <body className={`${bricolage.variable} ${publicSans.variable} antialiased`}>{children}</body>
    </html>
  );
}
