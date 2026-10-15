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
// doing ("Building override, then downloading to slot 3") with a Stop, and
// the VEX panes' project pickers are off — a chain never lands on a robot the
// view no longer shows.
//
// **The chain is clean → build → download** (the owner's decision 3 on the
// 2026-10-15 redesign): a failed clean or build stops it before anything
// reaches the robot, and the brain terminal is let go of the port only once
// the build has succeeded. **It is never opened again by the chain** (VX-UI-4a,
// owner decision 5: the brain's link opens only on the owner's Connect): the
// Brain tab says it was closed for the download and offers Connect. **Stop stops whatever is running** (decision 6): a
// clean or build (its program terminal closed, as × does), and a download
// already being sent (its vexcom closed the same way; the view then says the
// slot may hold an incomplete program). Stop is never `vexcom --stop`: it
// sends nothing to the brain.
//
// **Download is only the owner's click** on the labelled button (the button
// asks lib/vex.ts `isExplicitClick`); this store has no other caller that
// downloads, and no key, palette or tool reaches it.

import { api } from "../api";
import { terminals } from "../components/Terminal";
import {
  DeviceLedger, LAST_BUILDS_KEY, chainStoppable, isSourcePath, isVexId, needsBuildFirst, nameRefusal,
  parseDeviceRecord, parseLastBuilds, slotRefusal, stillStale, stoppedDownloadNote, type BuildView, type ChainStep,
  type ChainView, type Detection,
  type DeviceAction, type DeviceRead, type VexStatus, type DownloadView, type ConnectionView,
} from "../lib/vex";

const POLL_MS = 600;
/** A build's own timeout is 5 minutes on the daemon; the poll gives up a little after. */
const BUILD_POLL_MAX_MS = 6 * 60_000;
/** A download's is 3 minutes. */
const DOWNLOAD_WAIT_MAX_MS = 4 * 60_000;
/** A project's detection older than this is asked again (on focus, and by App's timer). */
export const DETECT_STALE_MS = 30_000;

/** Said when a download closed the brain terminal: it is never opened again by itself (owner decision 5). */
const BRAIN_CLOSED = "The brain terminal was closed for the download";

export interface ProjectRun {
  /** This window's last build of the project (or one remembered across a reload). */
  build: BuildView | null;
  startingBuild: { action: "build" | "rebuild" | "clean"; started: number } | null;
  buildNote: string;
  download: DownloadView | null;
  downloadNote: string;
  downloadStarted: number | null;
  /** What this project's chain is doing now. */
  busy: "build" | "download" | null;
  /** A sentence about the last action: a refusal in the daemon's words, or progress. */
  note: string;
  noteTone: "ok" | "bad" | "info";
  /** The last download's end; `stopped` when the owner's Stop cut it off. */
  downloaded: { slot: number; ok: boolean | null; code: number | null; stopped?: boolean } | null;
  /** Each kind's last record in this window, for the step marks (VX-UI-2). */
  results: Partial<Record<"clean" | "build" | "rebuild", BuildView>>;
  /** The build the owner's Stop stopped: said as stopped, not as failed. */
  stoppedBuild: string | null;
  /** Stop was pressed while a build of this project was still starting: closed once its terminal is known. */
  stopAsked: boolean;
  /** When a source file of this project was last saved in the editor (an advisory "edited since"). */
  editedAt: number | null;
}

/** The window's one running download chain (M1). */
export interface Chain extends ChainView {
  seq: number;
  projectId: string;
  /** The running clean's or build's program terminal: what Stop closes. */
  buildTerminal: string | null;
  /** The download's program terminal once it is known: what Stop closes then. */
  downloadTerminal: string | null;
  done: ChainStep[];
  /** The chain closed the brain terminal for the download (the Brain tab says "paused"). */
  brainPaused: boolean;
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

const blankRun = (): ProjectRun => ({ build: null, startingBuild: null, buildNote: "",
  busy: null, note: "", noteTone: "info", downloaded: null, download: null, downloadNote: "", downloadStarted: null,
  results: {}, stoppedBuild: null, stopAsked: false, editedAt: null });

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
  connection: ConnectionView | null = null;
  connectionError = "";
  private connectionPending = false;
  private connectionAt = 0;
  info: { text: string; exit_code: number | null; read: DeviceRead } | null = null;
  /** The device read on its way, if any (one at a time: the daemon's device reservation). */
  infoBusy: DeviceRead | null = null;
  infoNote = "";
  /** Connect's request is on its way. */
  brainBusy = false;
  /** The daemon's refusal of the last Connect or Disconnect, in its words. */
  brainNote = "";
  /**
   * The brain terminal the Brain tab shows (VX-UI-4a): the one Connect opened,
   * or the one `/vex/status` names (a link already open, after a reload). Kept
   * after its program ends, so the tab shows how it ended; let go of when it is
   * disconnected or a download closes it.
   */
  brainTerminal: string | null = null;
  /** A download closed the brain terminal and it was not opened again: why, and Connect. */
  brainClosed = "";
  /** Brain terminals this window closed: a status read from before the daemon let go of one never brings it back. */
  private brainGone = new Set<string>();
  notices: DeviceNotice[] = [];
  blocked = false;
  /** Every card that goes up counts: a chain step after a card came drops. */
  cards = 0;
  crashed = false;
  /** The one download chain running in this window, whatever project it is for. */
  chain: Chain | null = null;
  private chainSeq = 0;
  /** Downloads the owner's Stop cut off: said as stopped whenever their end is read. */
  private stoppedDownloads = new Set<string>();

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
    const next = { ...this.run(projectId), ...patch };
    const b = patch.build;
    if (b && (b.action === "clean" || b.action === "build" || b.action === "rebuild")) {
      next.results = { ...next.results, [b.action]: b };
    }
    this.runs.set(projectId, next);
    this.changed();
  }

  /** A file of this project saved through the editor: a source file makes its last build "edited since" (advisory). */
  edited(projectId: string, path: string) {
    if (!isSourcePath(path)) return;
    this.patchRun(projectId, { editedAt: Date.now() });
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
    const build = this.run(projectId).build;
    if (id && (!build || (force && build.state === "running")) && !this.polling.has(id)) {
      void this.follow(projectId, id, false);
    }
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
        // A link the daemon holds open (a reload, another window's Connect) is
        // shown, never opened: nothing here asks for one.
        if (s?.brain && !this.brainGone.has(s.brain)) this.brainTerminal = s.brain;
        this.changed();
      },
      (e: any) => {
        this.statusError = e?.message || "the VEX status could not be read";
        this.changed();
      },
    );
  }

  // -- build ---------------------------------------------------------------

  monitoringBuild(projectId: string): boolean {
    const id = this.run(projectId).build?.id;
    return !!id && this.polling.has(id);
  }

  /** Recheck an action whose monitoring ended without a final status. */
  async refreshBuild(projectId: string): Promise<void> {
    if (!this.mayAct()) return;
    const build = this.run(projectId).build;
    if (build?.state === "running") await this.follow(projectId, build.id, false);
  }

  /** Build, Rebuild or Clean: the owner's click. */
  async build(projectId: string, action: "build" | "rebuild" | "clean"): Promise<void> {
    if (!this.mayAct()) return;
    const r = this.run(projectId);
    if (r.busy || r.startingBuild || r.build?.state === "running") return;
    this.patchRun(projectId, { busy: "build", note: "", stopAsked: false, stoppedBuild: null });
    try {
      await this.runBuild(projectId, action, (tid) => {
        // Stop pressed while the build was still starting.
        if (tid && this.run(projectId).stopAsked) this.stopBuildOf(projectId, tid);
      });
    } finally {
      this.patchRun(projectId, { busy: null, stopAsked: false });
    }
  }

  /** Start a build and follow it to its end; the record, or null when refused (said). */
  private async runBuild(projectId: string, action: "build" | "rebuild" | "clean",
                         onStarted?: (terminalId: string | null) => void): Promise<BuildView | null> {
    const requestedAt = Date.now();
    this.patchRun(projectId, { startingBuild: { action, started: requestedAt }, buildNote: "" });
    let started: { build_id: string; terminal_id: string };
    try {
      started = await api.vexBuild(projectId, action);
    } catch (e: any) {
      this.patchRun(projectId, { buildNote: e?.message || "the build could not start" });
      this.say(projectId, e?.message || "the build could not start");
      return null;
    } finally {
      this.patchRun(projectId, { startingBuild: null });
    }
    if (!isVexId(started?.build_id)) {
      this.patchRun(projectId, { buildNote: "the daemon answered without a build" });
      this.say(projectId, "the daemon answered without a build");
      return null;
    }
    this.last = { ...this.last, [projectId]: started.build_id };
    saveLast(this.last);
    this.patchRun(projectId, {
      build: {
        id: started.build_id, project_id: projectId, action, state: "running",
        terminal_id: isVexId(started.terminal_id) ? started.terminal_id : null,
        exit_code: null, errors: [], output: "", bin: null, started: new Date(requestedAt).toISOString(), ended: null,
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
    this.changed();
    const until = Date.now() + BUILD_POLL_MAX_MS;
    try {
      for (;;) {
        let b: BuildView;
        try {
          b = await api.vexBuildState(buildId);
        } catch (e: any) {
          if (e?.status === 404) {
            // Forgotten after a restart or 32 newer builds. Clear a restored
            // running record too, so it cannot leave the controls locked.
            if (this.last[projectId] === buildId) {
              const { [projectId]: _gone, ...rest } = this.last;
              this.last = rest;
              saveLast(this.last);
            }
            if (this.run(projectId).build?.id === buildId) {
              this.patchRun(projectId, { build: null, buildNote: "The daemon no longer knows this build (did Jarvis restart?)" });
            }
            if (mine) this.say(projectId, "The daemon no longer knows this build (did Jarvis restart?)");
            return null;
          }
          this.patchRun(projectId, { buildNote: `Build status unavailable: ${e?.message || "connection failed"}. Retrying…` });
          if (Date.now() > until) {
            this.patchRun(projectId, { buildNote: `Build status unavailable: ${e?.message || "connection failed"}. Open the terminal to check the action.` });
            this.say(projectId, e?.message || "the build's state could not be read");
            return null;
          }
          await sleep(POLL_MS * 2);
          continue;
        }
        if (this.run(projectId).build?.id === buildId || mine || !this.run(projectId).build) {
          this.patchRun(projectId, { build: b, buildNote: "" });
        }
        if (b.state !== "running") return b;
        if (Date.now() > until) {
          this.patchRun(projectId, { buildNote: "Status monitoring stopped: the daemon still reports this action running. Open the terminal to check it." });
          this.say(projectId, "The build is still running; its terminal shows how far it got.", "info");
          return null;
        }
        await sleep(POLL_MS);
      }
    } finally {
      this.polling.delete(buildId);
      this.changed();
    }
  }

  /** Visible panes demand one coalesced cached check, never behind a card or chain. */
  async refreshConnection(): Promise<void> {
    if (!this.mayAct() || this.chain || this.infoBusy || this.connectionPending
      || !this.status?.configured || document.visibilityState !== "visible" || Date.now() - this.connectionAt < 1000) return;
    this.connectionPending = true;
    this.connectionAt = Date.now();
    try {
      this.connection = await api.vexConnection();
      this.connectionError = "";
    } catch {
      this.connectionError = "USB status unavailable; last confirmed state retained.";
    } finally {
      this.connectionPending = false;
      this.changed();
    }
  }

  async refreshDownload(projectId: string, expectedId?: string): Promise<DownloadView | null> {
    const id = expectedId ?? this.run(projectId).download?.id;
    if (!id || this.crashed) return null;
    try {
      const d = await api.vexDownloadState(id);
      if (this.crashed || this.run(projectId).download?.id !== id) return null;
      if (d.id !== id || d.project_id !== projectId) throw new Error("download identity changed");
      const current = this.run(projectId).download!;
      // A settled backend receipt is immutable. Late overlapping manual reads
      // must not regress it to running or replace its final output/outcome.
      if (current.state !== "running") return current;
      this.patchRun(projectId, {download: d, downloadNote: ""});
      if (d.state !== "running") this.downloadEnded(projectId, d.slot, d);
      return d;
    } catch (e: any) {
      if (this.crashed || this.run(projectId).download?.id !== id) return null;
      const current = this.run(projectId).download!;
      if (current.state !== "running") return current;
      this.patchRun(projectId, {downloadNote: e?.status === 404
        ? "This download record is no longer available. Check its terminal; no download was retried."
        : "Could not read Download status. Retrying; check its terminal for output."});
      return null;
    }
  }

  // -- download --------------------------------------------------------------

  /**
   * Download to the brain: **only the VEX view's labelled button calls
   * this, on the owner's own click** (`isExplicitClick`). One click's chain
   * (owner decision 3, 2026-10-15): **clean, then build**, then — the build
   * having succeeded — the brain terminal paused when it holds the port, the
   * download, and the brain terminal again. A failed clean or build stops the
   * chain before anything reaches the robot. Should the daemon still say
   * Build first (`build_first`: a source edited during the build), one
   * rebuild, then the download; still after that, it stops and says so.
   * **One chain per window**: refused while any project's chain runs. A card
   * that comes up at any point, a crash, or the owner's Stop drops the rest:
   * nothing more is sent, and the brain terminal is not opened again by
   * itself. Stop during the download itself closes vexcom (`stop`).
   */
  async download(projectId: string, slot: number, name: string, label = ""): Promise<void> {
    if (!this.mayAct()) return;
    if (this.chain) {
      // One chain per window (M1): the view the click came from says why.
      this.say(projectId, `A download is already running for ${this.chain.label || "another project"}: `
        + "wait for it, or Stop it, first.");
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
      downloadTerminal: null, done: [], brainPaused: false,
    };
    this.chain = chain;
    const cards = this.cards;
    const holds = () => !this.blocked && !this.crashed && this.cards === cards && !chain.cancelled;
    let droppedBy = "";
    // Said by what really stopped it (delta review NIT): the owner's Stop,
    // the window's crash, or an approval card (`blocked` is only ever a card).
    const dropped = (what: string) => {
      if (chain.cancelled) {
        droppedBy = "the download was stopped";
        this.say(projectId, "Stopped: nothing was downloaded.", "info");
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
    /** One make step of the chain; false when the chain stops there (said). */
    const step = async (action: "clean" | "build" | "rebuild"): Promise<boolean> => {
      const before = action === "clean" ? "before it cleaned" : action === "build" ? "before it built" : "before it rebuilt";
      const during = action === "clean" ? "while it cleaned" : action === "build" ? "while it built" : "while it rebuilt";
      // Nothing builds behind a card that came up while the daemon answered (L1).
      if (!holds()) {
        dropped(before);
        return false;
      }
      phase(action);
      this.say(projectId, action === "clean" ? "Cleaning first…" : action === "build" ? "Building…"
        : "Rebuilding from scratch: make found nothing to do, but the sources changed (a header it does not list)…",
      "info");
      const b = await this.runBuild(projectId, action, (tid) => {
        chain.buildTerminal = tid;
        if (chain.cancelled && tid) this.stopBuildOf(projectId, tid);
      });
      chain.buildTerminal = null;
      if (!holds()) {
        dropped(during);
        return false;
      }
      if (!b) return false;
      if (b.state !== "succeeded") {
        this.say(projectId, `The ${action} did not succeed, so nothing was downloaded.`);
        return false;
      }
      chain.done = [...chain.done, action];
      this.changed();
      return true;
    };
    this.patchRun(projectId, { busy: "download", note: "Cleaning first…", noteTone: "info", downloaded: null,
      download: null, downloadNote: "", downloadStarted: Date.now() });
    let paused = false;
    try {
      await this.refreshStatus();
      if (!holds()) return dropped("before it started");
      if (!(await step("clean")) || !(await step("build"))) return;
      if (!holds()) return dropped("before it downloaded");
      // The build may have taken minutes: whether the brain terminal holds
      // the port is asked now, not read from the chain's start.
      await this.refreshStatus();
      if (!holds()) return dropped("before it downloaded");
      const brain = this.status?.brain;
      if (brain) {
        // The brain terminal holds the brain's port: closed for the download —
        // only now that the build has succeeded, so its output runs on while
        // the project builds — and never opened again by the chain (VX-UI-4a).
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
        this.brainGone.add(brain);
        paused = true;
        chain.brainPaused = true;
        if (this.brainTerminal === brain) this.brainTerminal = null;
        this.brainClosed = "";
        this.changed();
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
      if (!holds()) return dropped("before it downloaded");
      phase("download");
      this.say(projectId, "Downloading…", "info");
      let sent = await this.sendDownload(projectId, slot, name);
      // Still "Build first" after the build (a source edited while it ran, or
      // a header the makefile does not list: VX-2 delta review MED-1): one
      // rebuild. Never a second.
      if (sent.refused && needsBuildFirst(sent.code)) {
        if (!(await step("rebuild"))) return;
        phase("download");
        this.say(projectId, "Downloading…", "info");
        sent = await this.sendDownload(projectId, slot, name);
      }
      if (sent.refused && needsBuildFirst(sent.code)) {
        if (!holds()) return dropped("before it downloaded");
        // Still "Build first" after a build and a rebuild: said, never a loop.
        this.say(projectId, stillStale(sent.refused));
        return;
      }
      if (sent.refused || !sent.terminal || !sent.id) {
        if (chain.cancelled) return dropped("");
        this.say(projectId, sent.refused || "the download could not start");
        return;
      }
      chain.downloadTerminal = sent.terminal;
      // Stop pressed while the request was on its way: vexcom is closed at once.
      if (chain.cancelled) {
        this.stoppedDownloads.add(sent.id);
        void this.stopProgram(sent.terminal);
      } else void terminals.showProgram(sent.terminal);
      const seeded: DownloadView = {id: sent.id, project_id: projectId, slot, state: "running",
        terminal_id: sent.terminal, exit_code: null, output: "", started: new Date().toISOString(), ended: null};
      this.patchRun(projectId, {download: seeded});
      const end = await this.followDownload(projectId, sent.id);
      if (chain.cancelled) droppedBy = "the download was stopped";
      this.downloadEnded(projectId, slot, end, chain.cancelled);
      if (end?.state === "succeeded") chain.done = [...chain.done, "download"];
    } finally {
      if (this.chain === chain) this.chain = null;
      this.patchRun(projectId, { busy: null });
      if (paused) {
        // Never opened again by itself (VX-UI-4a, owner decision 5: the link
        // opens only on the owner's Connect). A dropped or stopped chain says
        // why it stopped (L6); either way the Brain tab offers Connect.
        this.brainClosed = droppedBy
          ? `${BRAIN_CLOSED} and not opened again, since ${droppedBy}. Click Connect in the Brain tab when you want it.`
          : `${BRAIN_CLOSED}. Click Connect in the Brain tab to open it again; it never reopens by itself.`;
        this.changed();
      }
    }
  }

  private downloadEnded(projectId: string, slot: number, d: DownloadView | null, stopped = false) {
    // A download Stop cut off stays "stopped" however its end is learned —
    // the chain's own follow, or a later Check status (review NIT-2).
    stopped = stopped || (!!d && this.stoppedDownloads.has(d.id))
      || (!d && !!this.run(projectId).downloaded?.stopped);
    const ok = d ? d.state === "succeeded" : null;
    // Stop cut it off: the record's own end is the truth (a download that
    // finished before vexcom was closed is in the slot), else the slot's
    // state is not known and the view says so (owner decision 6).
    const cut = stopped && ok !== true;
    this.patchRun(projectId, {
      downloaded: {slot, ok: cut ? false : ok, code: d?.exit_code ?? null, stopped: cut},
      note: cut ? stoppedDownloadNote(slot)
        : ok === true ? `Downloaded to slot ${slot}. Start it on the brain or the controller.`
        : !d ? "Download outcome unknown. Check status or its terminal; no download was retried."
          : d.state === "timed_out" ? "Download timed out. Check its terminal and the brain before retrying."
            : `Download failed (exit ${d.exit_code ?? "unknown"}); check its terminal.`,
      noteTone: ok === true ? "ok" : ok === null && !cut ? "info" : "bad",
    });
  }

  /** Whether Stop has something to stop for this view: the window's chain, or this project's own clean or build. */
  stoppable(projectId: string | null): boolean {
    if (!projectId) return false;
    // A view's Stop is for its own project only (review MED-2): another
    // project's chain is said in every view, and stopped only from its own.
    if (this.chain?.projectId === projectId) return chainStoppable(this.chain);
    const r = this.run(projectId);
    if (r.stopAsked) return false;
    return !!r.startingBuild || (r.build?.state === "running" && !!r.build.terminal_id);
  }

  /**
   * The owner's **Stop** (owner decision 6, 2026-10-15; the view's handler
   * asks `isTrustedPress`), for **this view's project only** (review MED-2):
   * its running chain — its clean or build stopped, **or its download cut
   * off** — else its own running clean, build or rebuild. It closes the action's program terminal, as ×
   * does; the daemon's exit hook settles the record. Refused under a card or
   * after a crash. Never `vexcom --stop`: it never sends anything to the brain.
   */
  stop(projectId: string | null) {
    if (this.blocked || this.crashed || !projectId) return;
    const c = this.chain;
    // Never another project's chain (review MED-2); this project's own build, if any, below.
    if (c && c.projectId === projectId) {
      if (!chainStoppable(c)) return;
      c.cancelled = true;
      this.changed();
      // Its running clean or build is said as stopped, not failed (review LOW-2).
      if (c.buildTerminal) this.stopBuildOf(c.projectId, c.buildTerminal);
      if (c.downloadTerminal) {
        const id = this.run(c.projectId).download?.id;
        if (id) this.stoppedDownloads.add(id);
        void this.stopProgram(c.downloadTerminal);
      }
      return;
    }
    const r = this.run(projectId);
    const tid = r.build?.state === "running" ? r.build.terminal_id : null;
    if (tid) this.stopBuildOf(projectId, tid);
    else if (r.startingBuild) this.patchRun(projectId, { stopAsked: true });
  }

  private stopBuildOf(projectId: string, terminalId: string) {
    const b = this.run(projectId).build;
    this.patchRun(projectId, { stoppedBuild: b?.id ?? null, stopAsked: false });
    void this.stopProgram(terminalId);
  }

  /** Stop a program terminal: closed, as × does; the daemon's exit hook ends its build or download record. */
  private async stopProgram(terminalId: string): Promise<void> {
    try {
      await api.closeTerminal(terminalId);
      terminals.gone(terminalId);
    } catch {
      /* already gone: the action has ended */
    }
  }

  private async sendDownload(projectId: string, slot: number, name: string):
      Promise<{ terminal: string | null; id: string | null; refused: string; code: unknown }> {
    this.ledger.expect("download", Date.now());
    try {
      const r = await api.vexDownload(projectId, { slot, name });
      return { terminal: isVexId(r?.terminal_id) ? r.terminal_id : null, id: isVexId(r?.download_id) ? r.download_id : null, refused: "", code: null };
    } catch (e: any) {
      this.ledger.withdraw("download");
      return { terminal: null, id: null, refused: e?.message || "the download could not start", code: e?.body?.code ?? null };
    }
  }

  /** Actual Download receipts own completion; a missing/stale terminal list proves nothing. */
  private async followDownload(projectId: string, id: string): Promise<DownloadView | null> {
    const until = Date.now() + DOWNLOAD_WAIT_MAX_MS;
    while (!this.crashed && Date.now() < until) {
      const d = await this.refreshDownload(projectId, id);
      if (d && d.id === id && d.state !== "running") return d;
      if (this.run(projectId).downloadNote.startsWith("This download record")) return null;
      await sleep(POLL_MS);
    }
    this.patchRun(projectId, {downloadNote: "Automatic status checks stopped. Check status or open the terminal; no download was retried."});
    return null;
  }

  // -- the brain terminal and device info ---------------------------------------

  /**
   * **Connect** (VX-UI-4a): the brain terminal, `POST /vex/brain` (exactly
   * `vexcom --user`, unchanged). **Only the Brain tab's Connect calls this, on
   * the owner's trusted press** (`isTrustedPress`) — never a tab being shown,
   * a reload or the end of a download (owner decision 5). Off under a card,
   * after a crash, during a download chain and while a device read is on its
   * way; the daemon's one-device reservation decides the rest. The terminal is
   * held as this window's and drawn in the Brain tab (`vex-<pane>`), never
   * the panel opened for it.
   */
  async connectBrain(): Promise<void> {
    if (!this.mayAct() || this.brainBusy || this.chain || this.infoBusy) return;
    this.brainBusy = true;
    this.brainNote = "";
    this.brainClosed = "";
    this.changed();
    this.ledger.expect("brain", Date.now());
    try {
      const r = await api.vexBrain();
      if (!r?.started) this.ledger.withdraw("brain");
      if (isVexId(r?.terminal_id)) {
        this.brainTerminal = r.terminal_id;
        await terminals.adoptProgram(r.terminal_id);
      }
    } catch (e: any) {
      this.ledger.withdraw("brain");
      this.brainNote = e?.message || "the brain terminal could not be opened";
    } finally {
      this.brainBusy = false;
      void this.refreshStatus();
      this.changed();
    }
  }

  /**
   * **Disconnect**: the brain terminal closed (`DELETE /terminals/{id}`, as a
   * download's pause and × do), which ends `vexcom --user` and frees the port.
   * The program on the brain keeps running. Off under a card and after a crash.
   */
  async disconnectBrain(): Promise<void> {
    if (!this.mayAct()) return;
    const id = this.brainTerminal ?? this.status?.brain ?? null;
    if (!id) return;
    this.brainNote = "";
    try {
      await api.closeTerminal(id);
    } catch (e: any) {
      if (e?.status !== 404) {
        this.brainNote = e?.message || "the brain terminal could not be closed";
        this.changed();
        return;
      }
    }
    terminals.gone(id);
    this.brainGone.add(id);
    if (this.brainTerminal === id) this.brainTerminal = null;
    this.changed();
    void this.refreshStatus();
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
                                cancelled: vex.chain.cancelled, done: [...vex.chain.done],
                                brainPaused: vex.chain.brainPaused } : null),
    brain: () => ({ terminal: vex.brainTerminal, busy: vex.brainBusy, note: vex.brainNote, closed: vex.brainClosed }),
    run: (projectId: string) => {
      const r = vex.run(projectId);
      return { busy: r.busy, note: r.note, build: r.build ? r.build.state : null, downloaded: r.downloaded };
    },
  };
}
