// Continuing a terminal that ended with Jarvis (the owner's option (a),
// 2026-10-12): the pure rules — which gestures continue and when, what is
// held while the new shell starts, the old text kept (rebuilt from xterm's
// cells, bounded), and the "Jarvis can read" switch carried over, closed when
// unknown. The headless suite (tests/face/hud_v2_terminal_check.py,
// `_continue_checks`) drives the same in a real window.

import { beforeAll, describe, expect, it, vi } from "vitest";
import {
  HELD_MAX, HeldInput, PREFIX_MAX, continuationPrefix, continuesOn, hhmm, mayContinue, readableCarry,
  separatorLine, snapshotText, type SnapBuffer, type SnapCell,
} from "./terminal";
import { defaultWorkspace, renameTerminal, setPaneTerminal, setView } from "./workspace";

const enc = (s: string) => new TextEncoder().encode(s);

describe("which keys continue an ended terminal", () => {
  it("a typing key: printable text, Enter, Backspace, Tab, an arrow", () => {
    for (const k of ["a", "Z", " ", "é", "語", "😀", "\r", "\x7f", "\x08", "\t",
                     "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "\x1bOA", "\x1bOD"]) {
      expect(continuesOn(enc(k)), JSON.stringify(k)).toBe(true);
    }
  });

  it("never a control, Escape, an Alt+key, a function key, nor anything xterm answers on its own", () => {
    for (const k of ["", "\x03", "\x04", "\x1a", "\x0c", "\x1b", "\x1bb", "\x1bOP", "\x1b[15~", "\x1b[1;5A",
                     "\x1b[12;1R", "\x1b[?1;2c", "\x1b]11;rgb:1a1a/1a1a/1919\x1b\\", "\x1bP1$r0m\x1b\\",
                     "\x1b[I", "a\x04", "\u0085"]) {
      expect(continuesOn(enc(k)), JSON.stringify(k)).toBe(false);
    }
    expect(continuesOn(new Uint8Array([0xff, 0xfe]))).toBe(false);          // not UTF-8
  });
});

describe("which terminals continue", () => {
  const ok = { state: "ended", program: false, mine: true, known: true, crashed: false };

  it("one that ended with Jarvis (or is gone with it), shown by this tab, a shell whose folder is known", () => {
    expect(mayContinue(ok)).toBe(true);
  });

  it("never one that exited on its own, is live, lost, already starting, a program, another window's, or after a crash", () => {
    for (const state of ["exited", "attached", "connecting", "lost", "continuing", "idle", "taken", "refused", "closed"]) {
      expect(mayContinue({ ...ok, state }), state).toBe(false);
    }
    expect(mayContinue({ ...ok, program: true })).toBe(false);
    expect(mayContinue({ ...ok, mine: false })).toBe(false);
    expect(mayContinue({ ...ok, crashed: true })).toBe(false);
  });

  it("nor one whose folder is not known (no remembered `in`, no row): a program terminal after a reload", () => {
    expect(mayContinue({ ...ok, known: false })).toBe(false);
  });
});

describe("the switch a continuation is made with", () => {
  it("is the old terminal's", () => {
    expect(readableCarry({ readable: true })).toBe(true);
    expect(readableCarry({ readable: false })).toBe(false);
  });

  it("and off when it is not known: a continuation never widens what Jarvis can read", () => {
    expect(readableCarry(null)).toBe(false);
    expect(readableCarry(undefined)).toBe(false);
    expect(readableCarry({} as { readable: boolean })).toBe(false);
    expect(readableCarry({ readable: "yes" } as unknown as { readable: boolean })).toBe(false);
  });
});

describe("what is held while the new shell starts", () => {
  it("keys and pastes, in order, the paste cleaned", () => {
    const h = new HeldInput();
    expect(h.key(enc("l"))).toBe(true);
    expect(h.paste("s -la\x1b[201~\u009b\n")).toBe(true);
    expect(h.key(enc("\r"))).toBe(true);
    expect(h.parts).toEqual([{ key: enc("l") }, { paste: "s -la[201~\n" }, { key: enc("\r") }]);
    expect(h.bytes).toBe(1 + 11 + 1);
    expect(h.dropped).toBe(0);
  });

  it("at most 4 KiB: exactly the cap is held, a byte past it is not", () => {
    const h = new HeldInput();
    expect(h.paste("x".repeat(HELD_MAX - 1))).toBe(true);
    expect(h.key(enc("y"))).toBe(true);
    expect(h.bytes).toBe(HELD_MAX);
    expect(h.key(enc("z"))).toBe(false);
    expect(h.dropped).toBe(1);
  });

  it("a paste that does not fit is dropped whole, and counted in UTF-8 bytes", () => {
    const h = new HeldInput();
    expect(h.paste("é".repeat(HELD_MAX / 2 + 1))).toBe(false);           // 2 bytes each
    expect(h.parts).toEqual([]);
    expect(h.dropped).toBe(HELD_MAX + 2);
  });

  it("never spliced: once anything is refused, everything after it is too", () => {
    const h = new HeldInput();
    h.key(enc("r"));
    h.key(enc("m"));
    expect(h.key(enc("\x03"))).toBe(false);                               // a key that does not type
    expect(h.key(enc(" "))).toBe(false);
    expect(h.paste("-rf x\n")).toBe(false);
    expect(h.parts).toEqual([{ key: enc("r") }, { key: enc("m") }]);
    expect(h.closed).toBe(true);
    expect(h.dropped).toBe(1 + 1 + 6);
  });

  it("Ctrl+D is never held: it would end the new shell at once", () => {
    const h = new HeldInput();
    expect(h.key(enc("\x04"))).toBe(false);
    expect(h.parts).toEqual([]);
  });

  it("refused outright (a card came up): this and everything after", () => {
    const h = new HeldInput();
    h.key(enc("a"));
    h.refuse(3);
    expect(h.key(enc("b"))).toBe(false);
    expect(h.parts).toEqual([{ key: enc("a") }]);
    expect(h.dropped).toBe(4);
  });
});

// --- the old text, from cells ------------------------------------------------

interface FakeCell { ch: string; w?: number; fg?: [number, number]; bg?: [number, number]; bold?: boolean; inv?: boolean }
const P16 = 0x1000000;
const P256 = 0x2000000;
const RGB = 0x3000000;

function fakeBuffer(rows: { cells: FakeCell[]; wrapped?: boolean }[], cols = 10): SnapBuffer {
  let current: FakeCell = { ch: "" };
  const cell = {
    getChars: () => current.ch,
    getWidth: () => current.w ?? 1,
    getFgColorMode: () => current.fg?.[0] ?? 0,
    getFgColor: () => current.fg?.[1] ?? 0,
    getBgColorMode: () => current.bg?.[0] ?? 0,
    getBgColor: () => current.bg?.[1] ?? 0,
    isFgDefault: () => !current.fg,
    isFgRGB: () => current.fg?.[0] === RGB,
    isBgDefault: () => !current.bg,
    isBgRGB: () => current.bg?.[0] === RGB,
    isBold: () => (current.bold ? 1 : 0),
    isDim: () => 0, isItalic: () => 0, isUnderline: () => 0, isBlink: () => 0,
    isInverse: () => (current.inv ? 1 : 0),
    isInvisible: () => 0, isStrikethrough: () => 0, isOverline: () => 0,
  } satisfies SnapCell;
  return {
    length: rows.length,
    getNullCell: () => cell,
    getLine: (y) => {
      const r = rows[y];
      if (!r) return undefined;
      return {
        isWrapped: !!r.wrapped,
        length: cols,
        getCell: (x) => {
          current = r.cells[x] ?? { ch: "" };
          return cell;
        },
      };
    },
  };
}

const text = (s: string, extra: Partial<FakeCell> = {}): FakeCell[] => Array.from(s, (ch) => ({ ch, ...extra }));
// Every escape in a prefix is an SGR made of numbers: nothing to answer, no mode.
const onlySgr = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "").indexOf("\x1b") < 0;

describe("the old text kept for a continuation", () => {
  it("is each line's characters, trailing blanks and empty lines dropped", () => {
    const b = fakeBuffer([{ cells: text("$ ls") }, { cells: text("a  b") }, { cells: [] }, { cells: text("   ") }]);
    expect(snapshotText(b)).toBe("$ ls\r\na  b");
  });

  it("joins a wrapped row to the line it continues, so it rewraps at any width", () => {
    const b = fakeBuffer([{ cells: text("0123456789") }, { cells: text("abc"), wrapped: true }, { cells: text("$") }]);
    expect(snapshotText(b)).toBe("0123456789abc\r\n$");
  });

  it("keeps colours and weight as SGR, numbers only, every line ending in the default look", () => {
    const b = fakeBuffer([
      { cells: [...text("ok", { fg: [P16, 2] }), ...text(" "), ...text("!", { fg: [P16, 9], bold: true })] },
      { cells: [...text("x", { fg: [P256, 208] }), ...text("y", { fg: [RGB, 0x102030], bg: [P16, 4] })] },
      { cells: text("plain") },
    ]);
    const s = snapshotText(b);
    expect(s).toBe("\x1b[0;32mok\x1b[0m \x1b[0;1;91m!\x1b[0m\r\n"
      + "\x1b[0;38;5;208mx\x1b[0;38;2;16;32;48;44my\x1b[0m\r\nplain");
    expect(onlySgr(s)).toBe(true);
  });

  it("skips the second half of a wide character, and keeps a coloured background's blanks", () => {
    const b = fakeBuffer([{ cells: [{ ch: "語", w: 2 }, { ch: "", w: 0 }, { ch: "!" }, { ch: " ", bg: [P16, 1] }] }]);
    expect(snapshotText(b)).toBe("語!\x1b[0;41m \x1b[0m");
  });

  it("is bounded: the tail, cut at a line's start, within the limit", () => {
    const rows = Array.from({ length: 50 }, (_, i) => ({ cells: text(`line ${String(i).padStart(2, "0")}`) }));
    const s = snapshotText(fakeBuffer(rows), 7 * 3 + 2 * 2);              // three 7-byte lines and two line ends
    expect(s).toBe("line 47\r\nline 48\r\nline 49");
    expect(new TextEncoder().encode(snapshotText(fakeBuffer(rows), 30)).length).toBeLessThanOrEqual(30);
  });

  it("with the separator: plain text, no escape, the whole at most PREFIX_MAX", () => {
    expect(separatorLine("14:05")).toBe("── restarted 14:05 ──");
    expect(hhmm(new Date(2026, 9, 12, 9, 5))).toBe("09:05");
    const sep = "── restarted 09:05 ──\r\n";
    expect(continuationPrefix(fakeBuffer([{ cells: text("$ ls") }]), "09:05")).toBe("$ ls\r\n" + sep);
    expect(continuationPrefix(fakeBuffer([]), "09:05")).toBe(sep);
    const big = Array.from({ length: 2000 }, () => ({ cells: text("y".repeat(10)) }));
    const p = continuationPrefix(fakeBuffer(big, 10), "09:05", 4096);
    expect(new TextEncoder().encode(p).length).toBeLessThanOrEqual(4096);
    expect(p.endsWith(sep)).toBe(true);
    expect(PREFIX_MAX).toBe(1024 * 1024);
  });
});

// A real xterm (no window: it parses and keeps a buffer without being opened).
describe("the old text, from a real xterm", () => {
  let Terminal: typeof import("@xterm/xterm").Terminal;
  beforeAll(async () => {
    // jsdom has no canvas; xterm only asks for one to measure colours at load.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    ({ Terminal } = await import("@xterm/xterm"));
  });

  const write = (t: InstanceType<typeof Terminal>, s: string) => new Promise<void>((r) => t.write(s, r));

  it("carries no query, mode, alternate screen or title: written back, nothing is answered", async () => {
    const old = new Terminal({ cols: 40, rows: 6, allowProposedApi: true });
    const answered: string[] = [];
    old.onData((d) => answered.push(d));
    await write(old, "\x1b]0;evil title\x07\x1b[1;32mgreen\x1b[0m $ ls\r\n"
      + "\x1b[c\x1b[6n\x1b]11;?\x07\x1bP$qm\x1b\\" + "after-queries\r\n"
      + "\x1b[?2004h\x1b[?1000h\x1b[?1h" + "x".repeat(50) + "\r\n$ vim\r\n\x1b[?1049h\x1b[2Jfull-screen");
    expect(answered.length).toBeGreaterThan(0);                           // live, xterm did answer them
    const prefix = continuationPrefix(old.buffer.normal, "12:00");
    expect(onlySgr(prefix)).toBe(true);

    const fresh = new Terminal({ cols: 30, rows: 6, allowProposedApi: true });
    const sent: string[] = [];
    fresh.onData((d) => sent.push(d));
    let title = "";
    fresh.onTitleChange((t) => (title = t));
    await write(fresh, "\x1bc" + prefix);
    expect(sent).toEqual([]);
    expect(title).toBe("");
    expect(fresh.buffer.active.type).toBe("normal");
    expect(fresh.modes.bracketedPasteMode).toBe(false);
    expect(fresh.modes.mouseTrackingMode).toBe("none");
    expect(fresh.modes.applicationCursorKeysMode).toBe(false);
    const lines: string[] = [];
    for (let y = 0; y < fresh.buffer.active.length; y++) lines.push(fresh.buffer.active.getLine(y)!.translateToString(true));
    const shown = lines.join("\n");
    expect(shown).toContain("green $ ls");
    expect(shown).toContain("after-queries");
    expect(shown).toContain("── restarted 12:00 ──");
    expect(shown).not.toContain("full-screen");                          // the alternate screen is not the shell's history
    expect(shown.replace(/\n/g, "")).toContain("x".repeat(50));           // a wrapped line, rewrapped at 30
    const g = fresh.buffer.active.getLine(0)!.getCell(0)!;
    expect([g.isFgPalette(), g.getFgColor(), !!g.isBold()]).toEqual([true, 2, true]);
  });
});

describe("the panes holding a continued terminal", () => {
  it("hold its new id, shown or not; every other pane is left alone", () => {
    let w = setView(setView(defaultWorkspace(), 2, "terminal"), 3, "terminal");
    w = setPaneTerminal(setPaneTerminal(w, 2, "0a0a0a0a"), 3, "0b0b0b0b");
    const r = renameTerminal(w, "0a0a0a0a", "0c0c0c0c");
    expect(r.panes[1].terminalId).toBe("0c0c0c0c");
    expect(r.panes[2].terminalId).toBe("0b0b0b0b");
    expect(renameTerminal(w, "0d0d0d0d", "0c0c0c0c")).toBe(w);
  });
});
