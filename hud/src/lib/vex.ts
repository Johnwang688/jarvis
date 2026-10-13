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
  device: DeviceAction | null;
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
  const device = raw.device === "download" || raw.device === "info" || raw.device === "brain" ? raw.device : null;
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

/** The Download button says what it does (plan §2.9). */
export function downloadLabel(slot: number): string {
  return `Download to slot ${slot}: replaces whatever is in slot ${slot} on the brain`;
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

/**
 * What one click does on "Build first", in order (VX-2 delta review, MED-1):
 * a build; and if the daemon still says Build first — make found nothing to
 * do, while a header it does not list changed — one rebuild (make -B). Never
 * a third: the click's chain then stops and says so.
 */
export const BUILD_FIRST_STEPS: readonly ("build" | "rebuild")[] = ["build", "rebuild"];

/** What the view says when even a rebuild leaves "Build first": the daemon's own sentence, nothing sent. */
export function stillStale(sentence: string): string {
  return `Nothing was downloaded: after a build and a rebuild the daemon still says “${sentence}”. `
    + "The build's terminal shows what make did.";
}

/** One click's download chain, as every VEX view shows it (VX-2 review, M1: one per window). */
export interface ChainView {
  label: string;
  slot: number;
  phase: "start" | "pause" | "build" | "rebuild" | "download";
  cancelled: boolean;
}

/** The running chain's line, in every VEX view: what it is doing, on which project and slot. */
export function chainLine(c: ChainView): string {
  const who = c.label || "this project";
  if (c.cancelled) return `Cancelling the download of ${who}…`;
  switch (c.phase) {
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

/** Cancel stops a chain until its download has been sent; a download in flight is never cut off mid-write. */
export function chainCancellable(c: ChainView): boolean {
  return !c.cancelled && c.phase !== "download";
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

/** The one line under Build: ✓, or what went wrong, or that it is running. */
export function buildLine(b: BuildView | null): BuildLine {
  if (!b) return { tone: "none", text: "Not built in this window yet." };
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
