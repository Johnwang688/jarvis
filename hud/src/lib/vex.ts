// The VEX view's rules (VX-2; editor plan §2.9, decisions V-1..V-5), pure, so
// vitest holds them: what the daemon's answers mean, the download name and
// slot as the daemon checks them, which build error opens a file, and what a
// click must be to send code to a robot.
//
// **Download is only the owner's click** (V-2/V-3, plan §2.9: "a keystroke
// should never send code to a robot"). `isExplicitClick` is the one test the
// Download button asks: a trusted pointer click with the primary button —
// never the click a browser makes up for Enter or Space on a focused button
// (`detail` 0), never a script's `el.click()` (untrusted, `detail` 0). There
// is no key and no palette entry for it anywhere.
//
// The daemon decides everything that matters (`jarvis/v2/vex.py`); these
// mirrors only keep the window from asking for what it would refuse, and a
// refusal it sends anyway is shown in its own words.

/** The one command the owner runs once, before anything here works. */
export const SETUP_COMMAND = "jarvis vex setup";

/** The daemon's `DOWNLOAD_NAME`: never starts with `-` (never a flag), nothing a Windows command line misreads. */
export const DOWNLOAD_NAME = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,19}$/;
export const NAME_MAX = 20;
export const SLOTS: readonly number[] = [1, 2, 3, 4, 5, 6, 7, 8];

export type Language = "cpp" | "python";
export type DeviceAction = "download" | "info" | "brain";
/** The daemon's `DEVICE_READS`: `vexcom --json` (system info) or `vexcom --programs`. Both are the `info` action. */
export type DeviceRead = "system" | "programs";

export interface VexStatus {
  configured: boolean;
  /** Why not configured: a sentence ending in what to run. */
  hint: string | null;
  vexcom_version: string | null;
  extension_version: string | null;
  sdks: { cpp: string | null; python: string | null } | null;
  templates: boolean;
  /** Where new projects go (`vex.json`'s `project_folder`, V-4): the New VEX project dialog's default. */
  project_folder: string | null;
  /** The device action running now, if any. */
  device: DeviceAction | "connection" | null;
  /** The brain terminal's id while it is open. */
  brain: string | null;
}

export interface Detection {
  project_id: string;
  vex: boolean;
  name: string;
  language: Language | null;
  slot: number | null;
  sdk_version: string;
  printf_float: boolean;
  python_main: string | null;
  on_windows_drive: boolean;
  /** C++ on a Windows drive: the daemon can build it. */
  builds: boolean;
  /** Not a VEX project, or why the settings file cannot be used. */
  reason: string;
  /** A settings file that is not the extension's v2 shape: reported, never guessed around. */
  malformed: boolean;
  pros: boolean;
}

export interface BuildError {
  file: string;
  line: number;
  col: number;
  severity: "error" | "warning";
  message: string;
  in_project: boolean;
}

export type BuildState = "running" | "succeeded" | "failed" | "timed_out";

export interface BuildView {
  id: string;
  project_id: string;
  action: string;
  state: BuildState;
  terminal_id: string | null;
  exit_code: number | null;
  errors: BuildError[];
  /** Latest plain-text output, bounded by the daemon; never persisted. */
  output: string;
  bin: { path: string; size: number; mtime: string } | null;
  started: string;
  ended: string | null;
}

const ID = /^[0-9a-f]{8}$/;
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown, max = 400): string => (typeof v === "string" ? v.slice(0, max) : "");
const strOrNull = (v: unknown, max = 400): string | null => (typeof v === "string" ? v.slice(0, max) : null);
const int = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);

export function isVexId(v: unknown): v is string {
  return typeof v === "string" && ID.test(v);
}

export function parseStatus(raw: unknown): VexStatus | null {
  if (!isObj(raw)) return null;
  const sdks = isObj(raw.sdks) ? { cpp: strOrNull(raw.sdks.cpp, 100), python: strOrNull(raw.sdks.python, 100) } : null;
  const device = raw.device === "download" || raw.device === "info" || raw.device === "brain" || raw.device === "connection" ? raw.device : null;
  return {
    configured: raw.configured === true,
    hint: strOrNull(raw.hint),
    vexcom_version: strOrNull(raw.vexcom_version, 100),
    extension_version: strOrNull(raw.extension_version, 100),
    sdks,
    templates: raw.templates === true,
    project_folder: strOrNull(raw.project_folder, 1000),
    device,
    brain: isVexId(raw.brain) ? raw.brain : null,
  };
}

export function parseDetection(raw: unknown, projectId: string): Detection {
  const v = isObj(raw) ? raw : {};
  const vex = v.vex === true;
  const language = v.language === "cpp" || v.language === "python" ? v.language : null;
  const slot = int(v.slot);
  return {
    project_id: projectId,
    vex: vex && language !== null && slot !== null && slot >= 1 && slot <= 8,
    name: str(v.name, 200),
    language,
    slot: slot !== null && slot >= 1 && slot <= 8 ? slot : null,
    sdk_version: str(v.sdk_version, 100),
    printf_float: v.printf_float === true,
    python_main: strOrNull(v.python_main, 400),
    on_windows_drive: v.on_windows_drive === true,
    builds: v.builds === true,
    reason: str(v.reason, 400),
    malformed: v.malformed === true,
    pros: v.pros === true,
  };
}

function parseError(raw: unknown): BuildError | null {
  if (!isObj(raw)) return null;
  const line = int(raw.line);
  const col = int(raw.col);
  if (line === null || line < 1) return null;
  return {
    file: str(raw.file, 400),
    line,
    col: col !== null && col >= 1 ? col : 1,
    severity: raw.severity === "warning" ? "warning" : "error",
    message: str(raw.message, 300),
    in_project: raw.in_project === true,
  };
}

const BUILD_STATES: readonly BuildState[] = ["running", "succeeded", "failed", "timed_out"];

export function parseBuild(raw: unknown): BuildView | null {
  if (!isObj(raw) || !isVexId(raw.id)) return null;
  const state = (BUILD_STATES as readonly unknown[]).includes(raw.state) ? (raw.state as BuildState) : null;
  if (!state) return null;
  const bin = isObj(raw.bin) && typeof raw.bin.path === "string"
    ? { path: str(raw.bin.path, 400), size: int(raw.bin.size) ?? 0, mtime: str(raw.bin.mtime, 64) }
    : null;
  const errors = Array.isArray(raw.errors) ? raw.errors.map(parseError).filter((e): e is BuildError => !!e) : [];
  return {
    id: raw.id,
    project_id: str(raw.project_id, 200),
    action: str(raw.action, 20),
    state,
    terminal_id: isVexId(raw.terminal_id) ? raw.terminal_id : null,
    exit_code: int(raw.exit_code),
    errors: errors.slice(0, 200),
    output: typeof raw.output === "string" ? raw.output.slice(-16 * 1024) : "",
    bin,
    started: str(raw.started, 64),
    ended: strOrNull(raw.ended, 64),
  };
}

export interface DownloadView {
  id: string; project_id: string; slot: number; state: BuildState;
  terminal_id: string | null; exit_code: number | null; output: string;
  started: string; ended: string | null;
}

export function parseDownload(raw: unknown): DownloadView | null {
  if (!isObj(raw) || !isVexId(raw.id) || typeof raw.project_id !== "string" || raw.project_id.length > 200
    || !(BUILD_STATES as readonly unknown[]).includes(raw.state)
    || typeof raw.slot !== "number" || !SLOTS.includes(raw.slot)) return null;
  return { id: raw.id, project_id: raw.project_id, slot: raw.slot, state: raw.state as BuildState,
    terminal_id: isVexId(raw.terminal_id) ? raw.terminal_id : null, exit_code: int(raw.exit_code),
    output: typeof raw.output === "string" ? raw.output.slice(-16 * 1024) : "",
    started: str(raw.started, 64), ended: strOrNull(raw.ended, 64) };
}

export interface ConnectionView {
  brain: boolean | null; controller: boolean | null; checked_at: string | null;
  state: "unknown" | "checking" | "ready" | "busy" | "error"; message: string;
  events: { device: "brain" | "controller"; connected: boolean; at: string }[];
}

export function parseConnection(raw: unknown): ConnectionView | null {
  if (!isObj(raw) || !["unknown", "checking", "ready", "busy", "error"].includes(String(raw.state))
    || ![true, false, null].includes(raw.brain as boolean | null)
    || ![true, false, null].includes(raw.controller as boolean | null)
    || !Array.isArray(raw.events) || raw.events.length > 32) return null;
  const events: ConnectionView["events"] = [];
  for (const e of raw.events) {
    if (!isObj(e) || (e.device !== "brain" && e.device !== "controller")
      || typeof e.connected !== "boolean" || typeof e.at !== "string") return null;
    events.push({device: e.device, connected: e.connected, at: e.at.slice(0, 64)});
  }
  return {brain: raw.brain as boolean | null, controller: raw.controller as boolean | null,
    checked_at: strOrNull(raw.checked_at, 64), state: raw.state as ConnectionView["state"],
    message: str(raw.message, 400), events};
}

/**
 * The view strip offers "vex" only for a VEX project — one whose settings
 * file parses, and one whose file is there but cannot be used (malformed, or
 * a PROS project), so the view can say why. Never for an ordinary project.
 */
export function offersVex(d: Detection | null | undefined): boolean {
  return !!d && (d.vex || d.malformed || d.pros);
}

/** The download name's refusal, in the daemon's terms, or null when it would pass. */
export function nameRefusal(name: string): string | null {
  if (!name) return "Give the program a name.";
  if (name.length > NAME_MAX) return `The name can be at most ${NAME_MAX} characters.`;
  if (name.startsWith("-")) return "The name can't start with -.";
  if (!DOWNLOAD_NAME.test(name)) return "The name can hold only letters, digits, _ and -.";
  return null;
}

export function slotRefusal(slot: unknown): string | null {
  return typeof slot === "number" && Number.isInteger(slot) && slot >= 1 && slot <= 8
    ? null : "The slot must be a number from 1 to 8.";
}

/** What the name box starts with: the project's name, made to pass the rule (never starting with -). */
export function defaultName(projectName: string): string {
  const cleaned = (projectName || "").replace(/[^A-Za-z0-9_-]/g, "_").replace(/^-+/, "").slice(0, NAME_MAX);
  return cleaned && DOWNLOAD_NAME.test(cleaned) ? cleaned : "program";
}

/**
 * What Download to slot N does to the brain (plan §2.9). Since the 2026-10-15
 * redesign (owner decision 2) this sentence is the button's hover help, its
 * screen-reader description and the slot popover's warning; the button's own
 * text is the short `downloadButtonLabel`.
 */
export function downloadLabel(slot: number): string {
  return `Download to slot ${slot}: replaces whatever is in slot ${slot} on the brain`;
}

/** The Download button's visible text (owner decision 2, 2026-10-15). */
export function downloadButtonLabel(slot: number): string {
  return `Download to slot ${slot}`;
}

/**
 * How far a download is, 0-100, from vexcom's own progress line, or null
 * before it prints one. The live format (2026-10-14) is a `download :` line,
 * then `0....10....20....` up to `100`: a number every 10% and four dots
 * between, so each dot after the last number is 2%. The last such line
 * counts (a later phase starts again at 0). Output is untrusted text: only a
 * line made of nothing but those numbers and dots is read.
 */
export function downloadProgress(output: string): number | null {
  const lines = (output || "").split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    // A narrow terminal or vexcom itself may break the line: a run of
    // numbers and dot runs (two or more, so "1.0.2" is not one) is read
    // wherever it starts.
    if (!/^\d{1,3}(?:\.{2,}\d{1,3})*\.*$/.test(line) || !line.includes("..")) continue;
    const parts = line.split(/(\.+)/).filter(Boolean);
    let last = parts.length - 1;
    // A trailing number may be half written ("…90....10" on its way to 100):
    // it counts only once dots follow it, or when it is 100. Otherwise the
    // number before it and the dots between are what is known.
    if (/^\d/.test(parts[last]) && parts[last] !== "100") last -= 1;
    if (last < 0) continue;
    const dots = parts[last].startsWith(".") ? parts[last].length : 0;
    const base = dots ? Number(parts[last - 1] ?? 0) : Number(parts[last]);
    return Math.max(0, Math.min(100, base + Math.min(dots, 4) * 2));
  }
  return null;
}

/**
 * Whether a click on the Download button is the owner's own: a trusted
 * pointer click with the primary button, and **the first click of a run**
 * (`detail` exactly 1). Enter or Space on a focused button makes a click with
 * `detail` 0, and so does a script's `el.click()`; neither counts. A double
 * or triple click's later clicks (`detail` 2, 3) do not count either, so a
 * double click is one download, never two (VX-2 review, L3).
 */
export function isExplicitClick(e: { isTrusted: boolean; detail: number; button: number }): boolean {
  return e.isTrusted === true && e.detail === 1 && e.button === 0;
}

/**
 * Whether a press on **Stop** is the owner's own (VEX redesign, owner decision
 * 6): a trusted event — a pointer click with the primary button, or Enter or
 * Space on the focused button (stopping is the safe direction, so the
 * keyboard counts) — and never a script's `el.click()` or a dispatched event
 * (untrusted). Stop only ever stops this window's running clean, build or
 * download; it never sends anything to the brain (no `vexcom --stop`).
 */
export function isTrustedPress(e: { isTrusted: boolean; button: number }): boolean {
  return e.isTrusted === true && e.button === 0;
}

/** VX-1's code on the download's 409 when the program must be built first (`BuildFirst`). */
export const BUILD_FIRST_CODE = "build_first";

/**
 * A download the daemon refused because the program must be built first —
 * read from the refusal's machine-readable `code`, never its wording (VX-1
 * chains Build then Download on the one click). A refusal without the code
 * is shown in its own words and builds nothing.
 */
export function needsBuildFirst(code: unknown): boolean {
  return code === BUILD_FIRST_CODE;
}

/** What the view says when even a rebuild leaves "Build first": the daemon's own sentence, nothing sent. */
export function stillStale(sentence: string): string {
  return `Nothing was downloaded: after a build and a rebuild the daemon still says “${sentence}”. `
    + "The build's terminal shows what make did.";
}

/**
 * The steps of one click's download chain (owner decision 3, 2026-10-15):
 * clean, build, then download — a failed clean or build stops it before
 * anything reaches the robot. `rebuild` happens only when the daemon still
 * says Build first after the build (MED-1); `pause` (the brain terminal let go
 * of the port) is part of the download step.
 */
export type ChainStep = "clean" | "build" | "rebuild" | "download";

/** One click's download chain, as every VEX view shows it (VX-2 review, M1: one per window). */
export interface ChainView {
  label: string;
  slot: number;
  phase: "start" | "clean" | "build" | "pause" | "rebuild" | "download";
  /** The owner pressed Stop. */
  cancelled: boolean;
  /** The steps that have succeeded, in order. */
  done?: ChainStep[];
}

/** The running chain's line, in every VEX view: what it is doing, on which project and slot. */
export function chainLine(c: ChainView): string {
  const who = c.label || "this project";
  if (c.cancelled) return `Stopping the download of ${who}…`;
  switch (c.phase) {
    case "clean":
      return `Cleaning ${who}, then building and downloading to slot ${c.slot}`;
    case "pause":
      return `Pausing the brain terminal, then downloading ${who} to slot ${c.slot}…`;
    case "build":
      return `Building ${who}, then downloading to slot ${c.slot}`;
    case "rebuild":
      return `Rebuilding ${who} from scratch, then downloading to slot ${c.slot}`;
    default:
      return `Downloading ${who} to slot ${c.slot}…`;
  }
}

/** Which step a chain phase belongs to. */
export function chainStepOf(phase: ChainView["phase"]): ChainStep {
  return phase === "start" || phase === "clean" ? "clean"
    : phase === "build" ? "build" : phase === "rebuild" ? "rebuild" : "download";
}

/** The chain's steps as the status line lists them: done, running or still to come. */
export function chainSteps(c: ChainView): { step: ChainStep; state: "done" | "busy" | "todo" }[] {
  const done = c.done ?? [];
  const now = chainStepOf(c.phase);
  const order: ChainStep[] = done.includes("rebuild") || now === "rebuild"
    ? ["clean", "build", "rebuild", "download"] : ["clean", "build", "download"];
  return order.map((step) => ({
    step,
    state: done.includes(step) ? "done" : step === now ? "busy" : "todo",
  }));
}

/**
 * Stop is offered for the whole of a chain (owner decision 6, 2026-10-15): it
 * stops a running clean or build, and **a download that is already being
 * sent** — the owner's call, which replaces VX-2's "never cut off mid-write";
 * the view then says the slot may hold an incomplete program.
 */
export function chainStoppable(c: ChainView): boolean {
  return !c.cancelled;
}

/**
 * What the view says after Stop cut a download off (owner decision 6). The
 * vendor's own extension never cancels a program download (its progress is
 * `cancellable: false`), so nothing says what the brain keeps: honest about
 * not knowing, and what to do.
 */
export function stoppedDownloadNote(slot: number): string {
  return `Download stopped: slot ${slot} may hold an incomplete program, the previous one, or nothing. `
    + "Download again before running it.";
}

/**
 * The file a build error names, to open at its line — only one inside the
 * project, by a plain relative path: never absolute, never a drive, never
 * `..`, never a backslash. Anything else (an SDK header) is text only. A `.`
 * component (`./src/main.cpp`, as a compiler may print it) is dropped, so
 * the path is the one the editor knows the file by.
 */
export function errorTarget(e: BuildError): { path: string; line: number; col: number } | null {
  if (!e.in_project) return null;
  const raw = e.file;
  if (!raw || raw.length > 400 || raw.startsWith("/") || /^[A-Za-z]:/.test(raw) || raw.includes("\\")) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null;
  const parts = raw.split("/").filter((p) => p !== ".");
  if (!parts.length || parts.some((p) => p === "" || p === "..")) return null;
  return { path: parts.join("/"), line: Math.max(1, e.line), col: Math.max(1, e.col) };
}

/** `src/main.cpp:12:5` */
export function errorWhere(e: BuildError): string {
  return `${e.file}:${e.line}:${e.col}`;
}

export interface BuildLine {
  tone: "ok" | "bad" | "busy" | "none";
  text: string;
}

function size(n: number): string {
  return n >= 1024 ? `${Math.round(n / 102.4) / 10} KB` : `${n} B`;
}

/** The one line under Build: ✓, or what went wrong, or that it is running (or that Stop stopped it). */
export function buildLine(b: BuildView | null, stopped = false): BuildLine {
  if (!b) return { tone: "none", text: "Not built in this window yet." };
  const verb = b.action === "clean" ? "Clean" : b.action === "rebuild" ? "Rebuild" : "Build";
  if (stopped && b.state !== "running" && b.state !== "succeeded") {
    return { tone: "bad", text: `✗ ${verb} stopped. Its output so far is in the Build tab.` };
  }
  const errs = b.errors.filter((e) => e.severity === "error").length;
  const warns = b.errors.length - errs;
  const counts = [errs ? `${errs} error${errs === 1 ? "" : "s"}` : "", warns ? `${warns} warning${warns === 1 ? "" : "s"}` : ""]
    .filter(Boolean).join(", ");
  switch (b.state) {
    case "running":
      return { tone: "busy", text: b.action === "clean" ? "Cleaning…" : b.action === "rebuild" ? "Rebuilding…" : "Building…" };
    case "succeeded":
      if (b.action === "clean") return { tone: "ok", text: "✓ Cleaned" };
      return {
        tone: "ok",
        text: `✓ Built${b.bin ? ` ${b.bin.path} (${size(b.bin.size)})` : ""}${warns ? ` · ${counts}` : ""}`,
      };
    case "timed_out":
      return { tone: "bad", text: `✗ The ${b.action} took over 5 minutes and was stopped` };
    default:
      return { tone: "bad", text: `✗ ${b.action === "clean" ? "Clean" : b.action === "rebuild" ? "Rebuild" : "Build"} failed`
        + ` (exit ${b.exit_code ?? "?"})${counts ? `: ${counts}` : ""}` };
  }
}

/** What the view says when `vex.json` is not usable: the daemon's sentence, and always the one command. */
export function setupNote(s: VexStatus | null): { headline: string; detail: string | null } | null {
  if (!s || s.configured) return null;
  const hint = s.hint || "";
  if (!hint || /not set up/i.test(hint)) {
    return { headline: "VEX isn't set up on this machine yet. Run this once in a terminal:", detail: null };
  }
  return { headline: "VEX's setup needs running again. In a terminal:", detail: hint };
}

/**
 * Device actions this window started, matched against the bus's `vex_device`
 * `{action, at}` (VX-1): one this window cannot match to its own click — a
 * forged-Origin POST from a program running as the owner — is said.
 * Expectations are recorded *before* the request goes, since the daemon
 * publishes before it answers.
 */
export class DeviceLedger {
  private expected: { action: DeviceAction; at: number }[] = [];
  constructor(private windowMs = 30_000) {}

  expect(action: DeviceAction, now: number) {
    this.prune(now);
    this.expected.push({ action, at: now });
  }

  /** Withdraw one expectation of `action` (the request failed before the daemon started anything). */
  withdraw(action: DeviceAction) {
    const i = this.expected.findIndex((x) => x.action === action);
    if (i >= 0) this.expected.splice(i, 1);
  }

  /** True when this window started it. */
  heard(action: DeviceAction, now: number): boolean {
    this.prune(now);
    const i = this.expected.findIndex((x) => x.action === action);
    if (i < 0) return false;
    this.expected.splice(i, 1);
    return true;
  }

  private prune(now: number) {
    this.expected = this.expected.filter((x) => now - x.at <= this.windowMs);
  }
}

export function parseDeviceRecord(data: unknown): { action: DeviceAction; at: string } | null {
  if (!isObj(data)) return null;
  const action = data.action;
  if (action !== "download" && action !== "info" && action !== "brain") return null;
  return { action, at: str(data.at, 64) };
}

/** Last build id per project, kept across a reload so the errors list comes back (ids only, never output). */
export const LAST_BUILDS_KEY = "jarvis.hud.vex.builds";

export function parseLastBuilds(raw: string | null): Record<string, string> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    if (!isObj(v)) return {};
    const out: Record<string, string> = {};
    for (const [k, id] of Object.entries(v).slice(0, 50)) {
      if (k && k.length <= 200 && isVexId(id)) out[k] = id;
    }
    return out;
  } catch {
    return {};
  }
}

// -- the redesign's state marks, output tabs and help (VX-UI-1..3, 2026-10-15) --

/**
 * A step's state mark (plan §3.1). Text always goes with the colour. **Amber
 * is never a VEX result**: in Graphite it means an approval card holds the
 * window, and only the status line's "Held" says that.
 */
export type MarkState = "idle" | "busy" | "ok" | "failed" | "stale" | "unknown";

export interface Mark {
  state: MarkState;
  /** What a screen reader hears, and the mark's tooltip: "Build: failed (2 errors)". */
  text: string;
  /** Why a ✓ is stale, in the words the status line shows beside it: "edited since", "from an older build". */
  detail?: string;
}

/** The daemon's `SOURCE_SUFFIXES` and makefile names (`vex.is_source`): an edit to one makes a build stale. */
const SOURCE_SUFFIXES = [".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++", ".inl",
  ".ipp", ".tpp", ".inc", ".s", ".mk"];

export function isSourcePath(path: string): boolean {
  const name = (path.split("/").pop() || "").toLowerCase();
  if (name === "makefile" || name === "gnumakefile") return true;
  const dot = name.lastIndexOf(".");
  return dot > 0 && SOURCE_SUFFIXES.includes(name.slice(dot));
}

const at = (iso: string | null | undefined): number => {
  const t = Date.parse(iso ?? "");
  return Number.isFinite(t) ? t : NaN;
};

/**
 * Clean's, Build's or Rebuild's mark, from its own last record in this window.
 * Stale is **advisory only** (never disables anything; the daemon's freshness
 * rule decides a download): a build or rebuild that succeeded and was followed
 * by a source edited in the editor, or by a clean. Unknown: still running by
 * its last record, but no longer followed (status checks gave up).
 */
export function buildMark(kind: "clean" | "build" | "rebuild", o: {
  record: BuildView | null; starting: boolean; following: boolean; stopped: boolean;
  editedAt: number | null; cleanedAt: number | null;
}): Mark {
  const name = kind === "clean" ? "Clean" : kind === "rebuild" ? "Rebuild" : "Build";
  const r = o.record;
  if (o.starting || (r?.state === "running" && o.following)) return { state: "busy", text: `${name}: running` };
  if (!r) return { state: "idle", text: `${name}: not run in this window` };
  if (r.state === "running") return { state: "unknown", text: `${name}: outcome unknown, check status` };
  const errs = r.errors.filter((e) => e.severity === "error").length;
  if (r.state !== "succeeded") {
    const why = o.stopped ? "stopped" : r.state === "timed_out" ? "timed out"
      : `failed${errs ? ` (${errs} error${errs === 1 ? "" : "s"})` : ""}`;
    return { state: "failed", text: `${name}: ${why}` };
  }
  const ended = at(r.ended);
  if (kind !== "clean" && Number.isFinite(ended)) {
    if (o.editedAt !== null && o.editedAt > ended) {
      return { state: "stale", text: `${name}: succeeded, edited since`, detail: "edited since" };
    }
    if (o.cleanedAt !== null && o.cleanedAt > ended) {
      return { state: "stale", text: `${name}: succeeded, cleaned since`, detail: "cleaned since" };
    }
  }
  return { state: "ok", text: `${name}: succeeded` };
}

/**
 * Download's mark: running while this project's chain runs; ✓ in the slot
 * (stale — "older build" — once a later build succeeded); ✗ failed, timed out
 * or stopped; ? when the outcome was lost.
 */
export function downloadMark(o: {
  busy: boolean; downloaded: { slot: number; ok: boolean | null; stopped?: boolean } | null;
  endedAt: number | null; builtAt: number | null;
}): Mark {
  if (o.busy) return { state: "busy", text: "Download: running" };
  const d = o.downloaded;
  if (!d) return { state: "idle", text: "Download: not run in this window" };
  if (d.ok === null) return { state: "unknown", text: "Download: outcome unknown, check status" };
  if (!d.ok) return { state: "failed", text: d.stopped ? `Download: stopped, slot ${d.slot} may be incomplete`
    : "Download: failed" };
  if (o.endedAt !== null && o.builtAt !== null && o.builtAt > o.endedAt) {
    return { state: "stale", text: `Download: in slot ${d.slot}, from an older build`, detail: "from an older build" };
  }
  return { state: "ok", text: `Download: in slot ${d.slot}` };
}

/** The steps that carry a mark: the action bar's four buttons. */
export type ActionKind = "clean" | "build" | "rebuild" | "download";

/** What `actionState` reads of a project's run in this window (state/vex.ts `ProjectRun`). */
export interface ActionRun {
  build: BuildView | null;
  results: Partial<Record<"clean" | "build" | "rebuild", BuildView>>;
  startingBuild: { action: "clean" | "build" | "rebuild" } | null;
  stoppedBuild: string | null;
  editedAt: number | null;
  busy: "build" | "download" | null;
  download: { ended: string | null } | null;
  downloaded: { slot: number; ok: boolean | null; stopped?: boolean } | null;
}

const okAt = (r: BuildView | undefined): number => (r?.state === "succeeded" ? at(r.ended) : NaN);

/**
 * One step's state (plan §3.1, VX-UI-2), from its project's run alone:
 * idle, busy, ok, failed, stale or unknown, with words. `following` is
 * whether this window still follows a running build (`vex.monitoringBuild`);
 * a running record no one follows is **unknown**, never a spinner. Stale is
 * advisory (a source saved through the editor, or a clean, after Build's ✓; a
 * later build after Download's ✓) and disables nothing: the daemon's
 * freshness rule alone decides a download. **Never amber** — no state means
 * "held by a card"; only the status line says that.
 */
export function actionState(kind: ActionKind, run: ActionRun, following: boolean): Mark {
  if (kind === "download") {
    const built = Math.max(-Infinity, ...[okAt(run.results.build), okAt(run.results.rebuild)].filter(Number.isFinite));
    const ended = at(run.download?.ended);
    return downloadMark({
      busy: run.busy === "download", downloaded: run.downloaded,
      endedAt: Number.isFinite(ended) ? ended : null, builtAt: Number.isFinite(built) ? built : null,
    });
  }
  const record = run.results[kind] ?? null;
  const cleaned = okAt(run.results.clean);
  return buildMark(kind, {
    record, starting: run.startingBuild?.action === kind, following,
    stopped: !!run.stoppedBuild && record?.id === run.stoppedBuild,
    editedAt: run.editedAt, cleanedAt: Number.isFinite(cleaned) ? cleaned : null,
  });
}

/**
 * An output tab's badge (plan §2.5), the same state as its step's mark: the
 * Build tab follows the latest clean, build or rebuild (whichever it shows),
 * the Download tab the Download step. Its words follow the tab's name: "Build,
 * failed (1 error)", "Build, clean: succeeded", "Download, nothing yet".
 */
export function tabBadge(tab: "build" | "download", run: ActionRun, following: boolean): Mark {
  // With no current record (the daemon forgot it: a 404 clears `build`), the latest one kept for a step,
  // so the tab says what that step's mark says (review LOW).
  const shown = run.startingBuild?.action ?? run.build?.action ?? latestResult(run)?.action;
  const kind: ActionKind | null = tab === "download" ? "download"
    : shown === "clean" || shown === "build" || shown === "rebuild" ? shown : null;
  if (!kind) return { state: "idle", text: "nothing yet" };
  const m = actionState(kind, run, following);
  if (m.state === "idle") return { state: "idle", text: "nothing yet" };
  const rest = m.text.replace(/^[A-Za-z]+: /, "");
  return { ...m, text: kind === tab ? rest : `${kind}: ${rest}` };
}

/**
 * The Device tab's badge (plan §2.5): the worst confirmed USB state, and **?
 * when the reading is stale or unknown** (paused, old, erroring: what was last
 * confirmed may no longer hold). USB presence is not readiness: nothing reads
 * this to disable anything.
 */
export function deviceBadge(c: { brain: boolean | null; controller: boolean | null } | null, stale: boolean): Mark {
  if (!c || (c.brain == null && c.controller == null)) return { state: "unknown", text: "USB unknown" };
  if (stale) return { state: "unknown", text: "USB reading stale, last confirmed shown in the tab" };
  if (c.brain === false && c.controller === false) return { state: "failed", text: "nothing on USB" };
  return { state: "ok", text: "USB device present" };
}

/** The latest of a run's kept clean, build and rebuild records (by start). */
function latestResult(run: ActionRun): BuildView | null {
  let best: BuildView | null = null;
  for (const r of [run.results.clean, run.results.build, run.results.rebuild]) {
    if (r && (!best || at(r.started) > at(best.started))) best = r;
  }
  return best;
}

/**
 * The status line's words for stale marks (mockup state 6: "Build: ✓ edited
 * since"). A dimmed ✓ alone would say it in colour; this says it in words
 * where the owner looks. Only the **latest successful** of Build and Rebuild
 * is said (an older one was superseded by it, so its staleness is not news;
 * review LOW), then Download. Nothing while something runs (`active`: a
 * chain or a build), when the line says that instead.
 */
export function staleNotes(run: ActionRun, following: boolean, active: boolean)
  : { kind: ActionKind; name: string; detail: string }[] {
  if (active) return [];
  const NAME: Record<ActionKind, string> = { clean: "Clean", build: "Build", rebuild: "Rebuild", download: "Download" };
  const b = okAt(run.results.build), rb = okAt(run.results.rebuild);
  const built: ActionKind | null = !Number.isFinite(rb) ? (Number.isFinite(b) ? "build" : null)
    : !Number.isFinite(b) || rb > b ? "rebuild" : "build";
  const kinds: ActionKind[] = built ? [built, "download"] : ["download"];
  return kinds.map((kind) => ({ kind, mark: actionState(kind, run, following) }))
    .filter((m) => m.mark.state === "stale")
    .map((m) => ({ kind: m.kind, name: NAME[m.kind], detail: m.mark.detail ?? "stale" }));
}

/** The glyph a mark draws beside its words (never colour alone). */
export function markGlyph(state: MarkState): string {
  return state === "ok" || state === "stale" ? "✓" : state === "failed" ? "✗" : state === "unknown" ? "?" : "";
}

/** The output area's tabs (plan §2.5). The Brain tab holds the brain terminal (VX-UI-4a, `brainView`). */
export type OutputTab = "build" | "download" | "brain" | "device";
export const OUTPUT_TABS: readonly OutputTab[] = ["build", "download", "brain", "device"];

/** The tab an action the owner starts selects (background events never switch tabs). */
export function outputTabFor(action: "clean" | "build" | "rebuild" | "download" | "info" | "programs" | "usb" | "brain"): OutputTab {
  switch (action) {
    case "clean": case "build": case "rebuild": return "build";
    case "download": return "download";
    case "brain": return "brain";
    default: return "device";
  }
}

/** Arrow keys, Home and End in a tablist. */
export function nextTab(tabs: readonly OutputTab[], current: OutputTab, key: string): OutputTab | null {
  const i = tabs.indexOf(current);
  if (key === "ArrowRight") return tabs[(i + 1) % tabs.length];
  if (key === "ArrowLeft") return tabs[(i - 1 + tabs.length) % tabs.length];
  if (key === "Home") return tabs[0];
  if (key === "End") return tabs[tabs.length - 1];
  return null;
}

/** Every control's hover help (permanent: owner decision 7, 2026-10-15), and the `?` list's sentences. */
export type HelpId = "clean" | "build" | "rebuild" | "download" | "stop" | "slot" | "more" | "usb" | "help"
  | "tab-build" | "tab-download" | "tab-brain" | "tab-device" | "fold" | "max" | "connect" | "disconnect"
  | "terminal" | "recheck-build" | "recheck-download" | "usb-refresh";

export function helpText(id: HelpId, slot: number): string {
  switch (id) {
    case "clean": return "Deletes this project's build folder (make clean). The next Build compiles everything.";
    case "build": return "Compiles the project (make). Errors appear under the status line; click one to open the file at that line.";
    case "rebuild": return "Compiles every file again from scratch (make -B), even the ones make thinks are up to date.";
    case "download": return `Cleans, builds, then sends the program to the brain, by USB or through the controller. `
      + `${downloadLabel(slot)}. A failed clean or build stops before anything is sent. `
      + "Starting the program stays at the robot. Mouse click only.";
    case "stop": return "Stops the running clean, build or download. A download stopped part way can leave the slot "
      + "incomplete. It never stops the program on the brain.";
    case "slot": return `Which slot (1–8) and name the program gets on the brain. Download to slot ${slot} replaces `
      + `whatever is in slot ${slot}.`;
    case "more": return "Device reads: the brain's system info (vexcom --json) and the programs on it (vexcom --programs).";
    case "usb": return "USB presence only: plugged in or not. Not the radio link, and not whether a program is running. "
      + "Click for the Device tab.";
    case "help": return "Lists what each control does. Escape or ? again closes it.";
    case "tab-build": return "The latest clean or build's output, live.";
    case "tab-download": return "The latest download's output and progress, live.";
    case "tab-brain": return "What the program on the brain prints (printf / print), live; what you type goes to it. "
      + "It connects only when you click Connect, never by itself.";
    case "tab-device": return "USB status, its connection log, and what the device reads answered.";
    case "fold": return "Folds the output away (the tabs and their marks stay).";
    case "max": return "Gives the output the whole pane; again to restore.";
    case "connect": return "Opens the link to the program on the brain (vexcom --user, through the controller or "
      + "USB): what it prints shows here, and what you type goes to it. It holds the robot's port while open, so "
      + "a Download pauses it and you click Connect again after. It never opens by itself.";
    case "disconnect": return "Closes the link and frees the robot's port. The program on the brain keeps running; "
      + "Connect opens the link again.";
    case "terminal": return "Opens this action's program terminal in the bottom panel: its full output. It runs nothing.";
    case "recheck-build": return "Status checks for this build stopped. This asks Jarvis again and follows the build to "
      + "its end. It never starts or resends anything.";
    case "recheck-download": return "Status checks for this download stopped. This asks Jarvis once how it ended. It "
      + "never resends the download.";
    case "usb-refresh": return "Asks now for Jarvis's latest USB presence check (vexcom --usb). It runs at most every 5 "
      + "seconds, so the reading may be up to 5 s old, or older after errors (the tab says when it was last "
      + "confirmed). It writes nothing to the robot.";
  }
}

/**
 * The Brain tab (VX-UI-4a, plan §5.5): the brain terminal — the same `vexcom
 * --user` session, "Jarvis can read" switch and Ctrl-C rule as anywhere —
 * drawn in the VEX pane. **It connects only on the owner's Connect** (owner
 * decision 5, 2026-10-15): never when the tab is shown, never on a reload,
 * never after a download (the download closes it; the tab then offers
 * Connect again). A link the daemon already holds open (a reload, another
 * window) is only shown, never opened.
 */
export type BrainState =
  | "unconfigured" | "off" | "connecting" | "connected" | "paused" | "ended" | "error" | "elsewhere" | "taken"
  | "lost";

export interface BrainInput {
  configured: boolean;
  blocked: boolean;
  /** A download chain runs (any project); `paused`: it closed the brain terminal for the download. */
  chain: { paused: boolean } | null;
  /** The brain terminal this window shows, while it exists (listed, or its session ended here). */
  terminal: string | null;
  /** Connect's request is on its way. */
  asking: boolean;
  /** The terminal session's state in this window, if it has one (components/Terminal.tsx `SessionState`). */
  session: string | null;
  /** Its exit code, once its program ended. */
  exitCode: number | null;
  /** Shown somewhere else: a terminal pane, another VEX pane's Brain tab, or another window. */
  shownIn: { kind: "pane" | "vex"; slot: number } | { kind: "window" } | null;
  /** The daemon's refusal of the last Connect, in its words. */
  error: string;
  /** Why a download closed it and it was not opened again. */
  closed: string;
  /** The device action holding the port now (`/vex/status` `device`). */
  device: string | null;
  /** A device read (⋯) on its way from this window. */
  reading: boolean;
}

export interface BrainView {
  state: BrainState;
  /** What the tab says, in words (never colour alone). */
  text: string;
  /** Connect is offered; `off` says why it is disabled ("" when it is not). */
  connect: { shown: boolean; off: string };
  /** Disconnect is offered (`close`: the program already ended, so it only clears the terminal away). */
  disconnect: { shown: boolean; close: boolean };
  /** The terminal view is drawn in the tab. */
  terminal: boolean;
  /**
   * Offered instead of Connect when this window's socket gave up (`lost`):
   * attach again (`TermSession.retry`). The link itself may still be open, so
   * nothing is asked of the device.
   */
  reconnect: boolean;
  /** The tab's badge. */
  badge: { state: MarkState; text: string };
}

/** The terminal itself is gone: Jarvis restarted (`ended`) or it was closed. */
const BRAIN_GONE = new Set(["ended", "closed"]);

export function brainView(i: BrainInput): BrainView {
  const offWhy = !i.configured ? "VEX is not set up yet: run jarvis vex setup."
    : i.blocked ? "Off under an approval card."
      : i.chain ? "A download holds the robot's port; Connect again once it ends."
        : i.asking ? "Connecting…"
          : i.reading || i.device === "info" ? "A device read holds the robot's port; Connect once it ends."
            : i.device === "download" ? "A download holds the robot's port; Connect once it ends."
              : "";
  const view = (state: BrainState, text: string, o: Partial<Omit<BrainView, "state" | "text">> = {}): BrainView => ({
    state, text,
    connect: o.connect ?? { shown: false, off: "" },
    disconnect: o.disconnect ?? { shown: false, close: false },
    terminal: o.terminal ?? false,
    reconnect: o.reconnect ?? false,
    badge: o.badge ?? { state: "idle", text: "not connected" },
  });
  const connect = { shown: true, off: offWhy };
  if (!i.configured) {
    return view("unconfigured", "VEX is not set up yet: run jarvis vex setup, then Connect.", { connect });
  }
  if (i.chain?.paused) {
    return view("paused", "Paused for the download: it needs the robot's port. When the download ends, click "
      + "Connect to open the link again (it never reopens by itself).",
    { connect, badge: { state: "unknown", text: "paused for the download" } });
  }
  if (i.asking && !i.terminal) {
    return view("connecting", "Opening the link to the brain…", { badge: { state: "busy", text: "connecting" } });
  }
  if (i.terminal) {
    const gone = i.session !== null && BRAIN_GONE.has(i.session);
    if (i.session === "exited" || gone) {
      const why = i.session === "exited"
        ? `The link ended (vexcom exited${i.exitCode !== null ? ` with code ${i.exitCode}` : ""}). `
          + "Its last output is below. Connect opens a new link."
        : "The link is gone (Jarvis restarted, or it was closed). Connect opens a new link.";
      return view("ended", why, { connect, disconnect: { shown: true, close: true }, terminal: true,
        badge: { state: "failed", text: "ended" } });
    }
    if (i.shownIn && i.shownIn.kind !== "window") {
      const where = i.shownIn.kind === "pane" ? `pane ${i.shownIn.slot}` : `the VEX view in pane ${i.shownIn.slot}`;
      return view("elsewhere", `The brain terminal is shown in ${where}.`, {
        disconnect: { shown: true, close: false }, badge: { state: "ok", text: `live, shown in ${where}` } });
    }
    // Another window's say, or this window's socket: each in its own words, never a spinner that never ends.
    const live = { disconnect: { shown: true, close: false }, terminal: true };
    if (i.session === "taken") {
      return view("taken", "Taken by another window: it shows the brain terminal now. Take it back (below) asks "
        + "for it again.", { ...live, badge: { state: "ok", text: "live, taken by another window" } });
    }
    if (i.session === "refused") {
      return view("taken", "The window showing the brain terminal kept it. Ask again (below) asks once more.",
        { ...live, badge: { state: "ok", text: "live, kept by another window" } });
    }
    if (i.session === "waiting") {
      return view("connecting", "Asking the window that shows the brain terminal to let it go…",
        { ...live, badge: { state: "busy", text: "asking another window" } });
    }
    if (i.session === "lost") {
      // Its retries ran out; the link may still be open, so Disconnect (never "Close") names what it does.
      return view("lost", "The connection to the brain terminal was lost; the link itself may still be open. "
        + "Reconnect attaches again; Disconnect closes the link.",
      { ...live, reconnect: true, badge: { state: "unknown", text: "connection lost" } });
    }
    if (i.shownIn?.kind === "window") {
      return view("elsewhere", "The brain terminal is shown in another window. Show it here asks that window first.",
        { disconnect: { shown: true, close: false }, terminal: true,
          badge: { state: "ok", text: "live, in another window" } });
    }
    if (i.session !== "attached") {
      return view("connecting", "Connecting to the brain terminal…", { disconnect: { shown: true, close: false },
        terminal: true, badge: { state: "busy", text: "connecting" } });
    }
    return view("connected", "Live: what the program prints. Typing goes to the program; Ctrl-C too.",
      { disconnect: { shown: true, close: false }, terminal: true, badge: { state: "ok", text: "live" } });
  }
  if (i.error) {
    return view("error", i.error, { connect, badge: { state: "failed", text: "could not connect" } });
  }
  return view("off", i.closed || "Not connected. Connect opens the link to the program on the brain: what it "
    + "prints shows here. It never connects by itself.", { connect });
}
