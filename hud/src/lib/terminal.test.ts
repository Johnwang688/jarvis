import { describe, expect, it } from "vitest";
import {
  AttachLedger, CHUNK, InputGate, InputQueue, MINE_KEPT, MINE_KEY, OUTPUT_HIGH_WATER, OutputPipe, attachUrl, chunk,
  clampSize, cleanPaste, cleanText, cleanTitle, counterZoom, encodeInput, fontSizeFor, inTerminal, inheritMine,
  integrationNote, isCtrlC, judgeLink, loadMine, pageNonce, parseControl, parseMine, parsePrefs, parseRow, parseRows,
  parseSpec, placeTerminals, saveMine, terminalSpecFor, terminalTakesKey,
} from "./terminal";

const row = (over: Record<string, unknown> = {}) => ({
  id: "0a1b2c3d", title: "bash · Calc", folder: "/home/o/calc", project_id: "p1", created: "2026-10-09T00:00:00",
  cols: 80, rows: 24, shown: false, exited: false, exit_code: null, readable: true, busy: false,
  integration: "bash", integrated: false, marked: false, ...over,
});

describe("the socket's URL", () => {
  it("comes from the window's own location, never a fixed port", () => {
    expect(attachUrl({ protocol: "http:", host: "127.0.0.1:8474" }, "0a1b2c3d", "tk"))
      .toBe("ws://127.0.0.1:8474/terminals/0a1b2c3d/attach?ticket=tk");
    expect(attachUrl({ protocol: "http:", host: "localhost:9999" }, "0a1b2c3d", "tk"))
      .toBe("ws://localhost:9999/terminals/0a1b2c3d/attach?ticket=tk");
    expect(attachUrl({ protocol: "https:", host: "127.0.0.1:1" }, "0a1b2c3d", "tk")).toMatch(/^wss:\/\/127\.0\.0\.1:1\//);
  });

  it("encodes the ticket and the id", () => {
    const u = attachUrl({ protocol: "http:", host: "h:1" }, "0a1b2c3d", "a+b/c=&x");
    expect(u).toBe("ws://h:1/terminals/0a1b2c3d/attach?ticket=a%2Bb%2Fc%3D%26x");
    expect(new URL(u).searchParams.get("ticket")).toBe("a+b/c=&x");
  });
});

describe("pastes", () => {
  it("are chunked at 16 KiB, in order, losslessly", () => {
    const data = new Uint8Array(CHUNK * 3 + 5).map((_, i) => i % 251);
    const parts = chunk(data);
    expect(parts.map((p) => p.length)).toEqual([CHUNK, CHUNK, CHUNK, 5]);
    expect(Array.from(new Uint8Array(parts.flatMap((p) => Array.from(p))))).toEqual(Array.from(data));
    expect(chunk(new Uint8Array(0))).toEqual([]);
    expect(CHUNK).toBe(16 * 1024);
  });

  it("are bytes: a multi-byte character costs its UTF-8 length", () => {
    expect(encodeInput("é€").length).toBe(5);
    expect(isCtrlC(encodeInput("\x03"))).toBe(true);
    expect(isCtrlC(encodeInput("\x03\x03"))).toBe(false);
    expect(isCtrlC(encodeInput("c"))).toBe(false);
  });

  it("queue per socket, and a closed queue takes nothing more", () => {
    const q = new InputQueue();
    q.push(new Uint8Array(CHUNK + 10));
    expect(q.pending).toBe(CHUNK + 10);
    expect(q.next()!.length).toBe(CHUNK);
    expect(q.close()).toBe(10);
    expect(q.push(new Uint8Array(3))).toBe(false);
    expect(q.next()).toBeNull();
  });
});

describe("the input gate", () => {
  const paste = (n: number) => new Uint8Array(n).fill(0x61);

  it("stops the rest of a paste the moment the daemon drops a frame", () => {
    const g = new InputGate();
    g.open();
    expect(g.input(paste(CHUNK * 10))).toEqual({ send: null, held: false, dropped: 0 });
    expect(g.next()!.length).toBe(CHUNK);
    expect(g.next()!.length).toBe(CHUNK);
    expect(g.dropped()).toBe(CHUNK * 8);
    expect(g.latched).toBe(true);
    expect(g.next()).toBeNull();
    expect(g.pending).toBe(0);
  });

  it("holds typing while latched, until the owner resumes", () => {
    const g = new InputGate();
    g.open();
    g.dropped();
    expect(g.input(encodeInput("y\r"))).toEqual({ send: null, held: true, dropped: 0 });
    expect(g.next()).toBeNull();
    g.resumed();
    expect(g.input(encodeInput("y\r")).held).toBe(false);
    expect(Array.from(g.next()!)).toEqual([0x79, 0x0d]);
  });

  it("lets a lone Ctrl-C through even latched, and a Ctrl-C ends a waiting paste", () => {
    const g = new InputGate();
    g.open();
    g.input(paste(CHUNK * 4));
    g.next();
    const c = g.input(encodeInput("\x03"));
    expect(c.send && Array.from(c.send)).toEqual([3]);
    expect(c.dropped).toBe(CHUNK * 3);
    expect(g.next()).toBeNull();
    g.dropped();
    expect(g.input(encodeInput("\x03")).send).not.toBeNull();
    expect(g.input(encodeInput("\x03x")).held).toBe(true);
  });

  it("never continues a paste on a new socket", () => {
    const g = new InputGate();
    g.open();
    g.input(paste(CHUNK * 6));
    g.next();
    expect(g.close()).toBe(CHUNK * 5);
    expect(g.input(paste(5)).held).toBe(true);           // no socket: nothing queued
    g.open();
    expect(g.next()).toBeNull();                         // nothing of the old paste
    expect(g.latched).toBe(false);                       // a new socket starts unlatched
    g.input(paste(CHUNK * 2));
    g.next();
    expect(g.open()).toBe(CHUNK);                        // a reattach drops the old queue too
    expect(g.next()).toBeNull();
  });

  it("throws a paste away when the window stops drawing, typing not latched (review of PR #32)", () => {
    const g = new InputGate();
    g.open();
    g.input(paste(CHUNK * 6));
    g.next();
    expect(g.abort()).toBe(CHUNK * 5);
    expect(g.next()).toBeNull();
    expect(g.pending).toBe(0);
    expect(g.latched).toBe(false);
    expect(g.connected).toBe(true);
  });
});

describe("control messages", () => {
  it("are read field by field", () => {
    expect(parseControl(JSON.stringify({ type: "attached", terminal: row(), replay: 12 })))
      .toEqual({ type: "attached", terminal: parseRow(row()), replay: 12 });
    expect(parseControl('{"type":"replayed"}')).toEqual({ type: "replayed" });
    expect(parseControl('{"type":"exit","code":3}')).toEqual({ type: "exit", code: 3, reason: "" });
    expect(parseControl('{"type":"exit","code":null,"reason":"ended"}')).toEqual({ type: "exit", code: null, reason: "ended" });
    expect(parseControl('{"type":"exit","code":0,"reason":"closed"}')).toEqual({ type: "exit", code: 0, reason: "closed" });
    expect(parseControl('{"type":"takeover_request","id":"ab12","timeout_s":20}'))
      .toEqual({ type: "takeover_request", id: "ab12", timeout_s: 20 });
    expect(parseControl('{"type":"waiting","timeout_s":20}')).toEqual({ type: "waiting", timeout_s: 20 });
    expect(parseControl('{"type":"taken"}')).toEqual({ type: "taken" });
    expect(parseControl('{"type":"refused","reason":"kept"}')).toEqual({ type: "refused", reason: "kept" });
    expect(parseControl('{"type":"input_dropped","bytes":16384,"latched":true,"reason":"r"}'))
      .toEqual({ type: "input_dropped", bytes: 16384, latched: true, reason: "r" });
    expect(parseControl('{"type":"input_resumed"}')).toEqual({ type: "input_resumed" });
    expect(parseControl('{"type":"input_resume_refused","reason":"r"}')).toEqual({ type: "input_resume_refused", reason: "r" });
    expect(parseControl('{"type":"marked"}')).toEqual({ type: "marked" });
  });

  it("and anything malformed or unknown is ignored", () => {
    for (const bad of ["", "nope", "[]", "null", '"x"', '{"type":"rm -rf"}', '{"type":"takeover_request"}',
                       '{"type":"takeover_request","id":7}', "{}"]) {
      expect(parseControl(bad)).toBeNull();
    }
    expect(parseControl('{"type":"exit","code":"3","reason":"whatever"}')).toEqual({ type: "exit", code: null, reason: "" });
    expect(parseControl('{"type":"attached","terminal":{"id":"../x"},"replay":-5}'))
      .toEqual({ type: "attached", terminal: null, replay: 0 });
    expect(parseControl('{"type":"waiting","timeout_s":1e9}')).toEqual({ type: "waiting", timeout_s: 20 });
  });

  it("text a server sends is one clean line", () => {
    const r = parseControl(JSON.stringify({ type: "refused", reason: "a\x1b]0;evil\x07b\nc‮Z" + "x".repeat(400) }));
    expect(r && r.type === "refused" && r.reason).toMatch(/^a ]0;evil b c Z/);
    expect(r && r.type === "refused" && r.reason.length).toBeLessThanOrEqual(200);
  });
});

describe("rows", () => {
  it("keep only well-formed terminals, once each", () => {
    const rows = parseRows([row(), row(), row({ id: "nothex!!" }), "x", null, row({ id: "11112222", readable: false })]);
    expect(rows.map((r) => r.id)).toEqual(["0a1b2c3d", "11112222"]);
    expect(rows[1].readable).toBe(false);
    expect(parseRows({})).toEqual([]);
  });

  it("read the switch as on unless the daemon says off, and integration as none unless known", () => {
    expect(parseRow(row({ readable: undefined }))!.readable).toBe(true);
    expect(parseRow(row({ integration: "zsh" }))!.integration).toBe("none");
    expect(parseRow(row({ marked: "yes" }))!.marked).toBe(false);
  });
});

describe("links", () => {
  it("open only for http and https", () => {
    expect(judgeLink("http://localhost:5173/")).toEqual({ ok: true, url: "http://localhost:5173/", loopback: true });
    expect(judgeLink("https://example.com/a?b")).toEqual({ ok: true, url: "https://example.com/a?b", loopback: false });
    expect(judgeLink("http://127.0.0.1:8000").loopback).toBe(true);
    expect(judgeLink("http://[::1]:3000/").loopback).toBe(true);
    expect(judgeLink("http://app.localhost/").loopback).toBe(true);
  });

  it("and every other scheme is inert", () => {
    for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "data:text/html,<b>x",
                       "file:///etc/passwd", "vbscript:x", "ftp://x/", "mailto:a@b", "blob:http://x/1", "",
                       "not a url", "http://user:pass@example.com/"]) {
      expect(judgeLink(bad).ok).toBe(false);
    }
  });
});

describe("titles a program sets", () => {
  it("are text, one line, capped", () => {
    expect(cleanTitle("vim — notes.md")).toBe("vim — notes.md");
    expect(cleanTitle("a\x1b[31mb\x07c\r\nd")).toBe("a [31mb c d");
    expect(cleanTitle("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>");
    expect(cleanTitle("t".repeat(500)).length).toBe(80);
    expect(cleanTitle("‮evil")).toBe("evil");
    expect(cleanText("  a   b  ", 10)).toBe("a b");
  });
});

describe("keys", () => {
  it("Ctrl+B, Ctrl+Alt+B and Ctrl+_ are the shell's in a terminal; the rest stay the HUD's", () => {
    expect(terminalTakesKey("toggleLeft", "b")).toBe(true);
    expect(terminalTakesKey("toggleRight", "b")).toBe(true);
    expect(terminalTakesKey("zoomOut", "_")).toBe(true);
    expect(terminalTakesKey("zoomOut", "-")).toBe(false);
    expect(terminalTakesKey("zoomIn", "=")).toBe(false);
    expect(terminalTakesKey("zoomReset", "0")).toBe(false);
    expect(terminalTakesKey("togglePanel", "`")).toBe(false);
    expect(terminalTakesKey("focus2", "2")).toBe(false);
    expect(terminalTakesKey(null, "x")).toBe(false);
  });

  it("a terminal is recognised by its slot or xterm's own element", () => {
    document.body.innerHTML = '<div class="termslot"><div class="xterm"><textarea id="t"></textarea></div></div>'
      + '<div class="other"><input id="i"></div>';
    expect(inTerminal(document.getElementById("t"))).toBe(true);
    expect(inTerminal(document.getElementById("i"))).toBe(false);
    expect(inTerminal(null)).toBe(false);
  });
});

describe("zoom", () => {
  it("counter-zooms the host and scales the font instead", () => {
    expect(fontSizeFor(100)).toBe(13);
    expect(fontSizeFor(160)).toBe(21);
    expect(fontSizeFor(70)).toBe(9);
    expect(counterZoom(160)).toBeCloseTo(0.625);
    expect(counterZoom(70) * 0.7).toBeCloseTo(1);
    expect(fontSizeFor(NaN)).toBe(13);
  });

  it("sizes the daemon accepts", () => {
    expect(clampSize(80.7, 24.2)).toEqual({ cols: 80, rows: 24 });
    expect(clampSize(5000, 9000)).toEqual({ cols: 1000, rows: 500 });
    expect(clampSize(1, 1)).toEqual({ cols: 2, rows: 1 });
    expect(clampSize(0, 10)).toBeNull();
    expect(clampSize(NaN, 10)).toBeNull();
  });
});

describe("where a terminal is drawn", () => {
  const panes = [
    { view: "chat", terminalId: null }, { view: "terminal", terminalId: "aaaa0001" },
    { view: "terminal", terminalId: "aaaa0001" }, { view: "terminal", terminalId: "bbbb0002" },
  ];

  it("in one place at a time: the first drawn pane holding it", () => {
    expect([...placeTerminals(panes, [1, 2, 3, 4])]).toEqual([["aaaa0001", 2], ["bbbb0002", 4]]);
    expect([...placeTerminals(panes, [3, 1])]).toEqual([["aaaa0001", 3]]);
  });

  it("a pane the window does not draw holds nothing: its terminal is the panel's", () => {
    expect([...placeTerminals(panes, [1])]).toEqual([]);
    expect([...placeTerminals([{ view: "file", terminalId: "aaaa0001" }], [1])]).toEqual([]);
  });
});

describe("whose attach it was", () => {
  it("matches this window's own sockets, once each", () => {
    const l = new AttachLedger(40_000);
    l.mine("aaaa0001", 0);
    expect(l.heard("aaaa0001", 100)).toBe(true);
    expect(l.heard("aaaa0001", 200)).toBe(false);         // a second attach is not ours
    expect(l.heard("bbbb0002", 200)).toBe(false);
  });

  it("forgets a note that never attached", () => {
    const l = new AttachLedger(40_000);
    l.mine("aaaa0001", 0);
    expect(l.heard("aaaa0001", 50_000)).toBe(false);      // expired
    l.mine("aaaa0001", 60_000);
    l.failed("aaaa0001");
    expect(l.heard("aaaa0001", 60_100)).toBe(false);
    expect(l.size).toBe(0);
  });
});

describe("integration", () => {
  it("says when a shell has no startup file, and when one never ran", () => {
    expect(integrationNote({ integration: "none", marked: false }, false)!.kind).toBe("none");
    expect(integrationNote({ integration: "bash", marked: false }, false)).toBeNull();     // not yet
    expect(integrationNote({ integration: "bash", marked: false }, true)!.kind).toBe("inactive");
    expect(integrationNote({ integration: "posix", marked: true }, true)).toBeNull();
    expect(integrationNote(null, true)).toBeNull();
  });
});

describe("where + opens a terminal", () => {
  const at = { thread: "t1", compose: null, project: "p2", task: "k1" };
  it("follows what the focused pane shows, as ids", () => {
    expect(terminalSpecFor("chat", at)).toEqual({ thread: "t1" });
    expect(terminalSpecFor("chat", { ...at, thread: null, compose: "p3" })).toEqual({ project: "p3" });
    expect(terminalSpecFor("file", at)).toEqual({ project: "p2" });
    expect(terminalSpecFor("preview", at)).toEqual({ project: "p2" });
    expect(terminalSpecFor("task", at)).toEqual({ task: "k1" });
    expect(terminalSpecFor("diff", at)).toEqual({ task: "k1" });
    expect(terminalSpecFor("terminal", at)).toEqual({ thread: "t1" });
  });
  it("and falls back to the chat, then home", () => {
    expect(terminalSpecFor("task", { ...at, task: null })).toEqual({ thread: "t1" });
    expect(terminalSpecFor("file", { thread: null, compose: null, project: null, task: null })).toBe("home");
  });
});

describe("stored preferences", () => {
  it("parse each field on its own, and specs from ids only", () => {
    expect(parsePrefs("garbage")).toEqual({ active: null, specs: {} });
    expect(parsePrefs(JSON.stringify({ active: "aaaa0001", specs: { aaaa0001: { project: "p1" }, bad: "home",
                                                                       bbbb0002: { path: "/etc" } } })))
      .toEqual({ active: "aaaa0001", specs: { aaaa0001: { project: "p1" } } });
    expect(parseSpec("home")).toBe("home");
    expect(parseSpec({ thread: "t1" })).toEqual({ thread: "t1" });
    expect(parseSpec({ thread: "t1", project: "p1" })).toBeNull();
    expect(parseSpec({ folder: "/" })).toBeNull();
  });
});

const enc = (t: string) => new TextEncoder().encode(t);

/** A writer that parses later, as xterm does: a write is "parsed" when the test steps it. */
function laterParser() {
  const pending: { data: Uint8Array; done: () => void }[] = [];
  const parsed: string[] = [];
  const write = (data: Uint8Array, done: () => void) => {
    pending.push({ data, done });
  };
  const step = () => {
    const w = pending.shift();
    if (!w) return false;
    parsed.push(new TextDecoder().decode(w.data));
    w.done();
    return true;
  };
  const drain = () => {
    while (step());
  };
  return { write, step, drain, parsed, pending };
}

describe("output", () => {
  it("is written in order, one write in xterm's hands at a time", () => {
    const x = laterParser();
    const out = new OutputPipe(x.write);
    const g = out.next();
    out.data(g, enc("a"));
    out.data(g, enc("b"));
    out.data(g, enc("c"));
    expect(x.pending.length).toBe(1);
    x.drain();
    expect(x.parsed).toEqual(["a", "b", "c"]);
  });
  it("never parses an older socket's queued output into a new one", () => {
    const x = laterParser();
    const out = new OutputPipe(x.write);
    const g1 = out.next();
    out.data(g1, enc("old-1"));
    out.data(g1, enc("old-2 \x1b[6n"));
    out.data(g1, enc("old-3"));
    const g2 = out.next();                          // the socket dropped; a new one attached
    out.data(g1, enc("late, from the old socket"));
    out.data(g2, enc("RIS"));
    out.data(g2, enc("replay"));
    x.drain();
    // old-1 was already in xterm's hands; it finishes before anything new.
    expect(x.parsed).toEqual(["old-1", "RIS", "replay"]);
  });
  it("runs a step only once everything written before it has been parsed", () => {
    const x = laterParser();
    const out = new OutputPipe(x.write);
    const g = out.next();
    out.data(g, enc("replay-1"));
    out.data(g, enc("replay-2"));
    let done = false;
    out.then(g, () => {
      done = true;
    });
    expect(done).toBe(false);
    x.step();
    expect(done).toBe(false);
    x.step();
    expect(done).toBe(true);
    const idle = new OutputPipe(x.write);
    let now = false;
    idle.then(idle.next(), () => {
      now = true;
    });
    expect(now).toBe(true);                         // nothing pending: at once
  });
  it("drops an older socket's step, and everything after clear()", () => {
    const x = laterParser();
    const out = new OutputPipe(x.write);
    const g1 = out.next();
    out.data(g1, enc("in-flight"));
    let ran = 0;
    out.then(g1, () => ran++);
    out.next();
    out.then(g1, () => ran++);
    x.drain();
    expect(ran).toBe(0);
    const g = out.gen;
    out.clear();
    out.data(g, enc("after"));
    x.drain();
    expect(x.parsed).toEqual(["in-flight"]);
    expect(out.pending).toBe(0);
  });
});

describe("a paste", () => {
  it("is inert as a control stream: no ESC and no C1, so it cannot end bracketed paste early", () => {
    expect(cleanPaste("echo looks-harmless\x1b[201~echo INJECTED\n")).toBe("echo looks-harmless[201~echo INJECTED\n");
    expect(cleanPaste("\x1b[200~x\u009b201~y\u0090z")).toBe("[200~x201~yz");
    expect(cleanPaste("tab\there\r\nnext\n")).toBe("tab\there\r\nnext\n");
    expect(cleanPaste("Grüße · 日本 · 🚀")).toBe("Grüße · 日本 · 🚀");
    expect(cleanPaste("")).toBe("");
  });
});

describe("this tab's terminals", () => {
  const rec = (ids: unknown, holder: unknown) => JSON.stringify({ ids, holder });
  it("are ids only, once each, capped, with the page that holds them", () => {
    expect(parseMine("garbage")).toEqual({ ids: [], holder: "" });
    expect(parseMine(JSON.stringify({ a: 1 }))).toEqual({ ids: [], holder: "" });
    expect(parseMine(JSON.stringify(["0a1b2c3d"]))).toEqual({ ids: [], holder: "" });   // the old array: nobody's
    expect(parseMine(rec(["0a1b2c3d", "../etc", "0a1b2c3d", 7, "0000000f"], null)))
      .toEqual({ ids: ["0a1b2c3d", "0000000f"], holder: null });
    const ids = Array.from({ length: MINE_KEPT + 5 }, (_, i) => i.toString(16).padStart(8, "0"));
    expect(parseMine(rec(ids, "n1")).ids).toEqual(ids.slice(-MINE_KEPT));
  });
  it("are inherited only by a reload of the page that released them", () => {
    const released = rec(["0a1b2c3d"], null);
    expect(Array.from(inheritMine(released, "reload"))).toEqual(["0a1b2c3d"]);
    // A duplicate copies a list its still-live original holds: never inherited,
    // whatever the navigation is called.
    for (const nav of ["reload", "navigate", "back_forward", ""]) {
      expect(inheritMine(rec(["0a1b2c3d"], "a1b2c3d4e5f60718"), nav).size).toBe(0);
    }
    // A reopened closed tab has a released list, but is a restore, not a reload.
    for (const nav of ["navigate", "back_forward", "prerender", ""]) expect(inheritMine(released, nav).size).toBe(0);
    expect(inheritMine(JSON.stringify(["0a1b2c3d"]), "reload").size).toBe(0);
    expect(inheritMine("garbage", "reload").size).toBe(0);
  });
  it("round-trip through storage under this page's nonce, and are released as it goes", () => {
    const kept = new Map<string, string>();
    const storage = {
      getItem: (k: string) => kept.get(k) ?? null,
      setItem: (k: string, v: string) => void kept.set(k, v),
    };
    const me = pageNonce();
    expect(me).toMatch(/^[0-9a-f]{16}$/);
    expect(pageNonce()).not.toBe(me);
    saveMine(new Set(["0a1b2c3d"]), me, storage);
    expect(parseMine(kept.get(MINE_KEY))).toEqual({ ids: ["0a1b2c3d"], holder: me });
    expect(loadMine(storage, "reload").size).toBe(0);           // a copy taken while this page lives
    saveMine(new Set(["0a1b2c3d"]), null, storage);             // pagehide
    expect(Array.from(loadMine(storage, "reload"))).toEqual(["0a1b2c3d"]);
    expect(loadMine(storage, "navigate").size).toBe(0);
    expect(Array.from(loadMine({ getItem: () => { throw new Error("blocked"); } }, "reload"))).toEqual([]);
  });
});

describe("output that comes faster than it is drawn", () => {
  it("is bounded: past the high-water mark the backlog is dropped and the socket's output ends", () => {
    const x = laterParser();
    const overflows: number[] = [];
    const out = new OutputPipe(x.write, { cap: 100, onOverflow: (n) => overflows.push(n) });
    const g = out.next();
    out.data(g, new Uint8Array(30));                // in xterm's hands
    for (let i = 0; i < 3; i++) out.data(g, new Uint8Array(30));
    expect(out.backlog).toBe(90);
    expect(overflows).toEqual([]);
    out.data(g, new Uint8Array(30));                // 120 > 100
    expect(overflows).toEqual([120]);
    expect(out.backlog).toBe(0);
    expect(out.pending).toBe(0);
    out.data(g, new Uint8Array(30));                // the rest of that socket: never drawn
    expect(out.backlog).toBe(0);
    x.drain();
    expect(x.parsed.length).toBe(1);                // only the write already handed over
    const g2 = out.next();                          // the reattach
    out.data(g2, enc("replay"));
    x.drain();
    expect(x.parsed[1]).toBe("replay");
  });
  it("counts only what is queued: what xterm has parsed is no longer backlog", () => {
    const x = laterParser();
    const out = new OutputPipe(x.write, { cap: 100 });
    const g = out.next();
    for (let i = 0; i < 4; i++) out.data(g, new Uint8Array(30));
    x.drain();
    for (let i = 0; i < 3; i++) out.data(g, new Uint8Array(30));
    expect(out.backlog).toBe(60);
  });
  it("has a default high-water mark of 8 MiB", () => {
    expect(OUTPUT_HIGH_WATER).toBe(8 * 1024 * 1024);
  });
});

describe("a write that throws", () => {
  it("never leaves the pipe waiting for a callback that will not come", async () => {
    const parsed: string[] = [];
    const pending: (() => void)[] = [];
    const out = new OutputPipe((data, done) => {
      const t = new TextDecoder().decode(data);
      if (t === "boom") throw new Error("xterm refused it");
      parsed.push(t);
      pending.push(done);
    });
    const g = out.next();
    expect(() => out.data(g, enc("boom"))).toThrow("xterm refused it");
    out.data(g, enc("after"));
    expect(parsed).toEqual(["after"]);
    pending.shift()!();
    out.data(g, enc("a"));
    out.data(g, enc("boom"));
    out.data(g, enc("c"));
    expect(() => pending.shift()!()).toThrow("xterm refused it");   // a's callback hands over boom
    await Promise.resolve();
    expect(parsed).toEqual(["after", "a", "c"]);
  });
});
