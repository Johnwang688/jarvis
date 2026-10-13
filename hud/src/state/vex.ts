// The VEX view's state, outside React (VX-2): the daemon's VEX status, each
// project's detection, this window's last build per project (polled to its
// end), a download in progress, device info. Kept here so a pane switched
// away and back — or the VEX view in two panes — shows the same build, and a
// build's poll goes on while no VEX view is drawn.
//
// **Nothing builds or downloads behind an approval card** (the owner's rule
// for VX-2). Every action refuses while a card is up (`blocked`, driven from
// App above the workspace's error boundary, as the terminals' hold is), and
// once the workspace has crashed. A download's chain — pause the brain
// terminal, build first when the daemon says so, download, resume the brain
// terminal — is one click's: **any card that comes up between the click and
// a step drops the rest of the chain** (a card count, as `ownerOpened`'s),
// and the view says nothing was sent.
//
// **One chain per window** (VX-2 review, M1): while a download's chain runs,
// for any project, no other download starts, every VEX view says what it is
// doing ("Building override, then downloading to slot 3") with a Cancel that
// stops its build and never sends the download, and the VEX panes' project
// pickers are off — a chain never lands on a robot the view no longer shows.
//
// **Download is only the owner's click** on the labelled button (the button
// asks lib/vex.ts `isExplicitClick`); this store has no other caller that
// downloads, and no key, palette or tool reaches it.

import { api } from "../api";
import { terminals } from "../components/Terminal";
import {
  BUILD_FIRST_STEPS, DeviceLedger, LAST_BUILDS_KEY, chainCancellable, isVexId, needsBuildFirst, nameRefusal,
  parseDeviceRecord, parseLastBuilds, slotRefusal, stillStale, type BuildView, type ChainView, type Detection,
  type DeviceAction, type DeviceRead, type VexStatus,
} from "../lib/vex";

const POLL_MS = 600;
/** A build's own timeout is 5 minutes on the daemon; the poll gives up a little after. */
const BUILD_POLL_MAX_MS = 6 * 60_000;
/** A download's is 3 minutes. */
const DOWNLOAD_WAIT_MAX_MS = 4 * 60_000;
/** A project's detection older than this is asked again (on focus, and by App's timer). */
export const DETECT_STALE_MS = 30_000;

/** Said when a download closed the brain terminal and it was not opened again. */
const BRAIN_CLOSED = "The brain terminal was closed for the download";

export interface ProjectRun {
  /** This window's last build of the project (or one remembered across a reload). */
  build: BuildView | null;
  /** What this project's chain is doing now. */
  busy: "build" | "download" | null;
  /** A sentence about the last action: a refusal in the daemon's words, or progress. */
  note: string;
  noteTone: "ok" | "bad" | "info";
  /** The last download's end. */
  downloaded: { slot: number; ok: boolean; code: number | null } | null;
}

/** The window's one running download chain (M1). */
export interface Chain extends ChainView {
  seq: number;
  projectId: string;
  /** The build-first's program terminal: what Cancel stops. */
  buildTerminal: string | null;
}

export interface DeviceNotice {
  key: number;
  action: DeviceAction;
  at: string;
}

type Detected =
  | { state: "ok"; d: Detection; at: number }
  | { state: "loading" }
  | { state: "error"; message: string; at: number };

const blankRun = (): ProjectRun => ({ build: null, busy: null, note: "", noteTone: "info", downloaded: null });

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function loadLast(): Record<string, string> {
  try {
    return parseLastBuilds(localStorage.getItem(LAST_BUILDS_KEY));
  } catch {
    return {};
  }
}

function saveLast(v: Record<string, string>) {
  try {
    localStorage.setItem(LAST_BUILDS_KEY, JSON.stringify(v));
  } catch {
    /* a convenience only */
  }
}

export class VexStore {
  status: VexStatus | null = null;
  statusError = "";
  info: { text: string; exit_code: number | null; read: DeviceRead } | null = null;
  /** The device read on its way, if any (one at a time: the daemon's device reservation). */
  infoBusy: DeviceRead | null = null;
  infoNote = "";
  brainBusy = false;
  brainNote = "";
  notices: DeviceNotice[] = [];
  blocked = false;
  /** Every card that goes up counts: a chain step after a card came drops. */
  cards = 0;
  crashed = false;
  /** The one download chain running in this window, whatever project it is for. */
  chain: Chain | null = null;
  private chainSeq = 0;

  private detections = new Map<string, Detected>();
  private runs = new Map<string, ProjectRun>();
  private ledger = new DeviceLedger();
  private noticeSeq = 0;
  private version = 0;
  private listeners = new Set<() => void>();
  private polling = new Set<string>();
  private last = loadLast();

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getVersion = () => this.version;

  private changed() {
    this.version++;
    for (const fn of Array.from(this.listeners)) fn();
  }

  // -- the window's state (App drives these, above the boundary) -----------

  setBlocked(blocked: boolean) {
    if (this.blocked === blocked) return;
    this.blocked = blocked;
    if (blocked) this.cards++;
    this.changed();
  }

  /** The workspace crashed: nothing more is sent on the owner's behalf. */
  crash() {
    this.crashed = true;
    this.cards++;
    this.changed();
  }

  private mayAct(): boolean {
    return !this.blocked && !this.crashed;
  }

  // -- reading ---------------------------------------------------------------

  detection(projectId: string | null): Detection | null {
    if (!projectId) return null;
    const d = this.detections.get(projectId);
    return d && d.state === "ok" ? d.d : null;
  }

  detectionError(projectId: string | null): string {
    if (!projectId) return "";
    const d = this.detections.get(projectId);
    return d && d.state === "error" ? d.message : "";
  }

  detecting(projectId: string | null): boolean {
    return !!projectId && this.detections.get(projectId)?.state === "loading";
  }

  run(projectId: string): ProjectRun {
    let r = this.runs.get(projectId);
    if (!r) {
      r = blankRun();
      this.runs.set(projectId, r);
    }
    return r;
  }

  private patchRun(projectId: string, patch: Partial<ProjectRun>) {
    this.runs.set(projectId, { ...this.run(projectId), ...patch });
    this.changed();
  }

  private say(projectId: string, note: string, noteTone: ProjectRun["noteTone"] = "bad") {
    this.patchRun(projectId, { note, noteTone });
  }

  /** Detection for a project the window shows: fetched once, again when `force`. */
  want(projectId: string | null, force = false) {
    if (!projectId) return;
    const had = this.detections.get(projectId);
    if (had && (had.state === "loading" || !force)) return;
    if (!had) this.detections.set(projectId, { state: "loading" });
    const first = !had;
    if (first) this.changed();
    api.vexProject(projectId).then(
      (d) => {
        this.detections.set(projectId, { state: "ok", d, at: Date.now() });
        this.changed();
      },
      (e: any) => {
        // A refusal on a refresh keeps what was known.
        if (!first && this.detections.get(projectId)?.state === "ok") return;
        this.detections.set(projectId, { state: "error", message: e?.message || "VEX detection failed", at: Date.now() });
        this.changed();
      },
    );
    // A build this window ran before a reload: its record comes back.
    const id = this.last[projectId];
    if (id && !this.run(projectId).build && !this.polling.has(id)) void this.follow(projectId, id, false);
  }

  /**
   * Ask again for the detections older than `maxAgeMs` (0: all of them): on
   * the window's focus and on App's timer, so a project that becomes a VEX
   * project — its settings file written by VS Code or by Jarvis — is offered
   * the view without a reload (VX-2 review, L4).
   */
  refreshDetections(projectIds: string[], maxAgeMs: number) {
    const now = Date.now();
    for (const pid of projectIds) {
      const d = this.detections.get(pid);
      if (!d || d.state === "loading") continue;
      if (now - d.at >= maxAgeMs) this.want(pid, true);
    }
  }

  refreshStatus(): Promise<void> {
    return api.vexStatus().then(
      (s) => {
        this.status = s;
        this.statusError = s ? "" : "the daemon answered without a VEX status";
        this.changed();
      },
      (e: any) => {
        this.statusError = e?.message || "the VEX status could not be read";
        this.changed();
      },
    );
  }

  // -- build ---------------------------------------------------------------

  /** Build, Rebuild or Clean: the owner's click. */
  async build(projectId: string, action: "build" | "rebuild" | "clean"): Promise<void> {
    if (!this.mayAct()) return;
    const r = this.run(projectId);
    if (r.busy) return;
    this.patchRun(projectId, { busy: "build", note: "", downloaded: null });
    try {
      await this.runBuild(projectId, action);
    } finally {
      this.patchRun(projectId, { busy: null });
    }
  }

  /** Start a build and follow it to its end; the record, or null when refused (said). */
  private async runBuild(projectId: string, action: "build" | "rebuild" | "clean",
                         onStarted?: (terminalId: string | null) => void): Promise<BuildView | null> {
    let started: { build_id: string; terminal_id: string };
    try {
      started = await api.vexBuild(projectId, action);
    } catch (e: any) {
      this.say(projectId, e?.message || "the build could not start");
      return null;
    }
    if (!isVexId(started?.build_id)) {
      this.say(projectId, "the daemon answered without a build");
      return null;
    }
    this.last = { ...this.last, [projectId]: started.build_id };
    saveLast(this.last);
    this.patchRun(projectId, {
      build: {
        id: started.build_id, project_id: projectId, action, state: "running",
        terminal_id: isVexId(started.terminal_id) ? started.terminal_id : null,
        exit_code: null, errors: [], bin: null, started: "", ended: null,
      },
    });
    if (isVexId(started.terminal_id)) void terminals.showProgram(started.terminal_id);
    onStarted?.(isVexId(started.terminal_id) ? started.terminal_id : null);
    return this.follow(projectId, started.build_id, true);
  }

  /** Poll a build to its end. */
  private async follow(projectId: string, buildId: string, mine: boolean): Promise<BuildView | null> {
    if (this.polling.has(buildId)) return null;
    this.polling.add(buildId);
    const until = Date.now() + BUILD_POLL_MAX_MS;
    try {
      for (;;) {
        let b: BuildView;
        try {
          b = await api.vexBuildState(buildId);
        } catch (e: any) {
          if (e?.status === 404) {
            // Forgotten (a daemon restart, or 32 newer builds): not this window's any more.
            if (!mine) {
              const { [projectId]: _gone, ...rest } = this.last;
              this.last = rest;
              saveLast(this.last);
              return null;
            }
            this.say(projectId, "The daemon no longer knows this build (did Jarvis restart?)");
            return null;
          }
          if (Date.now() > until) {
            this.say(projectId, e?.message || "the build's state could not be read");
            return null;
          }
          await sleep(POLL_MS * 2);
          continue;
        }
        if (this.run(projectId).build?.id === buildId || mine || !this.run(projectId).build) {
          this.patchRun(projectId, { build: b });
        }
        if (b.state !== "running") return b;
        if (Date.now() > until) {
          this.say(projectId, "The build is still running; its terminal shows how far it got.", "info");
          return null;
        }
        await sleep(POLL_MS);
      }
    } finally {
      this.polling.delete(buildId);
    }
  }

  // -- download --------------------------------------------------------------

  /**
   * Download to the brain: **only the VEX view's labelled button calls
   * this, on the owner's own click** (`isExplicitClick`). One click's chain:
   * the brain terminal paused when it holds the port, Build first when the
   * daemon's refusal carries `build_first`, the download, then the brain
   * terminal again. **One chain per window**: refused while any project's
   * chain runs. A card that comes up at any point, a crash, or the owner's
   * Cancel drops the rest: nothing is sent, and the brain terminal is not
   * opened again by itself.
   */
  async download(projectId: string, slot: number, name: string, label = ""): Promise<void> {
    if (!this.mayAct()) return;
    if (this.chain) {
      // One chain per window (M1): the view the click came from says why.
      this.say(projectId, `A download is already running for ${this.chain.label || "another project"}: `
        + "wait for it, or Cancel it, first.");
      return;
    }
    const refusal = slotRefusal(slot) ?? nameRefusal(name);
    if (refusal) {
      this.say(projectId, refusal);
      return;
    }
    const r = this.run(projectId);
    if (r.busy) return;
    const chain: Chain = {
      seq: ++this.chainSeq, projectId, label, slot, phase: "start", cancelled: false, buildTerminal: null,
    };
    this.chain = chain;
    const cards = this.cards;
    const holds = () => !this.blocked && !this.crashed && this.cards === cards && !chain.cancelled;
    let droppedBy = "";
    // Said by what really stopped it (delta review NIT): the owner's Cancel,
    // the window's crash, or an approval card (`blocked` is only ever a card).
    const dropped = (what: string) => {
      if (chain.cancelled) {
        droppedBy = "the download was cancelled";
        this.say(projectId, "Cancelled: nothing was downloaded.", "info");
      } else if (this.crashed) {
        droppedBy = "the window hit an error";
        this.say(projectId, `The window hit an error ${what}: nothing was downloaded. Reload it, then click Download again.`);
      } else {
        droppedBy = "an approval card came up";
        this.say(projectId, `An approval card came up ${what}: nothing was downloaded. Click Download again when you are ready.`);
      }
    };
    const phase = (p: Chain["phase"]) => {
      chain.phase = p;
      this.changed();
    };
    this.patchRun(projectId, { busy: "download", note: "Downloading…", noteTone: "info", downloaded: null });
    let paused = false;
    try {
      await this.refreshStatus();
      if (!holds()) return dropped("before it started");
      const brain = this.status?.brain;
      if (brain) {
        // The brain terminal holds the brain's port: paused for the download,
        // opened again afterwards (plan §4.3 item 11).
        phase("pause");
        try {
          await api.closeTerminal(brain);
        } catch (e: any) {
          if (e?.status !== 404) {
            this.say(projectId, `The brain terminal could not be paused (${e?.message || "refused"}); close it, then download.`);
            return;
          }
        }
        terminals.gone(brain);
        paused = true;
        // The daemon frees the port once the program's end has been read:
        // asked again for a moment rather than once.
        for (let i = 0; i < 15; i++) {
          await this.refreshStatus();
          if (!this.status?.brain && this.status?.device !== "brain") break;
          await sleep(200);
        }
        if (this.status?.brain || this.status?.device === "brain") {
          this.say(projectId, "The brain terminal could not be paused; close it, then download.");
          return;
        }
      }
      if (!holds()) return dropped("before it started");
      // A download request in flight cannot be called back: no Cancel from here
      // until the daemon says Build first.
      phase("download");
      let sent = await this.sendDownload(projectId, slot, name);
      // "Build first": a build, then — when that build found nothing to do and
      // the daemon still says Build first (a header the makefile does not
      // list, VX-2 delta review MED-1) — one rebuild. Never a third.
      for (const action of BUILD_FIRST_STEPS) {
        if (!(sent.refused && needsBuildFirst(sent.code))) break;
        // Nothing builds behind a card that came up while the daemon answered (L1).
        if (!holds()) return dropped(action === "build" ? "before it built" : "before it rebuilt");
        phase(action);
        this.say(projectId, action === "build" ? "Building first…"
          : "Rebuilding from scratch: make found nothing to do, but the sources changed (a header it does not list)…",
        "info");
        const b = await this.runBuild(projectId, action, (tid) => {
          chain.buildTerminal = tid;
          if (chain.cancelled && tid) void this.stopBuild(tid);
        });
        if (!holds()) return dropped(action === "build" ? "while it built" : "while it rebuilt");
        if (!b) return;
        if (b.state !== "succeeded") {
          this.say(projectId, `The ${action} did not succeed, so nothing was downloaded.`);
          return;
        }
        chain.buildTerminal = null;
        phase("download");
        this.say(projectId, "Downloading…", "info");
        sent = await this.sendDownload(projectId, slot, name);
      }
      if (sent.refused && needsBuildFirst(sent.code)) {
        // Still "Build first" after a build and a rebuild: said, never a loop.
        this.say(projectId, stillStale(sent.refused));
        return;
      }
      phase("download");
      if (sent.refused || !sent.terminal) {
        this.say(projectId, sent.refused || "the download could not start");
        return;
      }
      void terminals.showProgram(sent.terminal);
      const end = await this.awaitExit(sent.terminal);
      this.patchRun(projectId, {
        downloaded: { slot, ok: end.code === 0, code: end.code },
        note: end.code === 0 ? `Downloaded to slot ${slot}. Start it on the brain or the controller.`
          : end.code === null ? "The download's end was not seen; its terminal says how it went."
            : `The download failed (exit ${end.code}); its terminal says why.`,
        noteTone: end.code === 0 ? "ok" : "bad",
      });
    } finally {
      if (this.chain === chain) this.chain = null;
      this.patchRun(projectId, { busy: null });
      if (paused) {
        if (droppedBy) {
          // A dropped chain never opens the brain terminal by itself (L6).
          this.brainNote = `${BRAIN_CLOSED} and not opened again, since ${droppedBy}. `
            + "Click \u201cOpen the brain terminal\u201d when you want it.";
          this.changed();
        } else {
          // The card count from the click: a card that came and went while
          // the download ran still keeps the brain terminal closed (NIT).
          void this.resumeBrain(cards);
        }
      }
    }
  }

  /** The owner's Cancel on the running chain: its build stopped, its download never sent. */
  cancelChain() {
    const c = this.chain;
    if (!c || !chainCancellable(c) || this.blocked || this.crashed) return;
    c.cancelled = true;
    this.changed();
    if (c.buildTerminal) void this.stopBuild(c.buildTerminal);
  }

  /** Stop a build's program: its terminal closed, as × does; the daemon's exit hook ends the build. */
  private async stopBuild(terminalId: string): Promise<void> {
    try {
      await api.closeTerminal(terminalId);
      terminals.gone(terminalId);
    } catch {
      /* already gone: the build has ended */
    }
  }

  private async sendDownload(projectId: string, slot: number, name: string):
      Promise<{ terminal: string | null; refused: string; code: unknown }> {
    this.ledger.expect("download", Date.now());
    try {
      const r = await api.vexDownload(projectId, { slot, name });
      return { terminal: isVexId(r?.terminal_id) ? r.terminal_id : null, refused: "", code: null };
    } catch (e: any) {
      this.ledger.withdraw("download");
      return { terminal: null, refused: e?.message || "the download could not start", code: e?.body?.code ?? null };
    }
  }

  /** A program terminal's end, read from the listing. */
  private async awaitExit(id: string): Promise<{ code: number | null }> {
    const until = Date.now() + DOWNLOAD_WAIT_MAX_MS;
    while (Date.now() < until) {
      await terminals.refresh();
      const row = terminals.row(id);
      if (!row) return { code: terminals.sessions.get(id)?.exitCode ?? null };
      if (row.exited) return { code: row.exit_code };
      await sleep(POLL_MS);
    }
    return { code: null };
  }

  // -- the brain terminal and device info ---------------------------------------

  /**
   * The brain terminal again after a download paused it — never behind a
   * card. The download's program may have ended a moment before the daemon
   * frees the device, so a "one device action at a time" is asked again.
   */
  private async resumeBrain(cards: number): Promise<void> {
    const closed = `${BRAIN_CLOSED} and could not be opened again by itself. `
      + "Click \u201cOpen the brain terminal\u201d when you want it.";
    for (let i = 0; i < 10; i++) {
      if (!this.mayAct() || this.cards !== cards) break;
      await this.refreshStatus();
      if (!this.status?.device) {
        await this.openBrain();
        if (!this.brainNote) return;
      }
      await sleep(300);
    }
    this.brainNote = closed;
    this.changed();
  }

  async openBrain(): Promise<void> {
    if (!this.mayAct() || this.brainBusy || this.chain) return;
    this.brainBusy = true;
    this.brainNote = "";
    this.changed();
    this.ledger.expect("brain", Date.now());
    try {
      const r = await api.vexBrain();
      if (!r?.started) this.ledger.withdraw("brain");
      if (isVexId(r?.terminal_id)) await terminals.showProgram(r.terminal_id);
    } catch (e: any) {
      this.ledger.withdraw("brain");
      this.brainNote = e?.message || "the brain terminal could not be opened";
    } finally {
      this.brainBusy = false;
      void this.refreshStatus();
      this.changed();
    }
  }

  /** System info (`vexcom --json`) or the programs on the brain (`vexcom --programs`). */
  async readInfo(read: DeviceRead = "system"): Promise<void> {
    if (!this.mayAct() || this.infoBusy || this.chain) return;
    this.infoBusy = read;
    this.infoNote = "";
    this.changed();
    this.ledger.expect("info", Date.now());
    try {
      const r = await api.vexInfo(read);
      this.info = { text: typeof r?.text === "string" ? r.text.slice(0, 64 * 1024) : "",
                    exit_code: typeof r?.exit_code === "number" ? r.exit_code : null, read };
    } catch (e: any) {
      this.ledger.withdraw("info");
      this.infoNote = e?.message || "the device could not be read";
    } finally {
      this.infoBusy = null;
      this.changed();
    }
  }

  // -- the bus ---------------------------------------------------------------

  /** `vex_device {action, at}`: one this window did not start is said. */
  heardDevice(data: unknown) {
    const rec = parseDeviceRecord(data);
    if (!rec) return;
    void this.refreshStatus();
    if (this.ledger.heard(rec.action, Date.now())) return;
    this.notices = [...this.notices, { key: ++this.noticeSeq, action: rec.action, at: rec.at }].slice(-3);
    this.changed();
  }

  dismissNotice(key: number) {
    this.notices = this.notices.filter((n) => n.key !== key);
    this.changed();
  }
}

export const vex = new VexStore();

if (typeof window !== "undefined") {
  // Read-only hooks for the headless checks.
  (window as any).__hudVex = {
    blocked: () => vex.blocked,
    cards: () => vex.cards,
    chain: () => (vex.chain ? { projectId: vex.chain.projectId, phase: vex.chain.phase, slot: vex.chain.slot,
                                cancelled: vex.chain.cancelled } : null),
    run: (projectId: string) => {
      const r = vex.run(projectId);
      return { busy: r.busy, note: r.note, build: r.build ? r.build.state : null, downloaded: r.downloaded };
    },
  };
}
