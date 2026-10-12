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
// **Download is only the owner's click** on the labelled button (the button
// asks lib/vex.ts `isExplicitClick`); this store has no other caller that
// downloads, and no key, palette or tool reaches it.

import { api } from "../api";
import { terminals } from "../components/Terminal";
import {
  DeviceLedger, LAST_BUILDS_KEY, isVexId, needsBuildFirst, nameRefusal, parseDeviceRecord, parseLastBuilds,
  slotRefusal, type BuildView, type Detection, type DeviceAction, type VexStatus,
} from "../lib/vex";

const POLL_MS = 600;
/** A build's own timeout is 5 minutes on the daemon; the poll gives up a little after. */
const BUILD_POLL_MAX_MS = 6 * 60_000;
/** A download's is 3 minutes. */
const DOWNLOAD_WAIT_MAX_MS = 4 * 60_000;

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

export interface DeviceNotice {
  key: number;
  action: DeviceAction;
  at: string;
}

type Detected = { state: "ok"; d: Detection; at: number } | { state: "loading" } | { state: "error"; message: string };

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
  info: { text: string; exit_code: number | null } | null = null;
  infoBusy = false;
  infoNote = "";
  brainBusy = false;
  brainNote = "";
  notices: DeviceNotice[] = [];
  blocked = false;
  /** Every card that goes up counts: a chain step after a card came drops. */
  cards = 0;
  crashed = false;

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
        this.detections.set(projectId, { state: "error", message: e?.message || "VEX detection failed" });
        this.changed();
      },
    );
    // A build this window ran before a reload: its record comes back.
    const id = this.last[projectId];
    if (id && !this.run(projectId).build && !this.polling.has(id)) void this.follow(projectId, id, false);
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
  private async runBuild(projectId: string, action: "build" | "rebuild" | "clean"): Promise<BuildView | null> {
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
   * daemon says the program is missing or older than its sources, the
   * download, then the brain terminal again. A card that comes up at any
   * point drops the rest: nothing is sent behind it.
   */
  async download(projectId: string, slot: number, name: string): Promise<void> {
    if (!this.mayAct()) return;
    const refusal = slotRefusal(slot) ?? nameRefusal(name);
    if (refusal) {
      this.say(projectId, refusal);
      return;
    }
    const r = this.run(projectId);
    if (r.busy) return;
    const cards = this.cards;
    const holds = () => !this.blocked && !this.crashed && this.cards === cards;
    const dropped = (what: string) =>
      this.say(projectId, `An approval card came up ${what}: nothing was downloaded. Click Download again when you are ready.`);
    this.patchRun(projectId, { busy: "download", note: "Downloading…", noteTone: "info", downloaded: null });
    let paused = false;
    try {
      await this.refreshStatus();
      if (!holds()) return dropped("before it started");
      const brain = this.status?.brain;
      if (brain) {
        // The brain terminal holds the brain's port: paused for the download,
        // opened again afterwards (plan §4.3 item 11).
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
        await this.refreshStatus();
        if (this.status?.brain) {
          this.say(projectId, "The brain terminal could not be paused; close it, then download.");
          return;
        }
      }
      if (!holds()) return dropped("before it started");
      let sent = await this.sendDownload(projectId, slot, name);
      if (sent.refused && needsBuildFirst(sent.refused)) {
        this.say(projectId, "Building first…", "info");
        const b = await this.runBuild(projectId, "build");
        if (!b) return;
        if (b.state !== "succeeded") {
          this.say(projectId, "The build did not succeed, so nothing was downloaded.");
          return;
        }
        if (!holds()) return dropped("while it built");
        this.say(projectId, "Downloading…", "info");
        sent = await this.sendDownload(projectId, slot, name);
      }
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
      this.patchRun(projectId, { busy: null });
      if (paused) {
        // The brain terminal again — not behind a card.
        if (this.mayAct()) void this.openBrain();
        else this.brainNote = "The brain terminal was paused for the download; open it again when you are ready.";
        this.changed();
      }
    }
  }

  private async sendDownload(projectId: string, slot: number, name: string):
      Promise<{ terminal: string | null; refused: string }> {
    this.ledger.expect("download", Date.now());
    try {
      const r = await api.vexDownload(projectId, { slot, name });
      return { terminal: isVexId(r?.terminal_id) ? r.terminal_id : null, refused: "" };
    } catch (e: any) {
      this.ledger.withdraw("download");
      return { terminal: null, refused: e?.message || "the download could not start" };
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

  async openBrain(): Promise<void> {
    if (!this.mayAct() || this.brainBusy) return;
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

  async readInfo(): Promise<void> {
    if (!this.mayAct() || this.infoBusy) return;
    this.infoBusy = true;
    this.infoNote = "";
    this.changed();
    this.ledger.expect("info", Date.now());
    try {
      const r = await api.vexInfo();
      this.info = { text: typeof r?.text === "string" ? r.text.slice(0, 64 * 1024) : "",
                    exit_code: typeof r?.exit_code === "number" ? r.exit_code : null };
    } catch (e: any) {
      this.ledger.withdraw("info");
      this.infoNote = e?.message || "the device could not be read";
    } finally {
      this.infoBusy = false;
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
    run: (projectId: string) => {
      const r = vex.run(projectId);
      return { busy: r.busy, note: r.note, build: r.build ? r.build.state : null, downloaded: r.downloaded };
    },
  };
}
