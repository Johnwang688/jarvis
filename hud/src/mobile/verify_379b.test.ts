import { afterEach, expect, it } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";

class Stream implements StreamLike {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage = null;
  onerror = null;
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  close() { this.readyState = 2; }
}

const apps: PhoneApp[] = [];
afterEach(() => apps.splice(0).forEach(app => app.stop()));
const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };

it.each([502, 503, 504])("stream-open /me %s must not leave the orb claiming Live", async status => {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const stream = new Stream();
  let me: Got = { ok: true, body: { signed_in: true, secure: { ok: true }, unlock_s: 1800 } };
  // Hold only the new /me response until the visible-list refreshes have finished.
  let pendingMe: Promise<Got> | null = null;
  const app = new PhoneApp({ root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async path => path === "/m/api/me" ? (pendingMe ?? me) : { ok: true, body: {
      rows: [], tasks: [], open: 0, entries: [], total: 0, more: false } },
    openStream: () => stream,
  });
  apps.push(app);
  app.start(); await settle();
  stream.open(); await settle();
  const word = root.querySelector<HTMLElement>('[data-testid="m-conn"]')!;
  expect(word.textContent).toBe("Live");
  let answer!: (got: Got) => void;
  pendingMe = new Promise(resolve => { answer = resolve; });
  stream.open(); await settle();
  me = { ok: false, why: "offline", status };
  answer(me); await settle();
  expect(app.failStatus).toBe(status);
  expect({ word: word.textContent, tone: word.dataset.tone })
    .toEqual({ word: "Jarvis unreachable", tone: "err" });
  // A failed session read cannot prove the unlock window closed.
  expect(root.querySelector<HTMLElement>('[data-testid="m-lock-pill"]')!.dataset.state).toBe("unlocked");
});
