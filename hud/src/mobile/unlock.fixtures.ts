// Fakes for WS-5c's unlock flow in the phone's tests (not a test file): op tokens of the server's
// shape (`o1.<payload>.<mac>`, low-entropy markers, never a real key), the issue route, and the unlock
// routes' happy answers. Shared by unlock.test.ts and the app harnesses.
import type { Got, Posted } from "./net";

const b64url = (s: string): string => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

let counter = 0;

/** A token of the server's shape whose nonce is `n` plus the counter (22 characters). */
export function fakeToken(i: number = counter++): string {
  const n = "n" + String(i).padStart(21, "0");
  return `o1.${b64url(JSON.stringify({ sid: "s000000000000000000000", n, t: 1 }))}.${"A".repeat(43)}`;
}

/** The nonce a `fakeToken(i)` carries. */
export const fakeNonce = (i: number): string => "n" + String(i).padStart(21, "0");

/** `GET /m/api/ops/tokens`: four fresh tokens each read. */
export function tokensGot(): Got {
  return { ok: true, body: { tokens: [fakeToken(), fakeToken(), fakeToken(), fakeToken()], ttl_s: 600 } };
}

export const UNLOCK_OPTIONS_REPLY: Posted = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
export const UNLOCK_REPLY: Posted = { status: 200, body: { unlocked: true, unlock_s: 3600 } };

/** An app harness's `gets` and `replies` taught the issue route (fresh tokens per read) and the unlock routes. */
export function installUnlock(gets: Record<string, Got>, replies: Record<string, Posted>): void {
  Object.defineProperty(gets, "/m/api/ops/tokens", { get: tokensGot, enumerable: true, configurable: true });
  replies["/m/api/unlock/options"] = UNLOCK_OPTIONS_REPLY;
  replies["/m/api/unlock"] = UNLOCK_REPLY;
}

/** The action POSTs among a harness's posts (the unlock routes left out). */
export function actionPosts<T extends { path: string }>(posts: T[]): T[] {
  return posts.filter((p) => !p.path.startsWith("/m/api/unlock") && p.path !== "/m/api/lock");
}
