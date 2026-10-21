import { NextRequest, NextResponse } from "next/server";
import { buildCsp, generateNonce } from "@/lib/csp";

/** Sets a per-request nonce CSP (Next.js 16 "proxy", formerly middleware). Pages must render dynamically (see layout.tsx). */
export function proxy(request: NextRequest) {
  const nonce = generateNonce();
  const csp = buildCsp({
    nonce,
    apiOrigin: process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000",
    isDev: process.env.NODE_ENV !== "production",
  });

  // Next.js reads the nonce from the CSP header on the REQUEST and applies it to its own scripts while rendering.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // Everything except static assets and prefetches, per the Next.js CSP guide.
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
