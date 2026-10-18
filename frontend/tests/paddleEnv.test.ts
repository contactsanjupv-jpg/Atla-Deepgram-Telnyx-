import test from "node:test";
import assert from "node:assert/strict";
import { resolvePaddleEnvironment } from "../src/lib/paddleEnv.ts";

test("sandbox server + test_ token -> sandbox", () => {
  assert.deepEqual(resolvePaddleEnvironment("sandbox", "test_abc"), { ok: true, environment: "sandbox" });
});

test("live server + live_ token -> production", () => {
  assert.deepEqual(resolvePaddleEnvironment("live", "live_abc"), { ok: true, environment: "production" });
});

test("a token from the other environment is refused (both directions)", () => {
  assert.equal(resolvePaddleEnvironment("live", "test_abc").ok, false);
  assert.equal(resolvePaddleEnvironment("sandbox", "live_abc").ok, false);
});

test("missing token, unknown or missing server environment fail closed", () => {
  assert.equal(resolvePaddleEnvironment("live", undefined).ok, false);
  assert.equal(resolvePaddleEnvironment(undefined, "live_abc").ok, false);
  assert.equal(resolvePaddleEnvironment("production", "live_abc").ok, false);
  assert.equal(resolvePaddleEnvironment("", "test_abc").ok, false);
});
