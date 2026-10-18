/**
 * The backend is authoritative for the Paddle environment (GET /api/v1/public-config -> {paddle_env}).
 * Checkout opens in THAT environment, and only if this build's client token belongs to it. Paddle documents the
 * client-side token prefix as "test_" for sandbox and "live_" for production. Anything unclear fails closed:
 * the browser never guesses an environment, so a sandbox/live mix-up cannot reach a customer silently.
 */
export type PaddleEnvironment = "sandbox" | "production";

export type EnvCheck = { ok: true; environment: PaddleEnvironment } | { ok: false; reason: string };

export function resolvePaddleEnvironment(serverEnv: unknown, token: string | undefined): EnvCheck {
  if (!token) return { ok: false, reason: "payments not configured" };
  if (serverEnv === "sandbox") {
    return token.startsWith("test_")
      ? { ok: true, environment: "sandbox" }
      : { ok: false, reason: "billing environment mismatch: server is sandbox but the client token is not a sandbox token" };
  }
  if (serverEnv === "live") {
    return token.startsWith("live_")
      ? { ok: true, environment: "production" }
      : { ok: false, reason: "billing environment mismatch: server is live but the client token is not a live token" };
  }
  return { ok: false, reason: "billing environment unknown" };
}
