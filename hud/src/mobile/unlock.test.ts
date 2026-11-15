import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Got, Posted } from "./net";
import {
  CODE_WORDS, codeWords, definite, LOCK_URL, ME_URL, Mirror, NOT_READY, nonceOf, opWords, pillWords, POOL_MAX,
  TAKE_MARGIN_MS, TOKENS_URL, UNLOCK_OPTIONS_URL, UNLOCK_URL, Unlock, type Outcome, type Timer,
} from "./unlock";
import { fakeNonce, fakeToken, tokensGot } from "./unlock.fixtures";

const ACT = "/m/api/inbox/appr%3Ar1/answer";

/** A fake server for the controller: queued replies per path, every request recorded. */
function server(opts: { open?: boolean } = {}) {
  let clock = 1_000_000;
  const posts: { path: string; body: any }[] = [];
  const gets: string[] = [];
  const queue: Record<string, Posted[]> = {};
  const fixed: Record<string, Posted> = {
    [UNLOCK_OPTIONS_URL]: { status: 200, body: { publicKey: { challenge: "AAEC" } } },
    [UNLOCK_URL]: { status: 200, body: { unlocked: true, unlock_s: 3600 } },
    [LOCK_URL]: { status: 200, body: { unlocked: false } },
  };
  const reads: Record<string, Got> = { [ME_URL]: { ok: true, body: { signed_in: true, unlock_s: 0 } } };
  let tokens: () => Got = tokensGot;
  const timers: { fn: () => void; ms: number; cleared: boolean }[] = [];
  const timer: Timer = {
    set: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
    clear: (h) => { (h as { cleared: boolean }).cleared = true; },
  };
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const signedOut = vi.fn();
  const gate: { unlock: Promise<void> | null } = { unlock: null };
  let active: boolean | null = null;
  const u = new Unlock({
    fetchJson: async (path) => {
      gets.push(path);
      if (path === TOKENS_URL) return tokens();
      return reads[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } };
    },
    postJson: async (path, value) => {
      posts.push({ path, body: JSON.parse(JSON.stringify(value)) });
      if (path === UNLOCK_URL && gate.unlock) await gate.unlock;
      const q = queue[path];
      if (q && q.length) return q.shift()!;
      return fixed[path] ?? { status: 200, body: { ok: true } };
    },
    getCredential: credential,
    activation: () => active,
    now: () => clock,
    timer,
    signedOut,
  });
  if (opts.open) u.mirror.fromMe(3000);
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  return {
    u, posts, gets, queue, fixed, reads, timers, credential, signedOut, gate, settle,
    tick: (ms: number) => { clock += ms; },
    setTokens: (fn: () => Got) => { tokens = fn; },
    setActive: (v: boolean | null) => { active = v; },
    paths: () => posts.map((p) => p.path),
    acts: () => posts.filter((p) => p.path === ACT),
  };
}

async function filled(opts: { open?: boolean } = {}) {
  const s = server(opts);
  await s.u.pool.refill();
  return s;
}

describe("WS-5c rule 1: the op-token pool", () => {
  it("fills to 4, refills when one is used, at half its life and on becoming visible", async () => {
    const s = server();
    expect(s.u.pool.size).toBe(0);
    await s.u.pool.refill();
    expect(s.u.pool.size).toBe(POOL_MAX);
    expect(s.u.pool.reads).toBe(1);
    const t = s.u.pool.take();
    expect(t).toMatch(/^o1\./);
    expect(s.u.pool.reads).toBe(2);            // taken: a refill started at once
    await s.settle();
    expect(s.u.pool.size).toBe(POOL_MAX);      // the newest 4 are kept
    // Half its life: the timer the last refill set fires a refill.
    const half = s.timers.filter((x) => !x.cleared).at(-1)!;
    expect(half.ms).toBe(300_000);
    half.fn();
    await s.settle();
    expect(s.u.pool.reads).toBe(3);
    // Visible: another.
    await s.u.pool.refill();
    expect(s.u.pool.reads).toBe(4);
  });

  it("an empty pool freezes nothing and sends nothing; the tap's words say try again", async () => {
    const s = server();
    s.setTokens(() => ({ ok: false, why: "offline", status: 0 }));
    const op = s.u.freeze("k", ACT, { decision: "allow" });
    expect(op).toBeNull();
    await s.settle();
    expect(s.posts).toEqual([]);
    expect(s.u.pool.reads).toBe(1);            // it still asked for more
    expect(NOT_READY).toBe("Not ready: try again in a moment.");
  });

  it("never hands out a token about to expire; never holds duplicates", async () => {
    const s = server();
    const same = fakeToken(900);
    s.setTokens(() => ({ ok: true, body: { tokens: [same, same, same, same], ttl_s: 600 } }));
    await s.u.pool.refill();
    expect(s.u.pool.size).toBe(1);
    s.tick(600_000 - TAKE_MARGIN_MS);
    expect(s.u.pool.size).toBe(0);
    s.setTokens(() => ({ ok: false, why: "error", status: 429 }));
    expect(s.u.freeze("k", ACT, {})).toBeNull();
  });

  it("a 401 from the issue route is signed out", async () => {
    const s = server();
    s.setTokens(() => ({ ok: false, why: "signed-out", status: 401 }));
    await s.u.pool.refill();
    expect(s.signedOut).toHaveBeenCalledTimes(1);
  });
});

describe("the frozen request", () => {
  it("is a detached, frozen copy with its op token: changing the source after the tap changes nothing", async () => {
    const s = await filled();
    const fields = { id: "pr:o/r#1", head: "a".repeat(40), card: "c1", nested: { n: [1] } };
    const op = s.u.freeze("pr", "/m/api/inbox/pr", fields)!;
    fields.head = "b".repeat(40);
    fields.nested.n.push(2);
    expect(op.body).toEqual({ id: "pr:o/r#1", head: "a".repeat(40), card: "c1", nested: { n: [1] }, op: op.token });
    expect(Object.isFrozen(op)).toBe(true);
    expect(Object.isFrozen(op.body)).toBe(true);
    expect(Object.isFrozen((op.body as any).nested)).toBe(true);
    expect(op.n).toBe(nonceOf(op.token));
  });

  it("run twice (a double tap) sends once", async () => {
    const s = await filled({ open: true });
    const op = s.u.freeze("k", ACT, { decision: "allow" })!;
    const [a, b] = await Promise.all([s.u.run(op), s.u.run(op)]);
    expect(a).toBe(b);
    expect(s.acts().length).toBe(1);
  });
});

describe("rules 3 and 4: the locked shortcut and the 423 path", () => {
  it("locked: unlock/options, Face ID, unlock, then the action, and no action POST before the unlock", async () => {
    const s = await filled();
    expect(s.u.mirror.open()).toBe(false);
    const op = s.u.freeze("k", ACT, { decision: "allow" })!;
    const out = await s.u.run(op);
    expect(out.kind).toBe("done");
    expect(s.paths()).toEqual([UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
    expect(s.credential).toHaveBeenCalledTimes(1);
    expect(s.credential).toHaveBeenCalledWith({ challenge: "AAEC" });
    expect(s.posts[1].body).toEqual({ assertion: { id: "cred", response: {} } });
    expect(s.acts()[0].body).toEqual({ decision: "allow", op: op.token });
    expect(s.u.mirror.open()).toBe(true);
  });

  it("unlocked: the action only, no Face ID", async () => {
    const s = await filled({ open: true });
    const out = await s.u.run(s.u.freeze("k", ACT, { decision: "allow" })!);
    expect(out.kind).toBe("done");
    expect(s.paths()).toEqual([ACT]);
    expect(s.credential).not.toHaveBeenCalled();
  });

  it("a 423 while the mirror said open: one unlock, then exactly one resend of the SAME frozen request", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }];
    const op = s.u.freeze("k", ACT, { decision: "allow" })!;
    const readsBefore = s.u.pool.reads;
    const out = await s.u.run(op);
    expect(out.kind).toBe("done");
    expect(s.paths()).toEqual([ACT, UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
    const [first, second] = s.acts();
    expect(second.body).toEqual(first.body);
    expect(second.body.op).toBe(op.token);
    // No token was taken for the resend.
    expect(s.u.pool.reads).toBe(readsBefore);
    expect(s.u.sends(op)).toBe(2);
  });

  it("a second 423 stops: no third send and no second unlock", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }, { status: 423, body: { error: "locked", unlock: true } }];
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(out.kind).toBe("locked-again");
    expect(s.paths()).toEqual([ACT, UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
    expect(s.u.words(out)).toBe("Locked again before it could be sent: nothing was done. Tap again.");
  });

  it("a 423 after the locked shortcut's own unlock is not unlocked again", async () => {
    const s = await filled();
    s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }];
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(out.kind).toBe("locked-again");
    expect(s.paths()).toEqual([UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
  });

  it("two taps while locked share one unlock", async () => {
    const s = await filled();
    const a = s.u.freeze("a", ACT, { decision: "allow" })!;
    const b = s.u.freeze("b", "/m/api/inbox/pr", { id: "x" })!;
    await Promise.all([s.u.run(a), s.u.run(b)]);
    expect(s.paths().filter((p) => p === UNLOCK_OPTIONS_URL).length).toBe(1);
    expect(s.credential).toHaveBeenCalledTimes(1);
  });
});

describe("rule 5: no automatic retry", () => {
  const cases: [string, (s: Awaited<ReturnType<typeof filled>>) => void, string, string[]][] = [
    ["a cancelled Face ID", (s) => { s.credential.mockResolvedValueOnce(null); }, "cancelled", [ACT, UNLOCK_OPTIONS_URL]],
    ["Face ID throwing", (s) => { s.credential.mockRejectedValueOnce(new Error("NotAllowedError")); }, "cancelled", [ACT, UNLOCK_OPTIONS_URL]],
    ["an unlock 409", (s) => { s.queue[UNLOCK_URL] = [{ status: 409, body: { error: "challenge expired" } }]; }, "unlock-refused",
      [ACT, UNLOCK_OPTIONS_URL, UNLOCK_URL]],
    ["an unlock 503", (s) => { s.queue[UNLOCK_URL] = [{ status: 503, body: { error: "passkeys unavailable" } }]; }, "unlock-refused",
      [ACT, UNLOCK_OPTIONS_URL, UNLOCK_URL]],
    ["an unlock with no answer", (s) => { s.queue[UNLOCK_URL] = [{ status: 0, body: {}, offline: true }]; }, "unlock-lost",
      [ACT, UNLOCK_OPTIONS_URL, UNLOCK_URL]],
    ["unlock options refused", (s) => { s.queue[UNLOCK_OPTIONS_URL] = [{ status: 503, body: { error: "passkeys unavailable" } }]; },
      "unlock-refused", [ACT, UNLOCK_OPTIONS_URL]],
  ];
  for (const [name, arrange, kind, paths] of cases) {
    it(`after ${name}, after a 423: nothing is resent`, async () => {
      const s = await filled({ open: true });
      s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }];
      arrange(s);
      const out = await s.u.run(s.u.freeze("k", ACT, {})!);
      expect(out.kind).toBe(kind);
      expect(s.paths()).toEqual(paths);
      expect(s.u.words(out)).toMatch(/nothing was sent/);
    });
  }

  it("an ambiguous answer (no response) is never resent: op status is read instead", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 0, body: {}, offline: true }];
    const op = s.u.freeze("k", ACT, {})!;
    s.reads[`/m/api/ops/${op.n}`] = { ok: true, body: { state: "running", outcome: null, code: null, words: "x" } };
    const out = await s.u.run(op);
    expect(out).toEqual({ kind: "recovered", why: "lost", status: { state: "running", outcome: null, code: null } });
    expect(s.acts().length).toBe(1);
    expect(s.gets).toContain(`/m/api/ops/${op.n}`);
    expect(s.u.words(out)).toBe("No answer came back. Still running.");
  });

  it("a 502 or 504 from the proxy is ambiguous too: one send, then op status", async () => {
    for (const status of [502, 504]) {
      const s = await filled({ open: true });
      s.queue[ACT] = [{ status, body: {} }];
      const out = await s.u.run(s.u.freeze("k", ACT, {})!);
      expect(out.kind).toBe("recovered");
      expect(s.acts().length).toBe(1);
    }
  });

  it("a 410: 'tap again', nothing resent, and the pool refills", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 410, body: { error: "this tap expired: nothing was done by this request", expired: true } }];
    const op = s.u.freeze("k", ACT, {})!;
    await s.settle();
    const reads = s.u.pool.reads;
    const out = await s.u.run(op);
    expect(out.kind).toBe("expired");
    expect(s.acts().length).toBe(1);
    expect(s.u.pool.reads).toBe(reads + 1);
    expect(s.u.words(out, "k")).toBe("This tap expired: nothing was done. Tap again.");
  });

  it("a frozen token that outlives its life during Face ID gets the 410, never a replacement", async () => {
    const s = await filled();
    const op = s.u.freeze("k", ACT, {})!;
    s.credential.mockImplementationOnce(async () => { s.tick(700_000); return { id: "cred", response: {} }; });
    s.queue[ACT] = [{ status: 410, body: { error: "expired", expired: true } }];
    const out = await s.u.run(op);
    expect(out.kind).toBe("expired");
    expect(s.acts().map((p) => p.body.op)).toEqual([op.token]);
  });

  it("tokens from before a restart (410 forged) refill the pool, and the next tap uses a fresh one", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 410, body: { error: "this tap is from before Jarvis restarted" } }];
    const first = s.u.freeze("k", ACT, {})!;
    await s.u.run(first);
    await s.settle();
    const second = s.u.freeze("k", ACT, {})!;
    expect(second.token).not.toBe(first.token);
  });
});

describe("rule 6: the fallback resumes the same op", () => {
  it("Face ID refused because the activation lapsed: 'Unlock and send' sends the same token once, unlocking first", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }];
    s.setActive(false);
    s.credential.mockRejectedValueOnce(Object.assign(new Error("lapsed"), { name: "NotAllowedError" }));
    const op = s.u.freeze("k", ACT, { decision: "allow" })!;
    await s.settle();
    const reads = s.u.pool.reads;
    const size = s.u.pool.size;
    const out = await s.u.run(op);
    expect(out.kind).toBe("needs-tap");
    expect(s.paths()).toEqual([ACT, UNLOCK_OPTIONS_URL]);
    s.setActive(true);
    const again = await s.u.resume(op);
    expect(again.kind).toBe("done");
    expect(s.paths()).toEqual([ACT, UNLOCK_OPTIONS_URL, UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
    const [a, b] = s.acts();
    expect(b.body).toEqual(a.body);
    expect(b.body.op).toBe(op.token);
    expect(s.u.pool.reads).toBe(reads);
    expect(s.u.pool.size).toBe(size);
  });

  it("from the locked path too, and the resumed op is still sent only once", async () => {
    const s = await filled();
    s.setActive(false);
    s.credential.mockResolvedValueOnce(null);
    const op = s.u.freeze("k", ACT, {})!;
    expect((await s.u.run(op)).kind).toBe("needs-tap");
    expect(s.acts().length).toBe(0);
    s.setActive(null);
    s.queue[ACT] = [{ status: 423, body: { error: "locked", unlock: true } }];
    expect((await s.u.resume(op)).kind).toBe("locked-again");
    expect(s.acts().length).toBe(1);
  });
});

describe("rule 7: outcome recovery, in fixed words", () => {
  const row = (state: string, outcome: string | null, code: string | null) => opWords({ state, outcome, code });

  it("the table: Done only for completed + ok; update_requested is never Done", () => {
    expect(row("completed", "ok", "merged")).toBe("Done");
    expect(row("completed", "ok", "allowed")).toBe("Done");
    expect(row("completed", "ok", "update_requested")).toBe("Update requested; check at the desk");
    expect(row("refused", null, null)).toBe("Not done");
    expect(row("completed", "failed", "not_updated")).toBe("Not done: not updated");
    expect(row("completed", "failed", "changed")).toBe("Not done: the pull request changed");
    expect(row("completed", "failed", "other")).toBe("Not done");
    expect(row("running", null, null)).toBe("Still running");
    expect(row("completed", "unknown", "not_read_again")).toBe("Finished; check at the desk");
    expect(row("uncertain", null, null)).toBe("Unknown: check at the desk");
    expect(row("unknown", null, null)).toBe("Unknown: check at the desk");
    expect(opWords(null)).toBe("Unknown: check at the desk");
  });

  it("covers every code the backend records, each in its own fixed words; an unknown code is 'other'", () => {
    const src = readFileSync(join(__dirname, "../../../jarvis/v2/remote_auth.py"), "utf8");
    const block = src.slice(src.indexOf("PR_CODES = {"), src.indexOf("def parse_op"));
    const codes = new Set([...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).filter((c) => /^[a-z]+(_[a-z]+)*$/.test(c) && !c.includes(" ")));
    for (const fam of ["pr", "approval", "task", "working", "review"]) codes.delete(fam);
    for (const words of ["update requested", "not updated", "not read again", "head moved", "not mergeable"]) codes.delete(words);
    codes.add("other");
    expect(codes.size).toBe(17);
    for (const code of codes) {
      expect(Object.keys(CODE_WORDS)).toContain(code);
      expect(codeWords(code)).toBeTruthy();
      for (const state of ["completed", "refused", "running", "uncertain", "unknown"]) {
        for (const outcome of ["ok", "failed", "unknown", null]) {
          const w = opWords({ state, outcome, code });
          if (w === "Done") expect([state, outcome, code !== "update_requested"]).toEqual(["completed", "ok", true]);
          expect(w.length).toBeGreaterThan(0);
        }
      }
    }
    expect(codeWords("gh said something")).toBe(CODE_WORDS.other);
    expect(new Set(Object.values(CODE_WORDS)).size).toBe(Object.keys(CODE_WORDS).length);
  });

  it("never reuses the desk's prRow resultOk", () => {
    const src = readFileSync(join(__dirname, "unlock.ts"), "utf8");
    expect(src).not.toMatch(/from "\.\.\/lib\/prRow"|resultOk\(/);
  });

  it("a duplicate is never success: op status decides, and no second token is taken for the tap", async () => {
    const s = await filled({ open: true });
    const op = s.u.freeze("k", ACT, {})!;
    await s.settle();
    s.queue[ACT] = [{ status: 409, body: { error: "already sent", duplicate: true, state: "completed" } }];
    s.reads[`/m/api/ops/${op.n}`] = { ok: true, body: { state: "completed", outcome: "ok", code: "update_requested" } };
    const reads = s.u.pool.reads;
    const out = await s.u.run(op);
    expect(out.kind).toBe("recovered");
    expect(s.u.words(out)).toBe("This tap was already sent. Update requested; check at the desk.");
    expect(s.u.pool.reads).toBe(reads);
    expect(s.acts().length).toBe(1);
  });

  it("an uncertain 409 reads op status; an unreadable status is Unknown", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 409, body: { error: "it may not have gone through", uncertain: true } }];
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(s.u.words(out)).toBe("Jarvis could not say how it ended. Unknown: check at the desk.");
  });

  it("uncertainty survives a 410 on the same control; a definite answer clears it", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 0, body: {}, offline: true }, { status: 410, body: { error: "expired", expired: true } }];
    const first = await s.u.run(s.u.freeze("pr:x:merge", ACT, {})!);
    expect(first.kind).toBe("recovered");
    expect(s.u.unsure.has("pr:x:merge")).toBe(true);
    await s.settle();
    const second = await s.u.run(s.u.freeze("pr:x:merge", ACT, {})!);
    expect(second.kind).toBe("expired");
    const words = s.u.words(second, "pr:x:merge");
    expect(words).toContain("still unknown");
    expect(words).not.toContain("nothing was done");
    expect(s.u.unsure.has("pr:x:merge")).toBe(true);
    await s.settle();
    await s.u.run(s.u.freeze("pr:x:merge", ACT, {})!);
    expect(s.u.unsure.has("pr:x:merge")).toBe(false);
    expect(definite({ state: "completed", outcome: "ok", code: "merged" })).toBe(true);
    expect(definite({ state: "running", outcome: null, code: null })).toBe(false);
  });
});

describe("rule 9: a 401 is signed out, never locked", () => {
  it("an action's 401 probes me; signed out there: signed out, and no unlock is tried", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 401, body: { error: "unauthorized" } }];
    s.reads[ME_URL] = { ok: false, why: "signed-out", status: 401 };
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(out.kind).toBe("signed-out");
    expect(s.signedOut).toHaveBeenCalledTimes(1);
    expect(s.paths()).toEqual([ACT]);
    expect(s.u.mirror.open()).toBe(true);
  });

  it("with the session still on, a 401 is a refusal: no unlock, no resend", async () => {
    const s = await filled({ open: true });
    s.queue[ACT] = [{ status: 401, body: { error: "unauthorized" } }];
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(out).toEqual({ kind: "refused", status: 401, body: { error: "unauthorized" } });
    expect(s.paths()).toEqual([ACT]);
    expect(s.credential).not.toHaveBeenCalled();
  });

  it("an unlock's 401 is the passkey not verified unless me says signed out", async () => {
    const s = await filled();
    s.queue[UNLOCK_URL] = [{ status: 401, body: { error: "unauthorized" } }];
    const out = await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(out.kind).toBe("unlock-refused");
    expect(s.u.words(out)).toBe("Not accepted: Face ID was not verified. Nothing was sent.");
    expect(s.acts().length).toBe(0);
  });
});

describe("rule 2: the mirror, and Lock now", () => {
  it("follows me, slides on a gated 2xx, and a session-only 2xx slides it only while open", () => {
    let now = 0;
    const m = new Mirror(() => now);
    m.fromMe(0);
    expect(m.open()).toBe(false);
    m.slideSession();                       // a 202 message while locked: never opens it
    expect(m.open()).toBe(false);
    m.fromMe(60);
    expect(m.open()).toBe(true);
    now = 30_000;
    m.slideSession();                       // while open: an hour from now
    expect(m.until).toBe(30_000 + 3_600_000);
    m.fromMe("x");                          // junk changes nothing
    expect(m.until).toBe(30_000 + 3_600_000);
    now = 30_000 + 3_600_000;
    expect(m.open()).toBe(false);
    expect(pillWords(m, now)).toBe("Locked");
    m.slideGated();
    expect(pillWords(m, now)).toMatch(/^Unlocked until /);
  });

  it("Lock now posts once and locks the mirror; the next tap takes the locked path", async () => {
    const s = await filled({ open: true });
    expect(await s.u.lockNow()).toBe("locked");
    expect(s.paths()).toEqual([LOCK_URL]);
    expect(s.posts[0].body).toEqual({});
    expect(s.u.mirror.open()).toBe(false);
    await s.u.run(s.u.freeze("k", ACT, {})!);
    expect(s.paths()).toEqual([LOCK_URL, UNLOCK_OPTIONS_URL, UNLOCK_URL, ACT]);
  });

  it("a Lock now that does not reach Jarvis leaves the mirror as it was (never a false 'locked')", async () => {
    const s = await filled({ open: true });
    s.queue[LOCK_URL] = [{ status: 0, body: {}, offline: true }];
    expect(await s.u.lockNow()).toBe("unreached");
    expect(s.u.mirror.open()).toBe(true);
  });

  it("the nonce is read from the token for op status", () => {
    expect(nonceOf(fakeToken(7))).toBe(fakeNonce(7));
    expect(nonceOf("o1.bad.token")).toBeNull();
  });
});

describe("words for every outcome the flow can end in", () => {
  it("each says whether anything was sent", () => {
    const s = server();
    const outs: [Outcome, string][] = [
      [{ kind: "cancelled" }, "Cancelled, or Face ID did not verify: nothing was sent."],
      [{ kind: "needs-tap" }, "Face ID needs a fresh tap: press Unlock and send."],
      [{ kind: "unlock-lost" }, "No answer to the unlock: nothing was sent. Tap again."],
      [{ kind: "unlock-refused", status: 409, body: {} }, "The unlock expired or was replaced: nothing was sent. Tap again."],
      [{ kind: "unlock-refused", status: 503, body: {} }, "Passkeys are unavailable at Jarvis: nothing was sent."],
      [{ kind: "expired" }, "This tap expired: nothing was done. Tap again."],
      [{ kind: "locked-again" }, "Locked again before it could be sent: nothing was done. Tap again."],
      [{ kind: "recovered", why: "lost", status: { state: "completed", outcome: "ok", code: "merged" } }, "No answer came back. Done."],
    ];
    for (const [o, w] of outs) expect(s.u.words(o)).toBe(w);
    expect(s.u.words({ kind: "done", status: 200, body: {} })).toBe("");
  });
});
