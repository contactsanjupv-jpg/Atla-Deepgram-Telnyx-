import { resolvePaddleEnvironment, type PaddleEnvironment } from "./paddleEnv";
import { CheckoutEventNames, initializePaddle, type Paddle } from "@paddle/paddle-js";

const TOKEN = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN;
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

/** Asks the backend which Paddle environment it uses and verifies this build's token belongs to it. */
async function checkedEnvironment(): Promise<PaddleEnvironment> {
  const res = await fetch(`${API_BASE}/api/v1/public-config`, { credentials: "include" });
  if (!res.ok) throw new Error("billing environment unavailable");
  const body = (await res.json()) as { paddle_env?: unknown };
  const check = resolvePaddleEnvironment(body.paddle_env, TOKEN);
  if (!check.ok) {
    console.error(check.reason);
    throw new Error(check.reason);
  }
  return check.environment;
}

interface Handlers {
  onCompleted: () => void;
  onClosed: () => void;
}

let paddle: Promise<Paddle | undefined> | null = null;
let current: Handlers | null = null;

export const paymentsConfigured = Boolean(TOKEN);

/** "…/checkout?_ptxn=txn_01h…" -> "txn_01h…". The browser only ever opens a transaction OUR server created. */
export function transactionIdFrom(checkoutUrl: string): string | null {
  return checkoutUrl.match(/txn_[a-z0-9]+/i)?.[0] ?? null;
}

/**
 * Opens Paddle's checkout for a server-created transaction. A "completed" callback means "Paddle took the
 * payment" — it never unlocks anything by itself: callers re-read the server's state afterwards.
 */
export async function openCheckout(transactionId: string, handlers: Handlers): Promise<void> {
  if (!TOKEN) throw new Error("payments not configured");
  current = handlers;
  const environment = await checkedEnvironment(); // fails closed on a sandbox/live mismatch, before any checkout opens
  if (!paddle) {
    paddle = initializePaddle({
      environment,
      token: TOKEN,
      eventCallback: (event) => {
        if (event.name === CheckoutEventNames.CHECKOUT_COMPLETED) current?.onCompleted();
        else if (event.name === CheckoutEventNames.CHECKOUT_CLOSED) current?.onClosed();
      },
    });
  }
  const instance = await paddle;
  if (!instance) throw new Error("checkout unavailable");
  instance.Checkout.open({ transactionId });
}
