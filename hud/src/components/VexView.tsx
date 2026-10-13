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

import { useEffect, useState, useSyncExternalStore } from "react";
import { vex } from "../state/vex";
import {
  SETUP_COMMAND, SLOTS, buildLine, chainCancellable, chainLine, defaultName, downloadLabel, errorTarget, errorWhere,
  isExplicitClick, nameRefusal, setupNote, type BuildError, type Detection,
} from "../lib/vex";

/** A press on a button whose second press matters never takes focus from a pointer. */
const noPointerFocus = (e: React.MouseEvent) => e.preventDefault();

export function VexView(props: {
  pane: number;
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
      <Card d={d} />
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
      <BuildSection d={d} pid={pid} blocked={blocked} configured={!setup} onOpenError={props.onOpenError} />
      <DownloadSection key={pid} d={d} pid={pid} label={props.name} blocked={blocked} configured={!setup} />
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

function Card(props: { d: Detection }) {
  const { d } = props;
  return (
    <div className="vexcard" data-testid="vex-card">
      <div className="vexname" data-testid="vex-name">{d.name}</div>
      <div className="vexfacts muted small">
        <span data-testid="vex-language">{d.language === "cpp" ? "C++" : "Python"}</span>
        <span> · slot <span data-testid="vex-slot">{d.slot}</span></span>
        {d.sdk_version ? <span> · SDK {d.sdk_version}</span> : null}
      </div>
      {d.language === "cpp" && !d.on_windows_drive ? (
        <div className="vexnote bad small" data-testid="vex-not-windows">
          This project is not on a Windows drive (/mnt/c/…): VEX's toolchain builds only there.
        </div>
      ) : null}
    </div>
  );
}

function BuildSection(props: {
  d: Detection; pid: string; blocked: boolean; configured: boolean;
  onOpenError: (path: string, line: number, col: number) => void;
}) {
  const { d, pid, blocked } = props;
  const run = vex.run(pid);
  if (d.language !== "cpp") {
    return <section className="vexsec"><h4>Build</h4><div className="muted small">A Python project has nothing to build.</div></section>;
  }
  const off = blocked || !props.configured || !d.builds || !!run.busy;
  const line = buildLine(run.build);
  const errors = run.build && run.build.state !== "running" ? run.build.errors : [];
  return (
    <section className="vexsec" data-testid="vex-build">
      <h4>Build</h4>
      <div className="vexrow">
        <button type="button" data-testid="vex-build-btn" disabled={off} onClick={() => void vex.build(pid, "build")}>
          {run.busy === "build" ? "Building…" : "Build"}
        </button>
        <button type="button" data-testid="vex-rebuild-btn" disabled={off} onClick={() => void vex.build(pid, "rebuild")}>
          Rebuild
        </button>
        <button type="button" data-testid="vex-clean-btn" disabled={off} onClick={() => void vex.build(pid, "clean")}>
          Clean
        </button>
        <span className={"vexstate " + line.tone} data-testid="vex-build-state"
              data-state={run.build?.state ?? "none"}>{line.text}</span>
      </div>
      {errors.length ? (
        <ul className="vexerrors" data-testid="vex-errors">
          {errors.map((e, i) => <ErrorRow key={i} e={e} i={i} blocked={blocked} onOpen={props.onOpenError} />)}
        </ul>
      ) : null}
    </section>
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

function DownloadSection(props: { d: Detection; pid: string; label: string; blocked: boolean; configured: boolean }) {
  const { d, pid, blocked } = props;
  const run = vex.run(pid);
  const [slot, setSlot] = useState<number>(d.slot ?? 1);
  const [name, setName] = useState<string>(() => defaultName(d.name));
  // A project whose settings change its slot: the picker starts from the new one.
  useEffect(() => setSlot(d.slot ?? 1), [d.slot]);
  if (d.language !== "cpp") {
    return (
      <section className="vexsec" data-testid="vex-download">
        <h4>Download</h4>
        <div className="muted small">Downloading a Python project waits on a setup check; use VS Code for now.</div>
      </section>
    );
  }
  const refusal = nameRefusal(name);
  // One chain per window: no second download while one runs, for any project.
  const other = vex.chain && vex.chain.projectId !== pid ? vex.chain : null;
  const off = blocked || !props.configured || !d.builds || !!run.busy || !!refusal || !!vex.chain;
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
      <button
        type="button"
        className="vexdownload"
        data-testid="vex-download-btn"
        // Out of the Tab order and never focused by a press: no key ever
        // lands on it (plan §2.9: a keystroke never sends code to a robot).
        tabIndex={-1}
        onMouseDown={noPointerFocus}
        disabled={off}
        title={other ? `A download is running for ${other.label || "another project"}`
          : "Builds first if the program is older than its sources. Starting it stays on the brain or the controller."}
        onClick={(e) => {
          if (!isExplicitClick({ isTrusted: e.nativeEvent.isTrusted, detail: e.detail, button: e.button })) return;
          if (props.blocked) return;
          void vex.download(pid, slot, name, props.label);
        }}
      >
        {run.busy === "download" ? "Downloading…" : downloadLabel(slot)}
      </button>
      {run.downloaded ? (
        <div className={"small " + (run.downloaded.ok ? "ok" : "err")} data-testid="vex-downloaded">
          {run.downloaded.ok ? `✓ In slot ${run.downloaded.slot}` : `✗ Not downloaded (exit ${run.downloaded.code ?? "?"})`}
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
