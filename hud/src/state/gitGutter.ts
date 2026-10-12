// The git gutter (ED-9): each open buffer's lines against what HEAD holds,
// drawn beside the line numbers — added `--syn-string`, modified `--syn-fn`,
// deleted a `--red` marker at 60% (ED-3's table; theme.css draws them).
//
//  - **Against the buffer, not the disk**: the diff runs on the model's own
//    lines, so the gutter follows an unsaved edit and is clean again once the
//    edit is undone. HEAD's text comes from `GET /projects/{id}/git/base`
//    through the project the buffer was read through.
//  - **One per buffer**, however many panes show it (two panes on one file
//    share one model, so they share one diff); each editor draws the marks in
//    its own decorations.
//  - **Debounced and bounded**: the diff waits for a pause in typing
//    (`DEBOUNCE_MS`, longer for a long file), reads the lines once, and is
//    bounded by `lib/linediff`'s budget; a file past `MAX_LINES` has no gutter.
//  - **HEAD's text is read again only when HEAD moves**: the project's status
//    poll (state/gitStatus.ts) carries HEAD's commit id.
//  - **Never an env file** (the owner's `.env*` rule): the caller asks only for
//    a text or safety-layer buffer, and the daemon refuses an env file, a
//    withheld one and the gate's state besides — by path and by name in git.

import { api } from "../api";
import { diffLines, gutterMarks, marksKey, splitLines, type GutterMark } from "../lib/linediff";
import { gitStatus, type GitStatusStore } from "./gitStatus";
import type { GitBase } from "../types";

export const DEBOUNCE_MS = 150;
export const DEBOUNCE_LONG_MS = 500;
/** Past this many lines a file draws no gutter (the editor's own read cap is 2 MiB). */
export const MAX_LINES = 100_000;
const LONG = 5_000;

/** The part of a Monaco model this needs. */
export interface GutterModel {
  onDidChangeContent(fn: () => void): { dispose(): void };
  getValue(): string;
  getLinesContent?(): string[];
  getLineCount?(): number;
  isDisposed(): boolean;
}

export interface GutterTarget {
  /** The buffer's file on disk: one gutter per buffer. */
  abs: string;
  /** The project the buffer was read through, and its path there. */
  projectId: string;
  path: string;
}

export interface GutterDeps {
  base(projectId: string, path: string): Promise<GitBase>;
  status: Pick<GitStatusStore, "subscribe" | "get">;
}

interface Ctl {
  target: GutterTarget;
  model: GutterModel;
  subs: Set<(marks: GutterMark[]) => void>;
  base: string[] | null;
  /** The daemon's word for HEAD's text: ok, new, no_repo, binary …, or refused / error / loading. */
  state: string;
  /** The project's HEAD (from the status poll) when HEAD's text was asked for. */
  seenHead: string | null | undefined;
  marks: GutterMark[];
  key: string;
  timer: ReturnType<typeof setTimeout> | null;
  gen: number;
  dead: boolean;
  unhook: () => void;
}

const defaultDeps: GutterDeps = {
  base: (projectId, path) => api.gitBase(projectId, path),
  status: gitStatus,
};

export class GitGutter {
  private ctls = new Map<string, Ctl>();

  constructor(private deps: GutterDeps = defaultDeps) {}

  /**
   * Draw `target`'s gutter: `onMarks` hears the marks now (if any) and every
   * time they change. The returned function stops listening; the gutter goes
   * when nobody listens.
   */
  watch(target: GutterTarget, model: GutterModel, onMarks: (marks: GutterMark[]) => void): () => void {
    let ctl = this.ctls.get(target.abs);
    if (ctl && (ctl.model !== model || ctl.target.projectId !== target.projectId || ctl.target.path !== target.path)) {
      // Another model for the file, or the buffer moved: a gutter of its own.
      this.close(ctl);
      ctl = undefined;
    }
    if (!ctl) ctl = this.open(target, model);
    const c = ctl;
    c.subs.add(onMarks);
    if (c.marks.length) onMarks(c.marks);
    return () => {
      c.subs.delete(onMarks);
      if (!c.subs.size) this.close(c);
    };
  }

  /** What each gutter shows, for the tests: never the text. */
  snapshot(): { abs: string; path: string; state: string; marks: GutterMark[] }[] {
    return [...this.ctls.values()].map((c) => ({ abs: c.target.abs, path: c.target.path, state: c.state, marks: c.marks }));
  }

  private open(target: GutterTarget, model: GutterModel): Ctl {
    const c: Ctl = {
      target, model, subs: new Set(), base: null, state: "loading", seenHead: undefined, marks: [], key: "",
      timer: null, gen: 0, dead: false, unhook: () => {},
    };
    this.ctls.set(target.abs, c);
    const content = model.onDidChangeContent(() => this.schedule(c));
    const status = this.deps.status.subscribe(target.projectId, () => this.onStatus(c));
    c.unhook = () => {
      content.dispose();
      status();
    };
    this.fetchBase(c);
    return c;
  }

  private close(c: Ctl) {
    if (c.dead) return;
    c.dead = true;
    if (c.timer) clearTimeout(c.timer);
    c.unhook();
    if (this.ctls.get(c.target.abs) === c) this.ctls.delete(c.target.abs);
  }

  private onStatus(c: Ctl) {
    const head = this.deps.status.get(c.target.projectId)?.head ?? null;
    if (c.seenHead === undefined) {
      c.seenHead = head;
      return;
    }
    if (head !== c.seenHead) this.fetchBase(c);
  }

  private fetchBase(c: Ctl) {
    const gen = ++c.gen;
    const known = this.deps.status.get(c.target.projectId);
    c.seenHead = known ? known.head ?? null : undefined;
    this.deps.base(c.target.projectId, c.target.path).then(
      (ans) => {
        if (c.dead || gen !== c.gen) return;
        c.state = ans?.state || "error";
        c.base = ans?.state === "ok" && typeof ans.text === "string" ? splitLines(ans.text) : null;
        this.compute(c);
      },
      (err: any) => {
        if (c.dead || gen !== c.gen) return;
        c.state = err?.status === 403 ? "refused" : "error";
        c.base = null;
        this.compute(c);
      },
    );
  }

  private schedule(c: Ctl) {
    if (c.dead || !c.base) return;
    if (c.timer) clearTimeout(c.timer);
    const long = (c.model.getLineCount?.() ?? 0) > LONG;
    c.timer = setTimeout(() => {
      c.timer = null;
      this.compute(c);
    }, long ? DEBOUNCE_LONG_MS : DEBOUNCE_MS);
  }

  private compute(c: Ctl) {
    if (c.dead) return;
    let marks: GutterMark[] = [];
    if (c.base && !c.model.isDisposed()) {
      const lines = c.model.getLinesContent ? c.model.getLinesContent() : splitLines(c.model.getValue());
      if (lines.length <= MAX_LINES && c.base.length <= MAX_LINES) {
        marks = gutterMarks(diffLines(c.base, lines).hunks, lines.length);
      }
    }
    const key = marksKey(marks);
    if (key === c.key) return;
    c.key = key;
    c.marks = marks;
    for (const fn of Array.from(c.subs)) fn(marks);
  }
}

export const gitGutter = new GitGutter();
