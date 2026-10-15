// The VEX view (VX-2; editor plan §2.9): a pane that shows one VEX V5
// project — its card (name, language, slot, SDK), Build, the errors list,
// Download, the brain terminal and device info. The build's output and the
// brain's user port are program terminals in the WP-D panel (VX-1 runs them;
// `terminals.showProgram` puts them there), so they get xterm's colours,
// scrollback and reattach after a reload.
//
// The rules this view keeps:
//   - **Download is only a click on the labelled button** ("Download to slot
//     3: replaces whatever is in slot 3 on the brain"). The button asks
//     `isExplicitClick` — Enter or Space on it, or a script's `.click()`, is
//     not the owner's click — takes no focus from a pointer and is out of the
//     Tab order. No key and no palette entry downloads.
//   - **Under an approval card every control here is disabled** (and inert
//     behind the veil), and the store refuses too, so nothing builds or
//     downloads behind the card; a download's chain a card came into is
//     dropped (state/vex.ts).
//   - **One download chain per window** (review M1): while one runs, for any
//     project, every VEX view says what it is doing, with a Cancel that stops
//     its build and never sends its download, and no other download, brain
//     terminal or device read starts here.
//   - Device info and every build error are the program's text, drawn as
//     text: markup in them is characters.
//   - With no `vex.json`, the view says the one command to run.

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { vex } from "../state/vex";
import { terminals } from "./Terminal";
import {
  SETUP_COMMAND, SLOTS, buildLine, chainCancellable, chainLine, defaultName, downloadLabel, downloadProgress, errorTarget, errorWhere,
  isExplicitClick, nameRefusal, setupNote, type BuildError, type Detection,
} from "../lib/vex";

/** A press on a button whose second press matters never takes focus from a pointer. */
const noPointerFocus = (e: React.MouseEvent) => e.preventDefault();

export function VexView(props: {
  pane: number;
  drawn?: boolean;
  projectId: string | null;
  projectName: string;
  /** An approval card is up: every control is disabled. */
  blocked: boolean;
  /** Open a build error's file at its line in the editor (ED-2's buffers). */
  onOpenError: (path: string, line: number, col: number) => void;
}) {
  useSyncExternalStore(vex.subscribe, vex.getVersion);
  const pid = props.projectId;
  const blocked = props.blocked;

  // Fresh on showing: the detection and the status (the owner may have run
  // setup, or the settings file changed).
  useEffect(() => {
    if (!pid) return;
    vex.want(pid, true);
    void vex.refreshStatus();
  }, [pid]);
  useEffect(() => {
    const again = () => {
      if (document.visibilityState !== "visible" || !pid) return;
      vex.want(pid, true);
      void vex.refreshStatus();
    };
    window.addEventListener("focus", again);
    return () => window.removeEventListener("focus", again);
  }, [pid]);
  // Setup is often run in one of the HUD's own terminals, where the window
  // never loses focus: while it is not set up, the status is asked again.
  const unconfigured = !!vex.status && !vex.status.configured;
  useEffect(() => {
    if (!unconfigured) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void vex.refreshStatus();
    }, 4000);
    return () => clearInterval(t);
  }, [unconfigured]);

  useEffect(() => {
    if (props.drawn === false || blocked) return;
    const check = () => void vex.refreshConnection();
    check();
    const timer = setInterval(check, 1000);
    return () => clearInterval(timer);
  }, [props.drawn, blocked]);

  const chain = <ChainBar pane={props.pane} blocked={blocked} />;
  if (!pid) {
    return (
      <div className="scroll vexview" data-testid={`vex-view-${props.pane}`} data-project="">
        {chain}
        <div className="pad muted" data-testid="vex-none">Pick a project in this pane&rsquo;s header.</div>
      </div>
    );
  }
  const d = vex.detection(pid);
  const err = vex.detectionError(pid);
  if (!d) {
    return (
      <div className="scroll vexview" data-testid={`vex-view-${props.pane}`} data-project={pid}>
        {chain}
        <div className="pad muted" data-testid="vex-loading">
          {err ? <span className="err">{err}</span> : "Reading the project's VEX settings…"}
        </div>
      </div>
    );
  }
  return (
    <div className="scroll vexview" data-testid={`vex-view-${props.pane}`} data-project={pid}>
      {chain}
      <Body d={d} pid={pid} name={props.projectName} blocked={blocked} onOpenError={props.onOpenError} />
    </div>
  );
}

/** The window's running download chain, in every VEX view, whatever project it shows (M1). */
function ChainBar(props: { pane: number; blocked: boolean }) {
  const c = vex.chain;
  if (!c) return null;
  return (
    <div className="vexnote info vexchain" data-testid="vex-chain" data-project={c.projectId} data-phase={c.phase}
         role="status">
      <span data-testid="vex-chain-line">{chainLine(c)}</span>
      {chainCancellable(c) ? (
        <button type="button" className="quiet" data-testid="vex-chain-cancel" disabled={props.blocked}
                title="Stops the build; nothing is downloaded" onClick={() => vex.cancelChain()}>
          Cancel
        </button>
      ) : null}
    </div>
  );
}

function Body(props: {
  d: Detection; pid: string; name: string; blocked: boolean;
  onOpenError: (path: string, line: number, col: number) => void;
}) {
  const { d, pid, blocked } = props;
  if (d.pros) {
    return <div className="pad" data-testid="vex-pros">PROS projects aren't supported. Open it in VS Code with PROS.</div>;
  }
  if (!d.vex) {
    return (
      <div className="pad" data-testid={d.malformed ? "vex-malformed" : "vex-not"}>
        {d.malformed
          ? <>This project's VEX settings file can't be used: <span data-testid="vex-reason">{d.reason}</span></>
          : <>{props.name} is not a VEX project: <span data-testid="vex-reason">{d.reason}</span></>}
      </div>
    );
  }
  const setup = setupNote(vex.status);
  const run = vex.run(pid);
  return (
    <>
      <Card d={d} usbOff={blocked || !!setup || !!vex.chain} />
      {setup ? (
        <div className="vexsetup" data-testid="vex-setup" role="status">
          <div>{setup.headline}</div>
          <code className="vexcmd" data-testid="vex-setup-command">{SETUP_COMMAND}</code>
          {setup.detail ? <div className="muted small" data-testid="vex-setup-detail">{setup.detail}</div> : null}
          <div className="muted small">
            This view checks again by itself every few seconds.{" "}
            <button type="button" className="quiet" data-testid="vex-setup-recheck" disabled={blocked}
                    onClick={() => void vex.refreshStatus()}>
              Check now
            </button>
          </div>
        </div>
      ) : vex.statusError ? (
        <div className="pad err small" data-testid="vex-status-error">{vex.statusError}</div>
      ) : null}
      <BuildAndDownload key={pid} d={d} pid={pid} label={props.name} blocked={blocked} configured={!setup}
                        onOpenError={props.onOpenError} />
      <DeviceSection blocked={blocked} configured={!setup} />
      {run.note ? (
        <div className={"vexnote " + run.noteTone} data-testid="vex-note" role="status">{run.note}</div>
      ) : null}
      {vex.notices.map((n) => (
        <div key={n.key} className="vexnote bad" data-testid="vex-device-notice" role="status">
          A VEX {n.action === "info" ? "device-info read" : n.action === "brain" ? "brain terminal" : "download"} started
          that this window did not start{n.at ? ` (${n.at})` : ""}.
          <button type="button" className="quiet" disabled={blocked} onClick={() => vex.dismissNotice(n.key)}>dismiss</button>
        </div>
      ))}
    </>
  );
}

function Card(props: { d: Detection; usbOff: boolean }) {
  const { d } = props;
  return (
    <div className="vexcard" data-testid="vex-card">
      <div className="vexcardtop">
        <div>
          <div className="vexname" data-testid="vex-name">{d.name}</div>
          <div className="vexfacts muted small">
            <span data-testid="vex-language">{d.language === "cpp" ? "C++" : "Python"}</span>
            <span> · slot <span data-testid="vex-slot">{d.slot}</span></span>
            {d.sdk_version ? <span> · SDK {d.sdk_version}</span> : null}
          </div>
        </div>
        {vex.status?.configured ? <UsbBadges off={props.usbOff} /> : null}
      </div>
      {d.language === "cpp" && !d.on_windows_drive ? (
        <div className="vexnote bad small" data-testid="vex-not-windows">
          This project is not on a Windows drive (/mnt/c/…): VEX's toolchain builds only there.
        </div>
      ) : null}
    </div>
  );
}

/** Build and Download for one project. The slot and name live here so the
 * Download button in the build row and the one under the pickers send the
 * same thing; `key={pid}` starts them afresh for another project. */
function BuildAndDownload(props: {
  d: Detection; pid: string; label: string; blocked: boolean; configured: boolean;
  onOpenError: (path: string, line: number, col: number) => void;
}) {
  const { d } = props;
  const [slot, setSlot] = useState<number>(d.slot ?? 1);
  const [name, setName] = useState<string>(() => defaultName(d.name));
  // A project whose settings change its slot: the picker starts from the new one.
  useEffect(() => setSlot(d.slot ?? 1), [d.slot]);
  const send: DownloadTarget = { d, pid: props.pid, label: props.label, blocked: props.blocked,
                                 configured: props.configured, slot, name };
  return <>
    <BuildSection d={d} pid={props.pid} blocked={props.blocked} configured={props.configured}
                  onOpenError={props.onOpenError} send={send} />
    <DownloadSection {...send} setSlot={setSlot} setName={setName} />
  </>;
}

type DownloadTarget = { d: Detection; pid: string; label: string; blocked: boolean; configured: boolean;
                        slot: number; name: string };

/**
 * The Download button, in the build row (`compact`) or under the pickers.
 * Both are the same button: out of the Tab order, never focused by a press,
 * and only the owner's own first primary click sends (plan §2.9, L3: a
 * keystroke or a script never sends code to a robot).
 */
function DownloadButton(props: DownloadTarget & { compact?: boolean }) {
  const { d, pid, slot, name } = props;
  const run = vex.run(pid);
  // One chain per window: no second download while one runs, for any project.
  const other = vex.chain && vex.chain.projectId !== pid ? vex.chain : null;
  const off = props.blocked || !props.configured || !d.builds || !!run.busy || !!nameRefusal(name) || !!vex.chain;
  const why = other ? `A download is running for ${other.label || "another project"}`
    : "Builds first if the program is older than its sources. Starting it stays on the brain or the controller.";
  return (
    <button
      type="button"
      className={props.compact ? "" : "vexdownload"}
      data-testid={props.compact ? "vex-download-quick" : "vex-download-btn"}
      tabIndex={-1}
      onMouseDown={noPointerFocus}
      disabled={off}
      title={props.compact ? `${downloadLabel(slot)} as "${name}". ${why}` : why}
      onClick={(e) => {
        if (!isExplicitClick({ isTrusted: e.nativeEvent.isTrusted, detail: e.detail, button: e.button })) return;
        if (props.blocked) return;
        void vex.download(pid, slot, name, props.label);
      }}
    >
      {run.busy === "download" ? "Downloading…" : props.compact ? `Download to slot ${slot}` : downloadLabel(slot)}
    </button>
  );
}

function BuildSection(props: {
  d: Detection; pid: string; blocked: boolean; configured: boolean;
  onOpenError: (path: string, line: number, col: number) => void;
  send: DownloadTarget;
}) {
  const { d, pid, blocked } = props;
  const run = vex.run(pid);
  const active = !!run.startingBuild || run.build?.state === "running";
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  if (d.language !== "cpp") {
    return <section className="vexsec"><h4>Build</h4><div className="muted small">A Python project has nothing to build.</div></section>;
  }
  const off = blocked || !props.configured || !d.builds || !!run.busy || active;
  const action = run.startingBuild?.action ?? (active ? run.build?.action : null);
  const line = run.startingBuild
    ? { tone: "busy", text: `Starting ${run.startingBuild.action}…` } : buildLine(run.build);
  const errors = run.startingBuild ? [] : run.build?.errors ?? [];
  const started = run.startingBuild?.started ?? Date.parse(run.build?.started ?? "");
  const ended = active ? now : Date.parse(run.build?.ended ?? "");
  const seconds = Number.isFinite(started) && Number.isFinite(ended)
    ? Math.max(0, Math.floor((ended - started) / 1000)) : null;
  const elapsed = seconds === null ? "" : seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const build = run.startingBuild ? null : run.build;
  return (
    <section className="vexsec" data-testid="vex-build" aria-busy={active}>
      <h4>Build</h4>
      <div className="vexrow">
        <button type="button" data-testid="vex-build-btn" disabled={off} onClick={() => void vex.build(pid, "build")}>
          {action === "build" ? "Building…" : "Build"}
        </button>
        <button type="button" data-testid="vex-rebuild-btn" disabled={off} onClick={() => void vex.build(pid, "rebuild")}>
          {action === "rebuild" ? "Rebuilding…" : "Rebuild"}
        </button>
        <button type="button" data-testid="vex-clean-btn" disabled={off} onClick={() => void vex.build(pid, "clean")}>
          {action === "clean" ? "Cleaning…" : "Clean"}
        </button>
        <DownloadButton {...props.send} compact />
        <span className={"vexstate " + line.tone} data-testid="vex-build-state"
              role="status" data-state={run.startingBuild ? "starting" : run.build?.state ?? "none"}>
          {active ? <span className="vexspinner" aria-hidden="true" /> : null}{line.text}
        </span>
        {elapsed ? <span className="muted small" data-testid="vex-build-elapsed">{elapsed}</span> : null}
        {build?.terminal_id ? (
          <button type="button" className="quiet" data-testid="vex-build-output-btn" disabled={blocked}
                  onClick={() => void terminals.showProgram(build.terminal_id!)}>Show terminal</button>
        ) : null}
        {build?.state === "running" && !vex.monitoringBuild(pid) ? (
          <button type="button" className="quiet" data-testid="vex-build-recheck" disabled={blocked}
                  onClick={() => void vex.refreshBuild(pid)}>Check status</button>
        ) : null}
      </div>
      {run.buildNote ? <div className="err small" role="alert" data-testid="vex-build-note">{run.buildNote}</div> : null}
      {active && !build?.output ? (
        <div className="muted small" data-testid="vex-build-waiting">
          {run.startingBuild ? "Waiting for the build tools to start…"
            : "Waiting for tool output. This action is stopped automatically after 5 minutes."}
        </div>
      ) : null}
      {build?.output ? <BuildOutput output={build.output} /> : null}
      {build && !active && !build.output && build.state !== "succeeded" ? (
        <div className="muted small">No output preview is available. Open the terminal for full output.</div>
      ) : null}
      {errors.length ? (
        <ul className="vexerrors" data-testid="vex-errors">
          {errors.map((e, i) => <ErrorRow key={i} e={e} i={i} blocked={blocked} onOpen={props.onOpenError} />)}
        </ul>
      ) : null}
    </section>
  );
}

function BuildOutput(props: { output: string; download?: boolean }) {
  const ref = useRef<HTMLPreElement>(null);
  const following = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && following.current) el.scrollTop = el.scrollHeight;
  }, [props.output]);
  return (
    <pre ref={ref} className="vexoutput" data-testid={props.download ? "vex-download-output" : "vex-build-output"} aria-label={props.download ? "Download output" : "Build output"} tabIndex={0}
         onScroll={() => {
           const el = ref.current;
           if (el) following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
         }}>{props.output}</pre>
  );
}

function ErrorRow(props: { e: BuildError; i: number; blocked: boolean; onOpen: (p: string, l: number, c: number) => void }) {
  const { e } = props;
  const target = errorTarget(e);
  const body = (
    <>
      <span className={"vexsev " + e.severity}>{e.severity}</span>
      <span className="vexwhere">{errorWhere(e)}</span>
      <span className="vexmsg">{e.message}</span>
    </>
  );
  return (
    <li>
      {target ? (
        <button type="button" className="vexerr" data-testid={`vex-error-${props.i}`} data-file={target.path}
                data-line={target.line} disabled={props.blocked}
                title={`Open ${target.path} at line ${target.line}`}
                onClick={() => props.onOpen(target.path, target.line, target.col)}>
          {body}
        </button>
      ) : (
        <div className="vexerr outside" data-testid={`vex-error-${props.i}`} title="Outside the project: not opened here">
          {body}
        </div>
      )}
    </li>
  );
}

function DownloadSection(props: DownloadTarget & { setSlot: (slot: number) => void; setName: (name: string) => void }) {
  const { d, pid, blocked, slot, name, setSlot, setName } = props;
  const run = vex.run(pid);
  const [now, setNow] = useState(Date.now());
  const active = run.busy === "download";
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const started = run.downloadStarted;
  const end = active ? now : Date.parse(run.download?.ended ?? "");
  const elapsed = started !== null && Number.isFinite(end) ? `${Math.max(0, Math.floor((end - started) / 1000))}s` : "";
  // vexcom's own progress line, while it runs; full once it has succeeded.
  const progress = run.download?.state === "succeeded" ? 100
    : active ? downloadProgress(run.download?.output ?? "") : null;
  if (d.language !== "cpp") {
    return (
      <section className="vexsec" data-testid="vex-download">
        <h4>Download</h4>
        <div className="muted small">Downloading a Python project waits on a setup check; use VS Code for now.</div>
      </section>
    );
  }
  const refusal = nameRefusal(name);
  return (
    <section className="vexsec" data-testid="vex-download">
      <h4>Download</h4>
      <div className="vexrow">
        <label className="small">
          Slot{" "}
          <select data-testid="vex-slot-pick" value={slot} disabled={blocked || !!run.busy}
                  onChange={(e) => setSlot(Number(e.target.value))}>
            {SLOTS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="small">
          Name{" "}
          <input data-testid="vex-name-input" value={name} maxLength={40} spellCheck={false} disabled={blocked || !!run.busy}
                 onChange={(e) => setName(e.target.value)} />
        </label>
      </div>
      {refusal ? <div className="err small" data-testid="vex-name-refusal">{refusal}</div> : null}
      <DownloadButton {...props} />
      <div className="vexrow" role="status" data-testid="vex-download-state" aria-busy={active}>
        {active ? <><span className="vexspinner" aria-hidden="true" /><span>Download in progress…</span></> : null}
        {elapsed ? <span className="muted small" data-testid="vex-download-elapsed">{elapsed}</span> : null}
        {run.download?.terminal_id ? <button type="button" className="quiet" disabled={blocked}
          data-testid="vex-download-output-btn" onClick={() => void terminals.showProgram(run.download!.terminal_id!)}>Show terminal</button> : null}
        {run.download && run.download.state === "running" && !active ? <button type="button" className="quiet"
          disabled={blocked} data-testid="vex-download-recheck" onClick={() => void vex.refreshDownload(pid)}>Check status</button> : null}
      </div>
      {progress !== null ? (
        <div className="vexprogress" data-testid="vex-download-progress" role="progressbar" aria-label="Download progress"
             aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
          <span className="vexprogress-track">
            <span className={"vexprogress-fill" + (progress === 100 && !active ? " ok" : "")} style={{ width: `${progress}%` }} />
          </span>
          <span className="vexprogress-pct">{progress}%</span>
        </div>
      ) : null}
      {run.downloadNote ? <div className="err small" role="alert" data-testid="vex-download-note">{run.downloadNote}</div> : null}
      {active && !run.download?.output ? <div className="muted small">Waiting for Download output…</div> : null}
      {run.download?.output ? <BuildOutput output={run.download.output} download /> : null}
      {run.downloaded ? (
        <div className={"small " + (run.downloaded.ok ? "ok" : "err")} data-testid="vex-downloaded">
          {run.downloaded.ok === true ? `✓ In slot ${run.downloaded.slot}`
            : run.downloaded.ok === null ? "Download outcome unknown — check status or terminal."
              : `Download ${run.download?.state === "timed_out" ? "timed out" : "failed"} (exit ${run.downloaded.code ?? "?"})`}
        </div>
      ) : null}
    </section>
  );
}

function DeviceSection(props: { blocked: boolean; configured: boolean }) {
  const { blocked } = props;
  const brain = vex.status?.brain ?? null;
  // A download's chain holds the device: no brain terminal or device read meanwhile.
  const off = blocked || !props.configured || !!vex.chain;
  return (
    <section className="vexsec" data-testid="vex-device">
      <h4>Device connection</h4>
      <ConnectionStatus blocked={off} />
      <h4>Brain</h4>
      <div className="vexrow">
        <button type="button" data-testid="vex-brain-btn" disabled={off || vex.brainBusy}
                title="The brain's user port: printf and print output, and what you type goes to the program"
                onClick={() => void vex.openBrain()}>
          {brain ? "Show the brain terminal" : "Open the brain terminal"}
        </button>
        <button type="button" data-testid="vex-info-btn" disabled={off || !!vex.infoBusy}
                title="The brain's system info (vexcom --json)"
                onClick={() => void vex.readInfo("system")}>
          {vex.infoBusy === "system" ? "Reading…" : "Read device info"}
        </button>
        <button type="button" data-testid="vex-programs-btn" disabled={off || !!vex.infoBusy}
                title="The programs on the brain (vexcom --programs)"
                onClick={() => void vex.readInfo("programs")}>
          {vex.infoBusy === "programs" ? "Reading…" : "List programs"}
        </button>
      </div>
      {vex.brainNote ? <div className="err small" data-testid="vex-brain-note">{vex.brainNote}</div> : null}
      {vex.infoNote ? <div className="err small" data-testid="vex-info-note">{vex.infoNote}</div> : null}
      {vex.info ? (
        <div className="small" data-testid="vex-info-what" data-read={vex.info.read}>
          {vex.info.read === "programs" ? "Programs on the brain" : "Device info"}
          {vex.info.exit_code ? ` (vexcom exited ${vex.info.exit_code})` : ""}
        </div>
      ) : null}
      {vex.info ? <pre className="vexinfo" data-testid="vex-info-text">{vex.info.text}</pre> : null}
    </section>
  );
}

/** The USB reading as both indicators show it: the last confirmed values, and
 * whether they are stale (paused, older than 12 s, not "ready", or erroring). */
function useUsbReading(off: boolean) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const c = vex.connection;
  const paused = off || document.visibilityState !== "visible" || vex.blocked || vex.crashed || !!vex.chain;
  const age = now - Date.parse(c?.checked_at ?? "");
  const stale = paused || !Number.isFinite(age) || age > 12_000 || c?.state !== "ready" || !!vex.connectionError;
  const label = (value: boolean | null | undefined) => value == null ? "Unknown" : value ? "Connected" : "Disconnected";
  // Green connected, red disconnected, plain unknown; a stale reading keeps its
  // colour, dimmed, beside "(last confirmed)".
  const tone = (value: boolean | null | undefined) =>
    "vexusb " + (value == null ? "muted" : value ? "ok" : "bad") + (stale && value != null ? " stale" : "");
  return { c, paused, stale, label, tone };
}

/** The same reading, small, in the project card: no scrolling to the device
 * section to see whether the brain or controller is plugged in. */
function UsbBadges(props: { off: boolean }) {
  const { c, stale, label, tone } = useUsbReading(props.off);
  const at = c?.checked_at ? ` · checked ${new Date(c.checked_at).toLocaleTimeString()}` : "";
  const badge = (name: string, testid: string, value: boolean | null | undefined) => {
    const text = `${name}: ${label(value)}${stale && value != null ? " (last confirmed)" : ""}`;
    return <span className={tone(value) + " vexbadge"} data-testid={testid} title={`${text} (USB)${at}`} aria-label={text}>
      <span className="vexdot" aria-hidden="true" />{name}
    </span>;
  };
  return <div className="vexbadges" role="status" data-testid="vex-usb-badges">
    {badge("Brain", "vex-badge-brain", c?.brain)}
    {badge("Controller", "vex-badge-controller", c?.controller)}
  </div>;
}

function ConnectionStatus(props: {blocked: boolean}) {
  const { c, paused, stale, label, tone } = useUsbReading(props.blocked);
  return <div data-testid="vex-connection">
    <div className="small" data-testid="vex-tooling">Tooling: {vex.status?.configured ? "Configured" : "Not configured"}</div>
    <div className="vexrow" role="status">
      <span data-testid="vex-usb-brain" className={tone(c?.brain)}>Brain: {label(c?.brain)}{stale && c?.brain != null ? " (last confirmed)" : ""}</span>
      <span data-testid="vex-usb-controller" className={tone(c?.controller)}>Controller: {label(c?.controller)}{stale && c?.controller != null ? " (last confirmed)" : ""}</span>
      <button type="button" className="quiet" disabled={props.blocked} data-testid="vex-connection-refresh"
        onClick={() => void vex.refreshConnection()}>Refresh USB status</button>
    </div>
    <div className="muted small" data-testid="vex-connection-note">{paused ? "USB checks paused; showing last confirmed state." : stale && c?.state === "ready" ? "USB reading is stale; awaiting a fresh check." : vex.connectionError || c?.message || "USB devices have not been checked."}
      {c?.checked_at ? ` Last confirmed: ${new Date(c.checked_at).toLocaleTimeString()}.` : ""}</div>
    <div className="muted small">USB presence only; radio link and extension health are not measured.</div>
    {c?.events.length ? <details><summary className="small">Connection log</summary>
      <ul className="small" data-testid="vex-connection-log">{c.events.map((e, i) => <li key={i}>
        <time dateTime={e.at}>{new Date(e.at).toLocaleTimeString()}</time>{` ${e.device === "brain" ? "Brain" : "Controller"} ${e.connected ? "connected" : "disconnected"}`}
      </li>)}</ul></details> : null}
  </div>;
}
