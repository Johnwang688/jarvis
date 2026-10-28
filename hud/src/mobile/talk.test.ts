import { describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { MAX_SAMPLES, RATE, STT_URL, Talk, WORDS, uploadNote, type AudioContextLike, type TalkEnv, type Uploaded } from "./talk";
import { cardView, parseDetail } from "./card";
import talkSource from "./talk.ts?raw";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// P-7 (jarvis/v2/remote_stt.py): push to talk in a chat's message box. A fake
// microphone and a fake Web Audio context throughout; never a real mic.

class Track {
  stopped = 0;
  stop() { this.stopped++; }
}

class Stream {
  tracks = [new Track(), new Track()];
  getTracks() { return this.tracks; }
}

class Ctx implements AudioContextLike {
  sampleRate = 48000;
  destination = {};
  closed = 0;
  resumed = 0;
  processor: { onaudioprocess: ((e: any) => void) | null; connect(): void; disconnect(): void } | null = null;
  disconnected = 0;
  resume = async () => { this.resumed++; };
  createMediaStreamSource() { return { connect() {}, disconnect: () => { this.disconnected++; } }; }
  createScriptProcessor() {
    this.processor = { onaudioprocess: null, connect() {}, disconnect: () => { this.disconnected++; } };
    return this.processor;
  }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect: () => { this.disconnected++; } }; }
  close = async () => { this.closed++; };
  /** `seconds` of a 220 Hz tone at the context's rate, in 4096-sample buffers. */
  feed(seconds: number) {
    const total = Math.round(seconds * this.sampleRate);
    for (let at = 0; at < total; at += 4096) {
      const n = Math.min(4096, total - at);
      const buf = new Float32Array(n);
      for (let i = 0; i < n; i++) buf[i] = 0.3 * Math.sin((2 * Math.PI * 220 * (at + i)) / this.sampleRate);
      this.processor?.onaudioprocess?.({ inputBuffer: { getChannelData: () => buf } });
    }
  }
}

function fakeEnv() {
  const streams: Stream[] = [];
  const contexts: Ctx[] = [];
  const uploads: Blob[] = [];
  let reply: Uploaded | (() => Promise<Uploaded>) = { status: 200, body: { text: "add a test" } };
  let gate: Promise<void> | null = null;
  let refuse: unknown = null;
  const getUserMedia = vi.fn(async (_c: MediaStreamConstraints) => {
    if (gate) await gate;
    if (refuse) throw refuse;
    const s = new Stream();
    streams.push(s);
    return s as unknown as MediaStream;
  });
  const timers: { fn: () => void; ms: number; cleared: boolean }[] = [];
  const env: TalkEnv = {
    media: () => ({ getUserMedia }),
    context: () => { const c = new Ctx(); contexts.push(c); return c; },
    upload: vi.fn(async (wav: Blob) => { uploads.push(wav); return typeof reply === "function" ? reply() : reply; }),
    now: () => 0,
    setTimer: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
    clearTimer: (t) => { (t as { cleared: boolean }).cleared = true; },
  };
  return {
    env, streams, contexts, uploads, getUserMedia, timers,
    reply: (r: Uploaded | (() => Promise<Uploaded>)) => { reply = r; },
    hold: () => { let open!: () => void; gate = new Promise<void>((r) => { open = r; }); return () => { gate = null; open(); }; },
    refuse: (e: unknown) => { refuse = e; },
    ctx: () => contexts[contexts.length - 1],
  };
}

class FakeStream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

const CHAT = "aaaaaaaa", OTHER = "dddddddd", PROJECT = "cccccccc", TASK = "bbbbbbbb";

function row(over: Record<string, unknown>) {
  return { title: "t", blocks: "b", project_id: null, thread_id: null, task_id: null, asked_at: null,
    expires_at: null, origin: "", detail: {}, ...over };
}

const ROWS = [
  row({ id: "appr:r1", kind: "approval", rank: 1, thread_id: CHAT, detail: { tool: "Bash", command: "ls", req_id: "r1" } }),
  row({ id: `q:${CHAT}:q-1`, kind: "question", rank: 2, thread_id: CHAT, detail: { text: "Which?", options: ["npm"] } }),
  row({ id: "dec:d0123456789ab", kind: "decision", rank: 6, thread_id: CHAT,
    detail: { decision_id: "d0123456789ab", question: "Which?", options: ["a", "b"], default: 0, why: "w", answer: null, delivery: null } }),
  row({ id: `prop:${TASK}`, kind: "proposal", rank: 3, task_id: TASK }),
];

function harness(rows: unknown[] = []) {
  const posts: { path: string; value: unknown }[] = [];
  const replies: Posted[] = [];
  const asked: string[] = [];
  const gets: Record<string, Got> = {};
  const ok = (body: unknown): Got => ({ ok: true, body });
  const mic = fakeEnv();
  const signedOut = vi.fn();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = ok({ rows });
  gets["/m/api/me"] = ok({ signed_in: true, secure: { ok: true } });
  gets["/m/api/projects"] = ok({ projects: [{ id: PROJECT, name: "Calc", inbox: false }] });
  gets["/m/api/threads"] = ok({ threads: [
    { id: CHAT, project_id: PROJECT, title: "Fix it", provider: "claude", updated: "2026-10-25T10:00:00Z" },
    { id: OTHER, project_id: PROJECT, task_id: TASK, title: "a task's", provider: "codex", updated: "2026-10-25T09:00:00Z" }] });
  gets[`/m/api/threads/${CHAT}/log`] = ok({ rows: [{ seq: 0, role: "owner", text: "hi", at: null, marks: [] }], next: 1,
    more: false, older: false, thread: { id: CHAT, project_id: PROJECT, role: "chat", title: "Fix it", provider: "claude" } });
  gets[`/m/api/threads/${OTHER}/log`] = ok({ rows: [], next: 0, more: false, older: false,
    thread: { id: OTHER, project_id: PROJECT, role: "orchestrator", task_id: TASK, title: "a task's", provider: "codex" } });
  gets[`/m/api/threads/${CHAT}/log?after=1`] = ok({ rows: [], next: 1, more: false, older: false, thread: null });
  const app = new PhoneApp({
    root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async (path) => { asked.push(path); return gets[path] ?? { ok: false, why: "error", status: 404, body: {} }; },
    postJson: async (path, value) => {
      posts.push({ path, value });
      return (replies.shift() ?? { status: 202, body: { status: "started" } }) as Posted;
    },
    getCredential: async () => null,
    openStream: () => new FakeStream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
    talkEnv: mic.env,
  });
  const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  const button = () => q("m-mic") as HTMLButtonElement;
  const box = () => q("m-compose-text") as HTMLTextAreaElement;
  const line = () => q("m-talk-line")!;
  const rect = (b: HTMLElement) => {
    b.getBoundingClientRect = () => ({ left: 0, top: 500, right: 300, bottom: 556, width: 300, height: 56, x: 0, y: 500,
      toJSON() {} }) as DOMRect;
  };
  const pointer = (type: string, target: EventTarget, x = 150, y = 528) => {
    const e = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
    target.dispatchEvent(e);
  };
  const down = async () => { rect(button()); pointer("pointerdown", button()); await settle(); };
  const up = async () => { pointer("pointerup", window); await settle(); };
  return { app, root, posts, replies, asked, gets, mic, signedOut, settle, q, button, box, line, pointer, down, up, ok };
}

async function openChat(h: ReturnType<typeof harness>, id = CHAT) {
  h.app.start();
  await h.settle();
  h.app.show("chats");
  await h.settle();
  await h.app.openThread(id);
  await h.settle();
}

function bytesOf(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

function wavInfo(bytes: ArrayBuffer) {
  const v = new DataView(bytes);
  const ascii = (o: number, n: number) => String.fromCharCode(...new Uint8Array(bytes, o, n));
  return { riff: ascii(0, 4), wave: ascii(8, 4), format: v.getUint16(20, true), channels: v.getUint16(22, true),
    rate: v.getUint32(24, true), bits: v.getUint16(34, true), samples: v.getUint32(40, true) / 2, size: bytes.byteLength };
}

describe("push to talk (P-7)", () => {
  it("asks for the microphone only on the press; the words land in the box, unsent", async () => {
    const h = harness();
    await openChat(h);
    // Nothing touched the microphone to draw the app and the chat.
    expect(h.mic.getUserMedia).not.toHaveBeenCalled();
    expect(h.mic.contexts.length).toBe(0);
    expect(h.button().textContent).toBe(WORDS.idle);
    expect(h.button().getBoundingClientRect).toBeDefined();
    h.box().value = "please";
    h.box().dispatchEvent(new Event("input"));
    await h.down();
    expect(h.mic.getUserMedia).toHaveBeenCalledTimes(1);
    expect(h.mic.getUserMedia.mock.calls[0][0]).toMatchObject({ audio: { channelCount: 1 } });
    expect(h.mic.ctx().resumed).toBe(1);
    expect(h.button().textContent).toBe(WORDS.listening);
    expect(h.button().dataset.phase).toBe("listening");
    expect(h.line().textContent).toBe(WORDS.listeningLine);
    h.mic.ctx().feed(1.0);
    let finish!: (r: Uploaded) => void;
    h.mic.reply(() => new Promise<Uploaded>((r) => { finish = r; }));
    await h.up();
    // Transcribing: the mic is already released, the button waits.
    expect(h.button().textContent).toBe(WORDS.transcribing);
    expect(h.button().disabled).toBe(true);
    expect(h.line().textContent).toBe(WORDS.transcribingLine);
    expect(h.mic.streams[0].tracks.every((t) => t.stopped === 1)).toBe(true);
    expect(h.mic.ctx().closed).toBe(1);
    expect(h.mic.ctx().processor!.onaudioprocess).toBeNull();
    expect(h.mic.uploads.length).toBe(1);
    const clip = h.mic.uploads[0];
    expect(clip.type).toBe("audio/wav");
    const info = wavInfo(await bytesOf(clip));
    expect(info).toMatchObject({ riff: "RIFF", wave: "WAVE", format: 1, channels: 1, rate: 16000, bits: 16 });
    expect(Math.abs(info.samples - RATE)).toBeLessThanOrEqual(1024);
    finish({ status: 200, body: { text: "add a test" } });
    await h.settle();
    expect(h.box().value).toBe("please add a test");
    expect(h.app.composeDrafts.get(CHAT)).toBe("please add a test");
    expect(h.line().textContent).toBe(WORDS.added);
    expect(h.button().textContent).toBe(WORDS.idle);
    // Nothing was sent, answered or posted: Send is still the owner's tap.
    expect(h.posts).toEqual([]);
    expect((h.q("m-compose-send") as HTMLButtonElement).disabled).toBe(false);
    (h.q("m-compose-send") as HTMLButtonElement).click();
    await h.settle();
    expect(h.posts.length).toBe(1);
    expect(h.posts[0]).toMatchObject({ path: `/m/api/threads/${CHAT}/messages`, value: { text: "please add a test" } });
  });

  it("words added after a send that may not have arrived make a new message (a new client_id)", async () => {
    const h = harness();
    await openChat(h);
    h.box().value = "please";
    h.box().dispatchEvent(new Event("input"));
    h.replies.push({ status: 502, body: {} });
    (h.q("m-compose-send") as HTMLButtonElement).click();
    await h.settle();
    expect(h.posts.length).toBe(1);
    const first = (h.posts[0].value as { client_id: string }).client_id;
    expect(first).toBeTruthy();
    // The ambiguous answer turned the app offline (P-5): the mic is off with it, until a read gets through.
    expect(h.button().disabled).toBe(true);
    h.app.setOnline(true);
    await h.settle();
    await h.down();
    h.mic.ctx().feed(0.5);
    await h.up();
    expect(h.box().value).toBe("please add a test");
    expect(h.posts.length).toBe(1);
    (h.q("m-compose-send") as HTMLButtonElement).click();
    await h.settle();
    expect(h.posts.length).toBe(2);
    expect(h.posts[1].value).toMatchObject({ text: "please add a test" });
    expect((h.posts[1].value as { client_id: string }).client_id).not.toBe(first);
  });

  it("an empty box takes the words as they are", async () => {
    const h = harness();
    await openChat(h);
    await h.down();
    h.mic.ctx().feed(0.5);
    await h.up();
    expect(h.box().value).toBe("add a test");
    expect(h.posts).toEqual([]);
  });

  it("sliding off the button, or a cancelled pointer, sends nothing and releases the mic", async () => {
    for (const how of ["slide", "cancel", "hidden"] as const) {
      const h = harness();
      await openChat(h);
      await h.down();
      h.mic.ctx().feed(1.0);
      if (how === "slide") h.pointer("pointermove", window, 150, 300);
      else if (how === "cancel") h.pointer("pointercancel", window);
      else h.app.talk.hidden();
      await h.settle();
      expect(h.line().textContent).toBe(WORDS.cancelled);
      await h.up();
      expect(h.mic.uploads).toEqual([]);
      expect(h.mic.streams[0].tracks.every((t) => t.stopped === 1)).toBe(true);
      expect(h.mic.ctx().closed).toBe(1);
      expect(h.box().value).toBe("");
    }
  });

  it("a slide within the button's slop still records", async () => {
    const h = harness();
    await openChat(h);
    await h.down();
    h.pointer("pointermove", window, 310, 540);
    h.mic.ctx().feed(0.5);
    await h.up();
    expect(h.mic.uploads.length).toBe(1);
  });

  it("a finger lifted before the permission came back records nothing, and the late stream is stopped", async () => {
    const h = harness();
    await openChat(h);
    const open = h.mic.hold();
    await h.down();
    expect(h.button().textContent).toBe(WORDS.starting);
    await h.up();
    expect(h.line().textContent).toBe(WORDS.early);
    open();
    await h.settle();
    expect(h.mic.streams.length).toBe(1);
    expect(h.mic.streams[0].tracks.every((t) => t.stopped === 1)).toBe(true);
    expect(h.mic.ctx().closed).toBe(1);
    expect(h.mic.uploads).toEqual([]);
    expect(h.button().textContent).toBe(WORDS.idle);
  });

  it("says when the microphone is refused, missing, or there is no Web Audio", async () => {
    const h = harness();
    await openChat(h);
    h.mic.refuse(Object.assign(new Error("no"), { name: "NotAllowedError" }));
    await h.down();
    expect(h.line().textContent).toBe(WORDS.denied);
    expect(h.mic.ctx().closed).toBe(1);
    await h.up();
    expect(h.mic.uploads).toEqual([]);
    h.mic.refuse(Object.assign(new Error("no"), { name: "NotFoundError" }));
    await h.down();
    expect(h.line().textContent).toBe(WORDS.noMic);
    await h.up();
    const env = { ...h.mic.env, media: () => null };
    const talk = new Talk(env, { insert: () => {}, unauthorized: () => {}, online: () => true });
    talk.view(CHAT);
    talk.press();
    expect(talk.note).toBe(WORDS.noRecording);
  });

  it("too short a hold is not sent", async () => {
    const h = harness();
    await openChat(h);
    await h.down();
    h.mic.ctx().feed(0.1);
    await h.up();
    expect(h.mic.uploads).toEqual([]);
    expect(h.line().textContent).toBe(WORDS.short);
    expect(h.mic.streams[0].tracks.every((t) => t.stopped === 1)).toBe(true);
  });

  it("stops itself at one minute and transcribes exactly a minute", async () => {
    const h = harness();
    await openChat(h);
    await h.down();
    expect(h.mic.timers.some((t) => t.ms === 60_000)).toBe(true);
    h.mic.ctx().feed(61);
    await h.settle();
    expect(h.mic.uploads.length).toBe(1);
    const info = wavInfo(await bytesOf(h.mic.uploads[0]));
    expect(info.samples).toBe(MAX_SAMPLES);
    expect(info.size).toBe(44 + MAX_SAMPLES * 2);
    expect(h.mic.streams[0].tracks.every((t) => t.stopped === 1)).toBe(true);
    expect(h.mic.timers.find((t) => t.ms === 60_000)!.cleared).toBe(true);
    expect(h.box().value).toBe("add a test");
    expect(h.line().textContent).toBe(WORDS.limit + " " + WORDS.added);
    await h.up();
    expect(h.mic.uploads.length).toBe(1);
  });

  it("offline: the button is off and nothing is asked or sent", async () => {
    const h = harness();
    await openChat(h);
    h.app.setOnline(false);
    await h.settle();
    expect(h.button().disabled).toBe(true);
    await h.down();
    await h.up();
    expect(h.mic.getUserMedia).not.toHaveBeenCalled();
    expect(h.mic.uploads).toEqual([]);
  });

  it("failures are said plainly: offline, unavailable, refused, nothing heard, signed out", async () => {
    const cases: [Uploaded, string][] = [
      [{ status: 0, body: {}, offline: true }, WORDS.offline],
      [{ status: 502, body: {} }, WORDS.offline],
      [{ status: 409, body: { error: "cloud speech-to-text is switched off in Settings > Voice" } },
        "Speech-to-text is unavailable: cloud speech-to-text is switched off in Settings > Voice."],
      [{ status: 502, body: { error: "speech-to-text unavailable: m (HTTP 500)" } },
        "Speech-to-text is unavailable: speech-to-text unavailable: m (HTTP 500)."],
      [{ status: 503, body: { error: "speaker verification is not set up: x" } },
        "Speech-to-text is unavailable: speaker verification is not set up: x."],
      [{ status: 422, body: { error: "not the enrolled speaker" } }, "Not the enrolled speaker."],
      [{ status: 429, body: { error: "too many clips from this phone; wait a minute" } }, "Too many clips from this phone; wait a minute."],
      [{ status: 200, body: { text: "  " } }, WORDS.nothing],
    ];
    for (const [reply, said] of cases) {
      const h = harness();
      await openChat(h);
      h.mic.reply(reply);
      await h.down();
      h.mic.ctx().feed(0.5);
      await h.up();
      expect(h.line().textContent).toBe(said);
      expect(h.box().value).toBe("");
      expect(h.posts).toEqual([]);
    }
    expect(uploadNote({ status: 503, body: {} })).toBe(WORDS.offline);
    const h = harness();
    await openChat(h);
    h.gets["/m/api/me"] = { ok: false, why: "signed-out", status: 401 };
    h.mic.reply({ status: 401, body: { error: "unauthorized" } });
    await h.down();
    h.mic.ctx().feed(0.5);
    await h.up();
    await h.settle();
    expect(h.signedOut).toHaveBeenCalled();
  });

  it("a voice note never answers: no mic on an inbox row, a card, a question, a decision or a task's chat", async () => {
    const h = harness(ROWS);
    h.app.start();
    await h.settle();
    expect(h.root.querySelector('[data-testid="m-answer-text"]')).not.toBeNull();
    expect(h.root.querySelector('[data-testid="m-decision-send"]')).not.toBeNull();
    expect(h.root.querySelectorAll('[data-testid="m-mic"], [data-testid="m-talk"]').length).toBe(0);
    const card = cardView({ rowId: "appr:r1", row: null, refused: null, busy: false, rule: null, notice: "",
      detail: parseDetail({ card: { req_id: "r1", tool: "Bash", args: { command: "ls" }, command: "ls", reason: "",
        layer: "", origin: "", headline: "", asked_at: null, timeout_s: 600, allowlistable: true, provider: "claude",
        thread_id: CHAT }, private: false, withheld: false, secure: { ok: true }, can: { deny: true, allow: true, always: true },
      why: "" }) }, { back() {}, deny() {}, allow() {}, always() {}, confirmAlways() {}, cancelAlways() {} });
    expect(card.querySelectorAll('[data-testid="m-mic"]').length).toBe(0);
    // A task's chat has no message box, so no mic either.
    h.app.show("chats");
    await h.settle();
    await h.app.openThread(OTHER);
    await h.settle();
    expect(h.root.querySelector('[data-testid="m-compose"]')).toBeNull();
    expect(h.root.querySelector('[data-testid="m-mic"]')).toBeNull();
    // An owner's chat has exactly one.
    await h.app.openThread(CHAT);
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-mic"]').length).toBe(1);
    expect(h.root.querySelector('[data-testid="m-compose"] [data-testid="m-mic"]')).not.toBeNull();
  });

  it("the transcribed words reach only the box: an 'allow' spoken is typed, never an answer", async () => {
    const h = harness(ROWS);
    await openChat(h);
    h.mic.reply({ status: 200, body: { text: "allow" } });
    await h.down();
    h.mic.ctx().feed(0.5);
    await h.up();
    expect(h.box().value).toBe("allow");
    expect(h.posts).toEqual([]);
    // The recorder's own source names one request, the clip upload, and nothing that answers or sends.
    expect(STT_URL).toBe("/m/api/stt");
    const paths = talkSource.match(/["'`]\/m\/api\/[^"'`]*["'`]/g) ?? [];
    expect(paths).toEqual(['"/m/api/stt"']);
    expect(talkSource).not.toMatch(/inbox\/|\/answer|\/messages|\/approvals|postJson/);
  });

  it("a redraw during a press keeps the same button, and the release still lands", async () => {
    const h = harness();
    await openChat(h);
    const before = h.button();
    await h.down();
    h.mic.ctx().feed(0.5);
    h.app.render();
    expect(h.button()).toBe(before);
    expect(h.button().dataset.phase).toBe("listening");
    await h.up();
    expect(h.mic.uploads.length).toBe(1);
    expect(h.box().value).toBe("add a test");
  });

  it("a second press while a clip is in flight is ignored; leaving the app releases the mic", async () => {
    const h = harness();
    await openChat(h);
    let finish!: (r: Uploaded) => void;
    h.mic.reply(() => new Promise<Uploaded>((r) => { finish = r; }));
    await h.down();
    h.mic.ctx().feed(0.5);
    await h.up();
    await h.down();
    expect(h.mic.getUserMedia).toHaveBeenCalledTimes(1);
    finish({ status: 200, body: { text: "one" } });
    await h.settle();
    await h.down();
    expect(h.mic.getUserMedia).toHaveBeenCalledTimes(2);
    h.app.stop();
    expect(h.mic.streams[1].tracks.every((t) => t.stopped === 1)).toBe(true);
    expect(h.mic.ctx().closed).toBe(1);
  });

  it("the button is held, at least 44 px, and cannot scroll or select while held (talk.css)", () => {
    const css = readFileSync(join(__dirname, "talk.css"), "utf8");
    const rule = css.slice(css.indexOf(".tap.m-mic {"), css.indexOf("}", css.indexOf(".tap.m-mic {")));
    expect(Number(/min-height:\s*(\d+)px/.exec(rule)![1])).toBeGreaterThanOrEqual(44);
    expect(rule).toContain("touch-action: none");
    expect(rule).toContain("-webkit-user-select: none");
    expect(rule).toContain("-webkit-touch-callout: none");
    expect(css).not.toMatch(/url\(/);
    // Listening is the accent (glacier cyan), never amber or red.
    expect(css).not.toMatch(/--amber|--red/);
    const talk = new Talk(fakeEnv().env, { insert: () => {}, unauthorized: () => {}, online: () => true });
    const button = talk.view(CHAT).querySelector("button")!;
    expect(button.type).toBe("button");
    expect(button.className).toContain("m-mic");
  });
});
