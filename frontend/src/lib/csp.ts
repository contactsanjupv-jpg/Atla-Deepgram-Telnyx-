/**
 * Content-Security-Policy, built per request.
 *
 * Why a nonce: Next.js emits inline bootstrap <script> tags in every HTML response. A policy without 'unsafe-inline'
 * blocks them and the app never hydrates. Instead of allowing ALL inline scripts, each response carries a fresh random
 * nonce; Next reads it from the request's CSP header and stamps it on its own scripts, so only those run.
 * 'strict-dynamic' lets those trusted scripts load their own children (e.g. Paddle.js, injected by
 * @paddle/paddle-js); in browsers that honour it, host allow-lists and 'self' are ignored for scripts, which is
 * stricter. The host entries below are only the fallback for browsers without 'strict-dynamic' support.
 *
 * style-src still allows 'unsafe-inline' (unchanged from the previous policy; Paddle's overlay and React inline
 * style attributes need it). That is a known, lower-risk residual; see the audit notes.
 */

export function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export interface CspOptions {
  nonce: string;
  /** Origin of the Atla API the browser talks to (NEXT_PUBLIC_API_BASE_URL). */
  apiOrigin: string;
  isDev: boolean;
}

export function buildCsp({ nonce, apiOrigin, isDev }: CspOptions): string {
  // One connect-src only: browsers honour the first duplicate directive and ignore the rest.
  const connectSrc = [
    "'self'",
    apiOrigin,
    "https://*.paddle.com",
    "https://api.retellai.com",
    "https://*.livekit.cloud",
    "wss://*.livekit.cloud",
    ...(isDev ? ["ws://localhost:*"] : []),
  ].join(" ");

  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https://cdn.paddle.com${isDev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline' https://*.paddle.com",
    "frame-src 'self' https://*.paddle.com",
    "img-src 'self' data: https:",
    "media-src 'self' https:",
    `connect-src ${connectSrc}`,
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}
