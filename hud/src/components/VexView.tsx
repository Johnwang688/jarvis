// The VEX view (VX-2; editor plan §2.9; redesigned 2026-10-15, plan
// docs/plans/2026-10-15-vex-ui-redesign.md, VX-UI-1..3): a pane that shows
// one VEX V5 project, top to bottom —
//
//   - a one-line header: name, language, the slot chip (`Slot 3 · override ▾`,
//     a popover holding the slot and name), the Brain/Controller USB dots
//     (a button to the Device tab) and `?` (help mode);
//   - the action bar: Clean › Build, Rebuild › Download to slot N, and ⋯
//     (the device reads) — all visible (owner decision 3), each with a state
//     mark beside it (✓ ✗ ? spinner, "stale" dimmed; never colour alone);
//   - one status line: the running chain step by step, or the running
//     build, or the last result in words, with **Stop** while something runs;
//     refusals in the daemon's words on alert lines under it;
//   - the errors list (click opens the file at its line);
//   - one output area with Build / Download / Brain / Device tabs, foldable
//     and maximisable. The Brain tab (VX-UI-4a) draws the brain terminal
//     itself — the same `vexcom --user` session, "Jarvis can read" switch and
//     Ctrl-C rule as in the panel — at the `vex-<pane>` place.
//
// Every control has permanent hover help (owner decision 7), shown on hover
// and on keyboard focus and named by `aria-describedby`; `?` lists them all.
//
// The rules this view keeps:
//   - **Download is only a click on its button** ("Download to slot 3"; "…
//     replaces whatever is in slot 3 on the brain" is its hover help, its
//     description and the slot popover's warning). The button asks
//     `isExplicitClick` — Enter or Space on it, a script's `.click()` or a
//     double click's later clicks are not the owner's click — takes no focus
//     from a pointer and is out of the Tab order. No key and no palette entry
//     downloads; there is one Download button.
//   - **Stop** (owner decision 6) stops this window's running clean, build or
//     download — only on a trusted press (`isTrustedPress`: a pointer, or
//     Enter/Space; never a script). It closes the action's program terminal
//     and never sends anything to the brain (no `vexcom --stop`).
//   - **The brain's link opens only on Connect** (owner decision 5): a
//     trusted press, as Stop's; never the Brain tab being shown, a reload or
//     the end of a download. Off under a card, during a download and while a
//     device read holds the port. Disconnect closes it (the port is freed).
//   - **Under an approval card every control here is disabled** (and inert
//     behind the veil), menus and popovers close, the status line says Held
//     in amber, and the store refuses too.
//   - **One download chain per window** (review M1): while one runs, for any
//     project, every VEX view's status line says what it is doing, with Stop.
//   - Device info and every build error are the program's text, drawn as
//     text: markup in them is characters.
//   - With no `vex.json`, the view says the one command to run instead of
//     the action bar.

import { createContext, useContext, useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { DEVICE_POLL_MS, vex } from "../state/vex";
import { TerminalView, terminals, useTerminals } from "./Terminal";
import {
  OUTPUT_TABS, SETUP_COMMAND, SLOTS, actionState, brainView, buildLine, chainLine, chainSteps, defaultName, deviceBadge,
  downloadButtonLabel, downloadLabel, downloadProgress, errorTarget, errorWhere, helpText, isExplicitClick, isTrustedPress,
  markGlyph, nameRefusal, nextTab, outputTabFor, setupNote, staleNotes, tabBadge, type ActionKind, type BrainView,
  type BuildError, type Detection, type HelpId, type Mark, type OutputTab,
} from "../lib/vex";
import type { PaneNo } from "../lib/workspace";

/** A press on a button whose second press matters never takes focus from a pointer. */
const noPointerFocus = (e: React.MouseEvent) => e.preventDefault();

/** The output area's per-pane choices: kept while the window lives (not across a reload). */
interface PaneUi { tab: OutputTab; folded: boolean; max: boolean }
const paneUi = new Map<number, PaneUi>();
const DEFAULT_UI: PaneUi = { tab: "build", folded: false, max: false };

function usePaneUi(pane: number): [PaneUi, (patch: Partial<PaneUi>) => void] {
  const [ui, setUi] = useState<PaneUi>(() => paneUi.get(pane) ?? DEFAULT_UI);
  const set = (patch: Partial<PaneUi>) => setUi((u) => {
    const next = { ...u, ...patch };
    paneUi.set(pane, next);
    return next;
  });
  return [ui, set];
}

/** Now, every second while `active`. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function elapsedText(from: number, to: number): string {
  if (!Number.isFinite(from) || !Number.isFinite(to)) return "";
  const s = Math.max(0, Math.floor((to - from) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

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
  // A download or device read the daemon says holds the port, with no chain
  // of this window's to see it end (another window's, or a read of status
  // that raced the end): asked again until it says the port is free, so
  // Connect is offered without a focus or a reload (2026-10-17). A read only.
  const held = !vex.chain && (vex.status?.device === "download" || vex.status?.device === "info");
  useEffect(() => {
    if (!held) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void vex.refreshStatus();
    }, DEVICE_POLL_MS);
    return () => clearInterval(t);
  }, [held]);

  useEffect(() => {
    if (props.drawn === false || blocked) return;
    const check = () => void vex.refreshConnection();
    check();
    const timer = setInterval(check, 1000);
    return () => clearInterval(timer);
  }, [props.drawn, blocked]);

  const cls = "scroll vexview" + (blocked ? " held" : "");
  if (!pid) {
    return (
      <div className={cls} data-testid={`vex-view-${props.pane}`} data-project="">
        <StatusLine pane={props.pane} pid={null} d={null} blocked={blocked} />
        <div className="pad muted" data-testid="vex-none">Pick a project in this pane&rsquo;s header.</div>
      </div>
    );
  }
  const d = vex.detection(pid);
  const err = vex.detectionError(pid);
  if (!d) {
    return (
      <div className={cls} data-testid={`vex-view-${props.pane}`} data-project={pid}>
        <StatusLine pane={props.pane} pid={null} d={null} blocked={blocked} />
        <div className="pad muted" data-testid="vex-loading">
          {err ? <span className="err">{err}</span> : "Reading the project's VEX settings…"}
        </div>
      </div>
    );
  }
  return (
    <Body key={pid} pane={props.pane} d={d} pid={pid} name={props.projectName} blocked={blocked}
          drawn={props.drawn !== false} onOpenError={props.onOpenError} />
  );
}

/** The window's unclaimed device actions (a forged-Origin canary): above everything, red, never folded. */
function Notices(props: { blocked: boolean }) {
  return <>
    {vex.notices.map((n) => (
      <div key={n.key} className="vexnote bad" data-testid="vex-device-notice" role="status">
        A VEX {n.action === "info" ? "device-info read" : n.action === "brain" ? "brain terminal" : "download"} started
        that this window did not start{n.at ? ` (${n.at})` : ""}.
        <button type="button" className="quiet" disabled={props.blocked} onClick={() => vex.dismissNotice(n.key)}>dismiss</button>
      </div>
    ))}
  </>;
}

function Body(props: {
  pane: number; d: Detection; pid: string; name: string; blocked: boolean; drawn: boolean;
  onOpenError: (path: string, line: number, col: number) => void;
}) {
  const { d, pid, blocked, pane } = props;
  const [ui, setUi] = usePaneUi(pane);
  const [help, setHelp] = useState(false);
  const [slot, setSlot] = useState<number>(d.slot ?? 1);
  const [name, setName] = useState<string>(() => defaultName(d.name));
  // A project whose settings change its slot: the picker starts from the new one.
  useEffect(() => setSlot(d.slot ?? 1), [d.slot]);
  // The chain this pane's own click started: its tab follows its steps.
  const mine = useRef<number | null>(null);
  const chain = vex.chain;
  useEffect(() => {
    if (!chain || chain.seq !== mine.current) return;
    const tab = chain.phase === "download" || chain.phase === "pause" ? "download" : "build";
    if (ui.tab !== tab) setUi({ tab });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chain?.seq, chain?.phase]);
  // Help mode closes under a card, and on Escape.
  useEffect(() => { if (blocked) setHelp(false); }, [blocked]);
  useEffect(() => {
    if (!help) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setHelp(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [help]);

  const view = (inner: ReactNode) => (
    <HelpMode.Provider value={help && !blocked}>
      <div className={"scroll vexview" + (blocked ? " held" : "") + (ui.max && !ui.folded ? " outmax" : "")
                      + (help && !blocked ? " helpmode" : "")}
           data-testid={`vex-view-${pane}`} data-project={pid}>
        <Notices blocked={blocked} />
        {inner}
      </div>
    </HelpMode.Provider>
  );
  if (d.pros) {
    return view(<>
      <StatusLine pane={pane} pid={null} d={null} blocked={blocked} />
      <div className="pad" data-testid="vex-pros">PROS projects aren't supported. Open it in VS Code with PROS.</div>
    </>);
  }
  if (!d.vex) {
    return view(<>
      <StatusLine pane={pane} pid={null} d={null} blocked={blocked} />
      <div className="pad" data-testid={d.malformed ? "vex-malformed" : "vex-not"}>
        {d.malformed
          ? <>This project's VEX settings file can't be used: <span data-testid="vex-reason">{d.reason}</span></>
          : <>{props.name} is not a VEX project: <span data-testid="vex-reason">{d.reason}</span></>}
      </div>
    </>);
  }
  const setup = setupNote(vex.status);
  const tip = (id: HelpId) => `vex-${pane}-tip-${id}`;
  const select = (tab: OutputTab) => setUi({ tab, folded: false });
  return view(<>
    <Header pane={pane} d={d} blocked={blocked} slot={slot} name={name} setSlot={setSlot} setName={setName}
            usbOff={blocked || !!setup || !!vex.chain} help={help} setHelp={setHelp} tip={tip}
            onUsb={() => select("device")} />
    {d.language === "cpp" && !d.on_windows_drive ? (
      <div className="vexnote bad small" data-testid="vex-not-windows">
        This project is not on a Windows drive (/mnt/c/…): VEX's toolchain builds only there.
      </div>
    ) : null}
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
    ) : (
      <ActionBar d={d} pid={pid} label={props.name} blocked={blocked} slot={slot} name={name} tip={tip}
                 onAction={(a) => select(outputTabFor(a))}
                 onDownload={() => { select("build"); mine.current = vex.chain?.seq ?? null; }} />
    )}
    <StatusLine pane={pane} pid={pid} d={d} blocked={blocked} />
    <Alerts pid={pid} />
    <ErrorsList pid={pid} blocked={blocked} onOpen={props.onOpenError} />
    <Output pane={pane} d={d} pid={pid} blocked={blocked} configured={!setup} ui={ui} setUi={setUi} help={help}
            slot={slot} name={name} tip={tip} drawn={props.drawn} />
  </>);
}

// -- help -----------------------------------------------------------------------------

/**
 * A control with its permanent hover help (owner decision 7): the sentence
 * shows after a short hover and on keyboard focus (`:focus-visible`, so a
 * click's focus does not), and the control names it with
 * `aria-describedby` (passed to the child as `id`). Not under a card.
 */
function Tip(props: { id: string; text: string; children: ReactNode; end?: boolean; help?: HelpId }) {
  // A press hides it until the pointer leaves, so the help never sits over
  // the status line the click just changed.
  const [pressed, setPressed] = useState(false);
  return (
    <span className={"vextipwrap" + (props.end ? " end" : "") + (pressed ? " pressed" : "")}
          onPointerDown={() => setPressed(true)} onPointerLeave={() => setPressed(false)}>
      <HelpNum id={props.help ?? props.id.replace(/^vex-\d+-tip-/, "") as HelpId} />
      {props.children}
      <span className="vextip" role="tooltip" id={props.id} data-testid={props.id.replace(/^vex-\d+-/, "vex-")}>
        {props.text}
      </span>
    </span>
  );
}

const HELP_ORDER: { id: HelpId; label: string }[] = [
  { id: "clean", label: "Clean" }, { id: "build", label: "Build" }, { id: "rebuild", label: "Rebuild" },
  { id: "download", label: "Download" }, { id: "stop", label: "Stop" }, { id: "slot", label: "Slot and name" },
  { id: "more", label: "⋯" }, { id: "usb", label: "Brain · Controller" }, { id: "help", label: "?" },
  { id: "tab-build", label: "Build tab" },
  { id: "tab-download", label: "Download tab" }, { id: "tab-brain", label: "Brain tab" },
  { id: "connect", label: "Connect (Brain tab)" }, { id: "disconnect", label: "Disconnect (Brain tab)" },
  { id: "tab-device", label: "Device tab" }, { id: "fold", label: "▾" }, { id: "max", label: "⤢" },
  { id: "terminal", label: "Show terminal" }, { id: "recheck-build", label: "Check status (build)" },
  { id: "recheck-download", label: "Check status (download)" },
  { id: "usb-refresh", label: "Refresh USB status (Device tab)" },
];

/** Help mode (`?`) is on, and no card holds the window: each control shows its number in the list. */
const HelpMode = createContext(false);

/**
 * In help mode, a control's number in the `?` list (plan §2.6, mockup state
 * 1): the list says what each number does, so no callout covers another.
 * Decorative only (`aria-hidden`, never focusable): the control's own
 * `aria-describedby` already carries its sentence.
 */
function HelpNum(props: { id: HelpId }) {
  const on = useContext(HelpMode);
  const n = HELP_ORDER.findIndex((h) => h.id === props.id) + 1;
  if (!on || !n) return null;
  return <span className="vexhelpnum" aria-hidden="true" data-testid={`vex-helpnum-${props.id}`}>{n}</span>;
}

function HelpList(props: { slot: number }) {
  return (
    <ol className="vexhelplist" data-testid="vex-help-list" aria-label="What each control does">
      {HELP_ORDER.map((h, i) => (
        <li key={h.id} data-help={h.id} data-n={i + 1}><b>{h.label}</b>: {helpText(h.id, props.slot)}</li>
      ))}
    </ol>
  );
}

// -- header ---------------------------------------------------------------------------

function Header(props: {
  pane: number; d: Detection; blocked: boolean; slot: number; name: string;
  setSlot: (n: number) => void; setName: (s: string) => void; usbOff: boolean;
  help: boolean; setHelp: (on: boolean) => void; tip: (id: HelpId) => string; onUsb: () => void;
}) {
  const { d, tip } = props;
  return (
    <div className="vexhead" data-testid="vex-card">
      <span className="vexname" data-testid="vex-name">{d.name}</span>
      <span className="vexfacts muted small" data-testid="vex-language"
            title={d.sdk_version ? `SDK ${d.sdk_version}` : undefined}>{d.language === "cpp" ? "C++" : "Python"}</span>
      {d.language === "cpp" ? <SlotChip {...props} /> : null}
      <span className="vexhead-right">
        {vex.status?.configured ? (
          <Tip id={tip("usb")} text={helpText("usb", props.slot)} end>
            <button type="button" className="vexusbbtn" data-testid="vex-usb-open" disabled={props.blocked}
                    aria-describedby={tip("usb")} onClick={props.onUsb}>
              <UsbBadges off={props.usbOff} />
            </button>
          </Tip>
        ) : null}
        <Tip id={tip("help")} text={helpText("help", props.slot)} end>
          <button type="button" className={"vexhelp" + (props.help ? " on" : "")} data-testid="vex-help-toggle"
                  aria-pressed={props.help} aria-label="Help: what each control does" aria-describedby={tip("help")}
                  disabled={props.blocked} onClick={() => props.setHelp(!props.help)}>?</button>
        </Tip>
      </span>
    </div>
  );
}

/** `Slot 3 · override ▾`: a disclosure holding the slot select and the name box (plan §2.2, §9). */
function SlotChip(props: {
  pane: number; d: Detection; blocked: boolean; slot: number; name: string;
  setSlot: (n: number) => void; setName: (s: string) => void; tip: (id: HelpId) => string;
}) {
  const { blocked, slot, name, tip } = props;
  const [open, setOpen] = useState(false);
  const chip = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLSpanElement>(null);
  const pick = useRef<HTMLSelectElement>(null);
  const popId = useId();
  const run = vex.run(props.d.project_id);
  const refusal = nameRefusal(name);
  // A card closes it (and the chip is off under it).
  useEffect(() => { if (blocked) setOpen(false); }, [blocked]);
  useEffect(() => { if (open) pick.current?.focus(); }, [open]);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    // Escape closes it wherever focus is (the select is off while an action runs).
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); chip.current?.focus(); } };
    window.addEventListener("mousedown", away, true);
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("mousedown", away, true); window.removeEventListener("keydown", esc); };
  }, [open]);
  const close = () => { setOpen(false); chip.current?.focus(); };
  return (
    <span className="vexchipwrap" ref={box}>
      <Tip id={tip("slot")} text={helpText("slot", slot)}>
        <button type="button" ref={chip} className={"vexchip" + (refusal ? " bad" : "")} data-testid="vex-slot-chip"
                aria-expanded={open} aria-controls={popId} aria-describedby={tip("slot")} disabled={blocked}
                aria-label={`Slot ${slot}, name ${name || "none"}${refusal ? ` (${refusal})` : ""}`}
                onClick={() => setOpen(!open)}>
          Slot <b data-testid="vex-slot">{slot}</b> · {name || "?"} {open ? "▴" : "▾"}
        </button>
      </Tip>
      {open ? (
        <span className="vexpop" id={popId} role="group" aria-label="Slot and name" data-testid="vex-slot-pop"
              onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } }}>
          <label className="small">
            Slot{" "}
            <select ref={pick} data-testid="vex-slot-pick" value={slot} disabled={blocked || !!run.busy}
                    onChange={(e) => props.setSlot(Number(e.target.value))}>
              {SLOTS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="small">
            Name{" "}
            <input data-testid="vex-name-input" value={name} maxLength={40} spellCheck={false}
                   disabled={blocked || !!run.busy} onChange={(e) => props.setName(e.target.value)} />
          </label>
          {refusal ? <span className="err small" data-testid="vex-name-refusal">{refusal}</span> : null}
          <span className="vexwarn small" data-testid="vex-slot-warning">{downloadLabel(slot)}.</span>
          <span className="muted small">Escape closes. Off under an approval card.</span>
        </span>
      ) : null}
    </span>
  );
}

// -- action bar -------------------------------------------------------------------------

function MarkView(props: { kind: string; mark: Mark }) {
  const { mark } = props;
  return (
    <span className={"vexmark " + mark.state} data-testid={`vex-mark-${props.kind}`} data-state={mark.state}
          role="img" aria-label={mark.text} title={mark.text}>
      {mark.state === "busy" ? <span className="vexspinner" aria-hidden="true" /> : markGlyph(mark.state)}
    </span>
  );
}

function ActionBar(props: {
  d: Detection; pid: string; label: string; blocked: boolean; slot: number; name: string;
  tip: (id: HelpId) => string; onAction: (a: "clean" | "build" | "rebuild" | "info" | "programs") => void;
  onDownload: () => void;
}) {
  const { d, pid, blocked, slot, name, tip } = props;
  const run = vex.run(pid);
  const cpp = d.language === "cpp";
  const configured = !!vex.status?.configured;
  const active = !!run.startingBuild || run.build?.state === "running";
  // Off while any project's chain runs (review MED-2), as Download is.
  const buildOff = blocked || !configured || !d.builds || !!run.busy || active || !!vex.chain;
  // One chain per window: no second download while one runs, for any project.
  const other = vex.chain && vex.chain.projectId !== pid ? vex.chain : null;
  const downloadOff = blocked || !configured || !d.builds || !!run.busy || !!nameRefusal(name) || !!vex.chain;
  const following = vex.monitoringBuild(pid);
  const mark = (kind: ActionKind) => actionState(kind, run, following);
  const dmark = mark("download");
  const noBuild = "A Python project has nothing to build.";
  const step = (kind: "clean" | "build" | "rebuild", text: string) => (
    <span className="vexstep">
      <Tip id={tip(kind)} text={cpp ? helpText(kind, slot) : noBuild}>
        <button type="button" data-testid={`vex-${kind}-btn`} disabled={buildOff || !cpp} aria-describedby={tip(kind)}
                onClick={() => { props.onAction(kind); void vex.build(pid, kind); }}>
          {text}
        </button>
      </Tip>
      <MarkView kind={kind} mark={mark(kind)} />
    </span>
  );
  const downloadHelp = !cpp ? "Downloading a Python project waits on a setup check; use VS Code for now."
    : (other ? `A download is running for ${other.label || "another project"}. ` : "") + helpText("download", slot);
  return (
    <div className="vexactions" data-testid="vex-actionbar" role="toolbar" aria-label="VEX actions">
      {step("clean", "Clean")}
      <span className="vexsep" aria-hidden="true">›</span>
      {step("build", "Build")}
      {step("rebuild", "Rebuild")}
      <span className="vexsep" aria-hidden="true">›</span>
      <span className="vexstep">
        <Tip id={tip("download")} text={downloadHelp}>
          {/* The one Download control: out of the Tab order, no pointer focus, the owner's first click only. */}
          <button
            type="button"
            className="vexdownload"
            data-testid="vex-download-btn"
            tabIndex={-1}
            onMouseDown={noPointerFocus}
            disabled={downloadOff || !cpp}
            aria-describedby={tip("download")}
            onClick={(e) => {
              if (!isExplicitClick({ isTrusted: e.nativeEvent.isTrusted, detail: e.detail, button: e.button })) return;
              if (props.blocked) return;
              void vex.download(pid, slot, name, props.label);
              props.onDownload();
            }}
          >
            {downloadButtonLabel(slot)}
          </button>
        </Tip>
        <MarkView kind="download" mark={dmark} />
      </span>
      <MoreMenu blocked={blocked} configured={configured} tip={tip} slot={slot}
                onRead={(read) => props.onAction(read)} />
    </div>
  );
}

/** ⋯: the rare device reads, `role="menu"` with arrow keys; closes under a card. */
function MoreMenu(props: { blocked: boolean; configured: boolean; tip: (id: HelpId) => string; slot: number;
                           onRead: (read: "info" | "programs") => void }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLSpanElement>(null);
  const menuId = useId();
  // A download's chain holds the device: no device read meanwhile.
  const off = props.blocked || !props.configured || !!vex.chain;
  useEffect(() => { if (props.blocked) setOpen(false); }, [props.blocked]);
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    // Escape closes it wherever focus is (every item may be off, leaving focus on ⋯).
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); btn.current?.focus(); } };
    window.addEventListener("mousedown", away, true);
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("mousedown", away, true); window.removeEventListener("keydown", esc); };
  }, [open]);
  const items = () => Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
  const read = (r: "system" | "programs") => {
    setOpen(false);
    props.onRead(r === "system" ? "info" : "programs");
    void vex.readInfo(r);
  };
  return (
    <span className="vexmore" ref={box}>
      <Tip id={props.tip("more")} text={helpText("more", props.slot)} end>
        <button type="button" ref={btn} data-testid="vex-more" aria-haspopup="menu" aria-expanded={open}
                aria-controls={menuId} aria-label="More: device reads" aria-describedby={props.tip("more")}
                disabled={props.blocked} onClick={() => setOpen(!open)}>⋯</button>
      </Tip>
      {open ? (
        <div className="vexmenu" role="menu" id={menuId} ref={menu} data-testid="vex-more-menu"
             onKeyDown={(e) => {
               const list = items();
               const i = list.indexOf(document.activeElement as HTMLButtonElement);
               if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
               else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
               else if (e.key === "Escape") { e.stopPropagation(); setOpen(false); btn.current?.focus(); }
             }}>
          <button type="button" role="menuitem" data-testid="vex-info-btn" disabled={off || !!vex.infoBusy}
                  onClick={() => read("system")}>
            {vex.infoBusy === "system" ? "Reading…" : "Read device info"}
            <span className="muted small"> vexcom --json</span>
          </button>
          <button type="button" role="menuitem" data-testid="vex-programs-btn" disabled={off || !!vex.infoBusy}
                  onClick={() => read("programs")}>
            {vex.infoBusy === "programs" ? "Reading…" : "List programs"}
            <span className="muted small"> vexcom --programs</span>
          </button>
        </div>
      ) : null}
    </span>
  );
}

// -- status line ------------------------------------------------------------------------

/**
 * One line, never folded (plan §2.3): the running chain (in **every** VEX
 * view, M1), else this project's running build, else its last result in
 * words, else Ready. Stop sits at its end while something can be stopped.
 * Under a card it says Held, in amber: amber's one meaning in Graphite.
 */
function StatusLine(props: { pane: number; pid: string | null; d: Detection | null; blocked: boolean }) {
  const { pid, blocked } = props;
  const c = vex.chain;
  const run = pid ? vex.run(pid) : null;
  const building = !!run && (!!run.startingBuild || run.build?.state === "running");
  const now = useNow(!!c || building);
  let state = "idle";
  let body: ReactNode = null;
  if (c) {
    state = "busy";
    const crun = vex.run(c.projectId);
    const pct = c.phase === "download" && crun.download?.state === "running"
      ? downloadProgress(crun.download.output) : null;
    body = <span className="vexchain" data-testid="vex-chain" data-project={c.projectId} data-phase={c.phase}>
      <span className="vexspinner" aria-hidden="true" />
      <span data-testid="vex-chain-line">{chainLine(c)}</span>
      <ol className="vexsteps" data-testid="vex-chain-steps" aria-label="Download steps">
        {chainSteps(c).map((s) => (
          <li key={s.step} data-step={s.step} data-state={s.state}>
            {s.state === "done" ? "✓ " : ""}{s.step === "clean" ? "Clean" : s.step === "build" ? "Build"
              : s.step === "rebuild" ? "Rebuild" : "Download"}
          </li>
        ))}
      </ol>
      {/* Ticking numbers stay out of the live region (review LOW-4): the progress bar carries them. */}
      {pct !== null ? <span className="vexel" aria-hidden="true">{pct}%</span> : null}
      {crun.downloadStarted !== null
        ? <span className="vexel" data-testid="vex-status-elapsed" aria-hidden="true">{elapsedText(crun.downloadStarted, now)}</span> : null}
    </span>;
  } else if (pid && run && props.d) {
    if (building) {
      state = "busy";
      const line = run.startingBuild
        ? { tone: "busy", text: `Starting ${run.startingBuild.action}…` } : buildLine(run.build);
      const started = run.startingBuild?.started ?? Date.parse(run.build?.started ?? "");
      const following = !!run.startingBuild || vex.monitoringBuild(pid);
      body = <>
        {following ? <span className="vexspinner" aria-hidden="true" /> : null}
        <span className={"vexstate " + line.tone} data-testid="vex-build-state"
              data-state={run.startingBuild ? "starting" : run.build?.state ?? "none"}>
          {following ? line.text : "Status checks stopped; the daemon last said it was running."}
        </span>
        <span className="vexel" data-testid="vex-build-elapsed" aria-hidden="true">{elapsedText(started, now)}</span>
        {run.build?.state === "running" && !following ? (
          <Tip id={`vex-${props.pane}-tip-recheck-build`} text={helpText("recheck-build", 1)}>
            <button type="button" className="quiet" data-testid="vex-build-recheck" disabled={blocked}
                    aria-describedby={`vex-${props.pane}-tip-recheck-build`}
                    onClick={() => void vex.refreshBuild(pid)}>Check status</button>
          </Tip>
        ) : null}
      </>;
    } else if (run.note) {
      state = run.noteTone === "ok" ? "ok" : run.noteTone === "bad" ? "failed"
        : run.downloaded?.ok === null ? "unknown" : "idle";
      body = <span className={"vexnote " + run.noteTone} data-testid="vex-note">{run.note}</span>;
    } else if (run.build) {
      const line = buildLine(run.build, run.stoppedBuild === run.build.id);
      state = line.tone === "ok" ? "ok" : line.tone === "bad" ? "failed" : "idle";
      body = <span className={"vexstate " + line.tone} data-testid="vex-build-state" data-state={run.build.state}>
        {line.text}
      </span>;
    } else if (props.d.language !== "cpp") {
      body = <span className="muted" data-testid="vex-status-idle">A Python project builds and downloads in VS Code for now.</span>;
    } else {
      body = <span className="muted" data-testid="vex-status-idle">Ready. Edit, then Clean › Build › Download.</span>;
    }
  }
  // Stale ✓s said in words beside the result (advisory; never while something runs).
  const stale = run && pid && props.d ? staleNotes(run, vex.monitoringBuild(pid), !!c || building) : [];
  const stoppable = vex.stoppable(pid);
  if (!body && !stoppable && !blocked) return null;
  const tipId = `vex-${props.pane}-tip-stop`;
  return (
    <div className={"vexstatus " + state + (blocked ? " held" : "")} data-testid="vex-status"
         data-state={blocked ? "held" : state}>
      <span className="vexstatus-text" role="status" aria-live="polite">
        {blocked ? <span className="vexheld" data-testid="vex-held">Held: an approval card is up.</span> : null}
        {body}
        {stale.length ? (
          <span className="vexstalenote muted small" data-testid="vex-stale-note">
            {stale.map((n) => (
              <span key={n.kind} data-kind={n.kind}>
                {n.name}: <span className="vexstaleglyph" aria-hidden="true">✓</span> {n.detail}
              </span>
            ))}
          </span>
        ) : null}
        {pid && props.d ? <UsbHint /> : null}
      </span>
      {stoppable ? (
        <Tip id={tipId} text={helpText("stop", c?.slot ?? 1)} end>
          <button type="button" className="vexstop" data-testid="vex-stop" disabled={blocked}
                  aria-describedby={tipId}
                  onClick={(e) => {
                    // A trusted press only: a pointer, or Enter/Space on it — never a script's click.
                    if (!isTrustedPress({ isTrusted: e.nativeEvent.isTrusted, button: e.button })) return;
                    if (blocked) return;
                    vex.stop(pid);
                  }}>
            ■ Stop
          </button>
        </Tip>
      ) : null}
    </div>
  );
}

/** Two confirmed USB disconnects: a muted hint. It never disables anything (USB is not readiness). */
function UsbHint() {
  const c = vex.connection;
  if (!c || c.brain !== false || c.controller !== false || !c.checked_at) return null;
  return <span className="vexusbhint muted small" data-testid="vex-usb-hint">
    <span className="vexusb bad" aria-hidden="true">●</span> No brain or controller on USB · checked{" "}
    {new Date(c.checked_at).toLocaleTimeString()}
  </span>;
}

/** Refusals in the daemon's own words, under the status line (each once). */
function Alerts(props: { pid: string }) {
  const run = vex.run(props.pid);
  const lines: [string, string][] = ([
    ["vex-status-error", vex.statusError],
    ["vex-build-note", run.buildNote],
    ["vex-download-note", run.downloadNote],
    ["vex-brain-note", vex.brainNote],
    ["vex-info-note", vex.infoNote],
  ] as [string, string][]).filter(([, text]) => !!text && text !== run.note);
  // Said in words wherever the owner is looking (not an alert: nothing failed).
  const closed = vex.brainClosed && !vex.brainTerminal ? vex.brainClosed : "";
  if (!lines.length && !closed) return null;
  return <div className="vexalerts">
    {lines.map(([id, text]) => <div key={id} className="err small" role="alert" data-testid={id}>{text}</div>)}
    {closed ? <div className="muted small" role="status" data-testid="vex-brain-closed">{closed}</div> : null}
  </div>;
}

// -- errors -----------------------------------------------------------------------------

const ERRORS_SHOWN = 5;

function ErrorsList(props: { pid: string; blocked: boolean; onOpen: (p: string, l: number, c: number) => void }) {
  const run = vex.run(props.pid);
  const [all, setAll] = useState(false);
  const errors = run.startingBuild ? [] : run.build?.errors ?? [];
  if (!errors.length) return null;
  return <div className="vexerrorswrap">
    <ul className={"vexerrors" + (all ? " all" : "")} data-testid="vex-errors" aria-label="Build errors">
      {errors.map((e, i) => <ErrorRow key={i} e={e} i={i} blocked={props.blocked} onOpen={props.onOpen} />)}
    </ul>
    {errors.length > ERRORS_SHOWN ? (
      <button type="button" className="quiet" data-testid="vex-errors-all" aria-expanded={all}
              disabled={props.blocked} onClick={() => setAll(!all)}>
        {all ? "Show fewer" : `Show all ${errors.length}`}
      </button>
    ) : null}
  </div>;
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

// -- output -----------------------------------------------------------------------------

const TAB_NAME: Record<OutputTab, string> = { build: "Build", download: "Download", brain: "Brain", device: "Device" };

function Output(props: {
  pane: number; d: Detection; pid: string; blocked: boolean; configured: boolean; ui: PaneUi;
  setUi: (p: Partial<PaneUi>) => void; help: boolean; slot: number; name: string; tip: (id: HelpId) => string;
  drawn: boolean;
}) {
  const { pane, pid, blocked, ui, setUi, tip } = props;
  const tabRefs = useRef<Partial<Record<OutputTab, HTMLButtonElement | null>>>({});
  const run = vex.run(pid);
  const usb = useUsbReading(blocked || !props.configured || !!vex.chain);
  const brain = useBrain(pane, blocked, props.configured);
  // The same state as each step's mark (lib/vex.ts `tabBadge`); the Device tab's is ? while its reading is stale.
  const badge = (tab: OutputTab): Mark => tab === "brain" ? brain.view.badge
    : tab === "device" ? deviceBadge(usb.c ?? null, usb.stale)
      : tabBadge(tab, run, vex.monitoringBuild(pid));
  const panelId = `vex-${pane}-outpanel`;
  return (
    <div className={"vexout" + (ui.folded ? " folded" : "")} data-testid="vex-output">
      <div className="vexotabs">
        <div role="tablist" aria-label="VEX output" className="vextablist">
          {OUTPUT_TABS.map((tab) => {
            const b = badge(tab);
            const on = ui.tab === tab;
            const tid = tip(`tab-${tab}` as HelpId);
            return (
              <button key={tab} type="button" role="tab" ref={(el) => { tabRefs.current[tab] = el; }}
                      id={`vex-${pane}-tab-${tab}`} className={"vextab" + (on ? " on" : "")}
                      data-testid={`vex-out-tab-${tab}`} data-state={b.state}
                      aria-selected={on} aria-controls={panelId} tabIndex={on ? 0 : -1}
                      aria-label={`${TAB_NAME[tab]}, ${b.text}`} aria-describedby={tid} disabled={blocked}
                      onClick={() => setUi({ tab, folded: false })}
                      onKeyDown={(e) => {
                        const to = nextTab(OUTPUT_TABS, ui.tab, e.key);
                        if (!to) return;
                        e.preventDefault();
                        setUi({ tab: to });
                        tabRefs.current[to]?.focus();
                      }}>
                <HelpNum id={`tab-${tab}` as HelpId} />
                {TAB_NAME[tab]}
                <span className={"vexbadgemark " + b.state} data-testid={`vex-out-badge-${tab}`} aria-hidden="true">
                  {b.state === "busy" ? <span className="vexspinner" />
                    : tab === "device" && b.state !== "unknown" ? "●"
                      : tab === "brain" ? (b.state === "ok" ? "●" : b.state === "unknown" ? "◌" : markGlyph(b.state))
                        : markGlyph(b.state)}
                </span>
                <span className="vextip" role="tooltip" id={tid} aria-hidden="true">{helpText(`tab-${tab}` as HelpId, props.slot)}</span>
              </button>
            );
          })}
        </div>
        <span className="vexotools">
          <Tip id={tip("max")} text={helpText("max", props.slot)} end>
            <button type="button" className="quiet" data-testid="vex-out-max" aria-pressed={ui.max && !ui.folded}
                    aria-label="Fill the pane with the output" aria-describedby={tip("max")} disabled={blocked}
                    onClick={() => setUi({ max: !(ui.max && !ui.folded), folded: false })}>⤢</button>
          </Tip>
          <Tip id={tip("fold")} text={helpText("fold", props.slot)} end>
            <button type="button" className="quiet" data-testid="vex-out-fold" aria-expanded={!ui.folded}
                    aria-controls={panelId} aria-label={ui.folded ? "Show the output" : "Fold the output"}
                    aria-describedby={tip("fold")} disabled={blocked}
                    onClick={() => setUi({ folded: !ui.folded })}>{ui.folded ? "▸" : "▾"}</button>
          </Tip>
        </span>
      </div>
      {ui.folded ? null : (
        <div className="vexopanel" role="tabpanel" id={panelId} aria-labelledby={`vex-${pane}-tab-${ui.tab}`}
             data-testid="vex-out-panel" data-tab={props.help ? "help" : ui.tab}>
          {props.help ? <HelpList slot={props.slot} />
            : ui.tab === "build" ? <BuildTab pane={pane} d={props.d} pid={pid} blocked={blocked} />
              : ui.tab === "download" ? <DownloadTab pane={pane} d={props.d} pid={pid} blocked={blocked} name={props.name} />
                : ui.tab === "brain" ? <BrainTab pane={pane} drawn={props.drawn} blocked={blocked} brain={brain}
                                                 tip={tip} slot={props.slot} />
                  : <DeviceTab pane={pane} blocked={blocked} configured={props.configured} />}
        </div>
      )}
    </div>
  );
}

function BuildTab(props: { pane: number; d: Detection; pid: string; blocked: boolean }) {
  const run = vex.run(props.pid);
  const active = !!run.startingBuild || run.build?.state === "running";
  const build = run.startingBuild ? null : run.build;
  if (props.d.language !== "cpp") return <div className="vexobody muted small">A Python project has nothing to build.</div>;
  return <>
    <div className="vexobar">
      <span>{build ? `make · ${build.action}${build.exit_code !== null ? ` · exit ${build.exit_code}` : ""}` : "make"}</span>
      <span className="vexobar-r">
        {build?.terminal_id ? (
          <Tip id={`vex-${props.pane}-tip-terminal`} text={helpText("terminal", 1)} end>
            <button type="button" className="quiet" data-testid="vex-build-output-btn" disabled={props.blocked}
                    aria-describedby={`vex-${props.pane}-tip-terminal`}
                    onClick={() => void terminals.showProgram(build.terminal_id!)}>Show terminal</button>
          </Tip>
        ) : null}
      </span>
    </div>
    {active && !build?.output ? (
      <div className="vexobody muted small" data-testid="vex-build-waiting">
        {run.startingBuild ? "Waiting for the build tools to start…"
          : "Waiting for tool output. This action is stopped automatically after 5 minutes."}
      </div>
    ) : build?.output ? <TextOutput output={build.output} />
      : build && !active && build.state !== "succeeded" ? (
        <div className="vexobody muted small">No output preview is available. Open the terminal for full output.</div>
      ) : !build ? <div className="vexobody muted small">Nothing built in this window yet.</div> : null}
  </>;
}

function DownloadTab(props: { pane: number; d: Detection; pid: string; blocked: boolean; name: string }) {
  const { pid, blocked } = props;
  const run = vex.run(pid);
  const active = run.busy === "download";
  // vexcom's own progress line, while it runs; full once it has succeeded.
  const progress = run.download?.state === "succeeded" ? 100
    : run.download?.state === "running" ? downloadProgress(run.download.output ?? "") : null;
  if (props.d.language !== "cpp") {
    return <div className="vexobody muted small">Downloading a Python project waits on a setup check; use VS Code for now.</div>;
  }
  return <>
    <div className="vexobar" data-testid="vex-download-state" aria-busy={active}>
      <span>{run.download ? `vexcom · slot ${run.download.slot}` : "vexcom"}</span>
      {progress !== null ? (
        <span className="vexprogress" data-testid="vex-download-progress" role="progressbar" aria-label="Download progress"
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
          <span className="vexprogress-track">
            <span className={"vexprogress-fill" + (progress === 100 && !active ? " ok" : "")} style={{ width: `${progress}%` }} />
          </span>
          <span className="vexprogress-pct">{progress}%</span>
        </span>
      ) : null}
      {run.downloaded ? (
        <span className={"small " + (run.downloaded.ok ? "ok" : run.downloaded.ok === null ? "muted" : "err")}
              data-testid="vex-downloaded">
          {run.downloaded.ok === true ? `✓ In slot ${run.downloaded.slot}`
            : run.downloaded.ok === null ? "? Download outcome unknown — check status or terminal."
              : run.downloaded.stopped ? `✗ Stopped: slot ${run.downloaded.slot} may be incomplete`
                : `✗ Download ${run.download?.state === "timed_out" ? "timed out" : "failed"} (exit ${run.downloaded.code ?? "?"})`}
        </span>
      ) : null}
      <span className="vexobar-r">
        {run.download?.terminal_id && !run.downloaded?.stopped ? (
          <Tip id={`vex-${props.pane}-tip-terminal`} text={helpText("terminal", 1)} end>
            <button type="button" className="quiet" disabled={blocked} data-testid="vex-download-output-btn"
                    aria-describedby={`vex-${props.pane}-tip-terminal`}
                    onClick={() => void terminals.showProgram(run.download!.terminal_id!)}>Show terminal</button>
          </Tip>
        ) : null}
        {run.download && run.download.state === "running" && !active ? (
          <Tip id={`vex-${props.pane}-tip-recheck-download`} text={helpText("recheck-download", 1)} end>
            <button type="button" className="quiet" disabled={blocked} data-testid="vex-download-recheck"
                    aria-describedby={`vex-${props.pane}-tip-recheck-download`}
                    onClick={() => void vex.refreshDownload(pid)}>Check status</button>
          </Tip>
        ) : null}
      </span>
    </div>
    {run.download?.output ? <TextOutput output={run.download.output} download />
      : active ? <div className="vexobody muted small">Waiting for Download output…</div>
        : <div className="vexobody muted small">No download in this window yet.</div>}
  </>;
}

/** The Brain tab's reading of the brain terminal, for its body and its badge (lib/vex.ts `brainView`). */
interface BrainNow {
  view: BrainView;
  /** The brain terminal this window shows, while it exists here. */
  tid: string | null;
  /** Another place shows it: a terminal pane or another VEX pane (pane ids). */
  other: { kind: "pane" | "vex"; pane: number } | null;
  /** This pane's Brain tab holds it (`vex-<pane>`): the terminal is drawn here. */
  here: boolean;
  /** Nothing has been printed in it yet in this window. */
  silent: boolean;
}

function useBrain(pane: number, blocked: boolean, configured: boolean): BrainNow {
  const mgr = useTerminals();
  const id = vex.brainTerminal;
  // While it exists here: listed, a session in this window, or the daemon says it holds the port.
  const tid = id && (mgr.row(id) || mgr.sessions.has(id) || vex.status?.brain === id) ? mgr.resolve(id) : null;
  const s = tid ? mgr.sessions.get(tid) : undefined;
  const shown = tid ? mgr.shownIn(tid) : null;
  const here = shown?.kind === "vex" && shown.pane === pane;
  const other = shown && !here ? shown : null;
  const row = tid ? mgr.row(tid) : null;
  const view = brainView({
    configured, blocked, chain: vex.chain ? { paused: vex.chain.brainPaused } : null,
    terminal: tid, asking: vex.brainBusy,
    session: s ? s.state : row?.exited ? "exited" : null,
    exitCode: s?.exitCode ?? row?.exit_code ?? null,
    shownIn: other ? { kind: other.kind, slot: mgr.slotOf(other.pane) } : tid && mgr.elsewhere(tid) ? { kind: "window" } : null,
    error: vex.brainNote, closed: vex.brainClosed, device: vex.status?.device ?? null, reading: !!vex.infoBusy,
  });
  return { view, tid, other, here, silent: !s?.printed };
}

/**
 * The Brain tab (VX-UI-4a, plan §5.5): the brain terminal drawn here, at the
 * `vex-<pane>` place — the same session, "Jarvis can read" switch and Ctrl-C
 * rule as in the panel; xterm draws its bytes as characters. **Connect is the
 * only way it opens** (owner decision 5), on a trusted press; showing this tab
 * opens nothing, and a reload only shows a link the daemon already holds.
 * Switching to another tab keeps the session attached (its ring keeps the
 * output); Disconnect closes it.
 */
function BrainTab(props: { pane: number; drawn: boolean; blocked: boolean; brain: BrainNow; tip: (id: HelpId) => string;
                           slot: number }) {
  const mgr = useTerminals();
  const { pane, blocked, tip } = props;
  const { view, tid, other, here, silent } = props.brain;
  // Held here while this tab is drawn and its terminal is not shown elsewhere in this window.
  const want = props.drawn && !!tid && view.terminal && !other;
  useEffect(() => {
    if (!want || !tid) return;
    terminals.holdInVex(pane, tid);
    return () => terminals.holdInVex(pane, null);
  }, [want, tid, pane]);
  // Whether this tab is still drawn when its Connect's answer comes: only then does it take the place first.
  const live = useRef(false);
  live.current = props.drawn;
  useEffect(() => () => { live.current = false; }, []);
  const s = tid ? mgr.sessions.get(tid) : undefined;
  const connectHelp = view.connect.off ? `${view.connect.off} ${helpText("connect", props.slot)}` : helpText("connect", props.slot);
  return <div className="vexbrain" data-testid="vex-brain-tab" data-state={view.state}>
    <div className="vexobar">
      <span>vexcom --user</span>
      <span className={"vexbrain-state " + view.badge.state} data-testid="vex-brain-state">{view.badge.text}</span>
      <span className="vexobar-r">
        {other ? (
          <button type="button" className="quiet" data-testid="vex-brain-goto" disabled={blocked}
                  onClick={() => mgr.focusPane(other.pane as PaneNo)}>Go there</button>
        ) : null}
        {view.connect.shown ? (
          <Tip id={tip("connect")} text={connectHelp} end>
            <button type="button" className="vexconnect" data-testid="vex-brain-connect"
                    disabled={blocked || !!view.connect.off} aria-describedby={tip("connect")}
                    onClick={(e) => {
                      // The owner's own press only: a pointer, or Enter/Space on it — never a script's click.
                      if (!isTrustedPress({ isTrusted: e.nativeEvent.isTrusted, button: e.button })) return;
                      if (blocked) return;
                      // The pane whose Connect was pressed shows it, before any other Brain tab can.
                      void vex.connectBrain((id) => { if (live.current) terminals.holdInVex(pane, id); });
                    }}>
              Connect
            </button>
          </Tip>
        ) : null}
        {view.reconnect && s ? (
          <button type="button" className="quiet" data-testid="vex-brain-reconnect" disabled={blocked}
                  title="Attach to the brain terminal again; the link itself is not reopened"
                  onClick={() => s.retry()}>Reconnect</button>
        ) : null}
        {view.disconnect.shown ? (
          <Tip id={tip("disconnect")} text={helpText("disconnect", props.slot)} end>
            <button type="button" className="quiet" data-testid="vex-brain-disconnect" disabled={blocked}
                    aria-describedby={tip("disconnect")}
                    onClick={(e) => {
                      if (!isTrustedPress({ isTrusted: e.nativeEvent.isTrusted, button: e.button })) return;
                      if (blocked) return;
                      void vex.disconnectBrain();
                    }}>
              {view.disconnect.close ? "Close" : "Disconnect"}
            </button>
          </Tip>
        ) : null}
      </span>
    </div>
    <div className={"vexbrain-text small" + (view.state === "error" ? " err" : view.state === "ended" ? "" : " muted")}
         role="status" data-testid="vex-brain-text">
      {view.text}
      {view.state === "connected" && silent ? (
        <span data-testid="vex-brain-silent">
          {" "}Nothing printed yet: output appears here as the program prints. If nothing ever appears, the link
          may not have started: whether it starts through WSL is not yet checked on the real robot (hardware check H4).
        </span>
      ) : null}
    </div>
    {here && tid && view.terminal ? (
      <div className="vexbrain-term">
        <TerminalView id={tid} where={`vex-${pane}`} />
      </div>
    ) : null}
  </div>;
}

function DeviceTab(props: { pane: number; blocked: boolean; configured: boolean }) {
  const off = props.blocked || !props.configured || !!vex.chain;
  // Refresh sits in the tab's toolbar, like Show terminal in the others: outside the scrolling body, so its
  // hover help is never cut off by it (review MED).
  return <>
    <div className="vexobar">
      <span>vexcom --usb</span>
      <span className="vexobar-r">
        <Tip id={`vex-${props.pane}-tip-usb-refresh`} text={helpText("usb-refresh", 1)} end>
          <button type="button" className="quiet" disabled={off} data-testid="vex-connection-refresh"
                  aria-describedby={`vex-${props.pane}-tip-usb-refresh`}
                  onClick={() => void vex.refreshConnection()}>Refresh USB status</button>
        </Tip>
      </span>
    </div>
    <div className="vexobody vexdev" data-testid="vex-device">
    <ConnectionStatus blocked={off} />
    {vex.info ? (
      <div className="small" data-testid="vex-info-what" data-read={vex.info.read}>
        {vex.info.read === "programs" ? "Programs on the brain" : "Device info"}
        {vex.info.exit_code ? ` (vexcom exited ${vex.info.exit_code})` : ""}
      </div>
    ) : <div className="muted small">Read device info and List programs are in ⋯.</div>}
    {vex.info ? <pre className="vexinfo" data-testid="vex-info-text">{vex.info.text}</pre> : null}
    </div>
  </>;
}

function TextOutput(props: { output: string; download?: boolean }) {
  const ref = useRef<HTMLPreElement>(null);
  const following = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && following.current) el.scrollTop = el.scrollHeight;
  }, [props.output]);
  return (
    <pre ref={ref} className="vexoutput" data-testid={props.download ? "vex-download-output" : "vex-build-output"}
         aria-label={props.download ? "Download output" : "Build output"} tabIndex={0}
         onScroll={() => {
           const el = ref.current;
           if (el) following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
         }}>{props.output}</pre>
  );
}

// -- USB --------------------------------------------------------------------------------

/** The USB reading as both indicators show it: the last confirmed values, and
 * whether they are stale (paused, older than 12 s, not "ready", or erroring). */
function useUsbReading(off: boolean) {
  const now = useNow(true);
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

/** The same reading, small, in the header: no tab to open to see whether the brain or controller is plugged in. */
function UsbBadges(props: { off: boolean }) {
  const { c, stale, label, tone } = useUsbReading(props.off);
  const at = c?.checked_at ? ` · checked ${new Date(c.checked_at).toLocaleTimeString()}` : "";
  const badge = (name: string, testid: string, value: boolean | null | undefined) => {
    const text = `${name}: ${label(value)}${stale && value != null ? " (last confirmed)" : ""}`;
    return <span className={tone(value) + " vexbadge"} data-testid={testid} title={`${text} (USB)${at}`} aria-label={text}>
      <span className="vexdot" aria-hidden="true" />{name}
    </span>;
  };
  return <span className="vexbadges" data-testid="vex-usb-badges">
    {badge("Brain", "vex-badge-brain", c?.brain)}
    {badge("Controller", "vex-badge-controller", c?.controller)}
  </span>;
}

function ConnectionStatus(props: { blocked: boolean }) {
  const { c, paused, stale, label, tone } = useUsbReading(props.blocked);
  return <div data-testid="vex-connection" className="vexconn">
    <div className="small" data-testid="vex-tooling">Tooling: {vex.status?.configured ? "Configured" : "Not configured"}
      {vex.status?.vexcom_version ? <span className="muted"> · vexcom {vex.status.vexcom_version}</span> : null}</div>
    <div className="vexrow" role="status">
      <span data-testid="vex-usb-brain" className={tone(c?.brain)}>Brain: {label(c?.brain)}{stale && c?.brain != null ? " (last confirmed)" : ""}</span>
      <span data-testid="vex-usb-controller" className={tone(c?.controller)}>Controller: {label(c?.controller)}{stale && c?.controller != null ? " (last confirmed)" : ""}</span>
    </div>
    <div className="muted small" data-testid="vex-connection-note">{paused ? "USB checks paused; showing last confirmed state." : stale && c?.state === "ready" ? "USB reading is stale; awaiting a fresh check." : vex.connectionError || c?.message || "USB devices have not been checked."}
      {c?.checked_at ? ` Last confirmed: ${new Date(c.checked_at).toLocaleTimeString()}.` : ""}</div>
    <div className="muted small">USB presence only; radio link and extension health are not measured.</div>
    {c?.events.length ? <>
      <div className="vexh5">Connection log</div>
      <ul className="small vexlog" data-testid="vex-connection-log">{c.events.map((e, i) => <li key={i}>
        <time dateTime={e.at}>{new Date(e.at).toLocaleTimeString()}</time>
        <span className={e.connected ? "ok" : "err"}>{` ${e.device === "brain" ? "Brain" : "Controller"} ${e.connected ? "connected" : "disconnected"}`}</span>
      </li>)}</ul></> : null}
  </div>;
}
