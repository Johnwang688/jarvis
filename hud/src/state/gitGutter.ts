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
//    bounded by `lib/linediff`'s budget; a file past `MAX_LINES` has no gutter
//    (counted before the lines are read).
//  - **HEAD's text is read again when HEAD moves**: the project's status poll
//    (state/gitStatus.ts) carries its repository's HEAD. **A file in another
//    repository** — a nested one, a folder of several robot repositories, the
//    Inbox at `~`, an extra folder — answers with a HEAD that is not the
//    project's (or the project is no repository at all), so the poll can
//    never see it move: such a gutter asks again every `RECHECK_MS` while the
//    window is visible (review of PR #61, LOW 2).
//  - **Never an env file** (the owner's `.env*` rule): the caller asks only for
//    a text or safety-layer buffer, and the daemon refuses an env file, a
//    withheld one and the gate's state besides — by path and by name in git.

import { api } from "../api";
import { diffLines, gutterMarks, marksKey, splitLines, type GutterMark } from "../lib/linediff";
import { gitStatus, type GitStatusStore } from "./gitStatus";
import type { GitBase } from "../types";

export const DEBOUNCE_MS = 150;
export const DEBOUNCE_LONG_MS = 500;
/** How often a file in another repository than the project's asks for HEAD's text again. */
export const RECHECK_MS = 10_000;
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
  /** False while the window is hidden: a recheck waits. */
  visible?(): boolean;
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
  /** The HEAD the base answer was read from: the file's own repository's. */
  baseHead: string | null | undefined;
  marks: GutterMark[];
  key: string;
  timer: ReturnType<typeof setTimeout> | null;
  recheck: ReturnType<typeof setTimeout> | null;
  gen: number;
  dead: boolean;
  unhook: () => void;
}

const defaultDeps: GutterDeps = {
  base: (projectId, path) => api.gitBase(projectId, path),
  status: gitStatus,
  visible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
};

/** A UTF-8 byte-order mark is the file's, not the text's: the buffer keeps it off its model too. */
const unBOM = (text: string) => (text.startsWith("﻿") ? text.slice(1) : text);

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
  snapshot(): { abs: string; path: string; state: string; marks: GutterMark[]; rechecking: boolean }[] {
    return [...this.ctls.values()].map((c) => ({
      abs: c.target.abs, path: c.target.path, state: c.state, marks: c.marks, rechecking: !!c.recheck,
    }));
  }

  private open(target: GutterTarget, model: GutterModel): Ctl {
    const c: Ctl = {
      target, model, subs: new Set(), base: null, state: "loading", seenHead: undefined, baseHead: undefined,
      marks: [], key: "", timer: null, recheck: null, gen: 0, dead: false, unhook: () => {},
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
    if (c.recheck) clearTimeout(c.recheck);
    c.recheck = null;
    c.unhook();
    if (this.ctls.get(c.target.abs) === c) this.ctls.delete(c.target.abs);
  }

  private onStatus(c: Ctl) {
    const head = this.deps.status.get(c.target.projectId)?.head ?? null;
    if (c.seenHead === undefined) {
      c.seenHead = head;
      this.armRecheck(c);
      return;
    }
    if (head !== c.seenHead) this.fetchBase(c);
  }

  /**
   * Whether the project's status poll cannot see this file's HEAD move: its
   * base was read from a HEAD that is not the project's (another repository,
   * or a project that is none).
   */
  private elsewhere(c: Ctl): boolean {
    return c.baseHead != null && c.seenHead !== undefined && c.baseHead !== c.seenHead;
  }

  private armRecheck(c: Ctl) {
    if (c.dead) return;
    if (!this.elsewhere(c)) {
      if (c.recheck) clearTimeout(c.recheck);
      c.recheck = null;
      return;
    }
    if (c.recheck) return;
    c.recheck = setTimeout(() => {
      c.recheck = null;
      if (c.dead) return;
      if (this.deps.visible ? this.deps.visible() : true) this.fetchBase(c);
      else this.armRecheck(c);
    }, RECHECK_MS);
  }

  private fetchBase(c: Ctl) {
    const gen = ++c.gen;
    const known = this.deps.status.get(c.target.projectId);
    c.seenHead = known ? known.head ?? null : undefined;
    this.deps.base(c.target.projectId, c.target.path).then(
      (ans) => {
        if (c.dead || gen !== c.gen) return;
        c.state = ans?.state || "error";
        c.baseHead = ans?.head ?? null;
        c.base = ans?.state === "ok" && typeof ans.text === "string" ? splitLines(unBOM(ans.text)) : null;
        this.compute(c);
        this.armRecheck(c);
      },
      (err: any) => {
        if (c.dead || gen !== c.gen) return;
        c.state = err?.status === 403 ? "refused" : "error";
        c.base = null;
        c.baseHead = null;
        this.compute(c);
        this.armRecheck(c);
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
    if (c.base && c.base.length <= MAX_LINES && !c.model.isDisposed()) {
      // Counted before the lines are read: a huge file is never copied out.
      const count = c.model.getLineCount?.();
      if (count === undefined || count <= MAX_LINES) {
        const lines = c.model.getLinesContent ? c.model.getLinesContent() : splitLines(c.model.getValue());
        if (lines.length <= MAX_LINES) marks = gutterMarks(diffLines(c.base, lines).hunks, lines.length);
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
