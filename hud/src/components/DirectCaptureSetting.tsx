// Settings → Browser: the owner's Direct capture switch and probe
// (B-DIRECT-CAPTURE, docs/plans/2026-10-19-direct-capture-plan.md).
//
// The switch is off by default. The daemon refuses to turn it on until the
// owner's probe passed since Jarvis started; the checkbox says so rather than
// trying. The probe runs only on the owner's trusted press, only in the
// installed app, and captures a HUD-made test pattern, never a web page.
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { browserSettings } from "../state/browserSettings";
import { browserEnv, type CaptureEnv } from "../lib/directCapture";
import {
  PROBE_APP_ONLY, PROBE_HEIGHT, PROBE_REASONS, PROBE_WIDTH, chromeMajor, displayMode, patternHtml, runProbe,
} from "../lib/directProbe";
import { noPointerFocus } from "../lib/talkkey";

export const DIRECT_CAPTURE_HELP = "Annotate and Send to chat on a Direct page ask Chrome to share this tab, keep one frame of the page and stop sharing at once. Jarvis cannot read a Direct page's text, so that picture has no credential check.";
export const DIRECT_UNTESTED = "Not tested since Jarvis started. Turning this on needs a passing test.";

export function DirectCaptureSetting(props: { blocked: boolean; env?: CaptureEnv; match?: (query: string) => boolean }) {
  const direct = useSyncExternalStore(browserSettings.subscribe, browserSettings.directCapture);
  const probe = useSyncExternalStore(browserSettings.subscribe, browserSettings.probe);
  const [busy, setBusy] = useState(false), [probing, setProbing] = useState(false);
  const [note, setNote] = useState(""), [error, setError] = useState("");
  const pattern = useRef<HTMLIFrameElement>(null), loaded = useRef<(() => void) | null>(null);
  const mounted = useRef(true), epoch = useRef(0);
  const now = useRef(props); now.current = props;
  const env = () => now.current.env ?? browserEnv();
  useEffect(() => { mounted.current = true; void browserSettings.refreshProbe();
    return () => { mounted.current = false; epoch.current++; }; }, []);
  // A card arriving during the probe ends it; the capture's tracks stop.
  useLayoutEffect(() => { if (props.blocked) epoch.current++; }, [props.blocked]);
  const run = async (press: { isTrusted: boolean }) => {
    const e = env();
    if (!e.trusted(press) || now.current.blocked || probing) return;
    const mode = displayMode(now.current.match ?? (q => window.matchMedia(q).matches));
    setError("");
    if (mode !== "standalone") { setNote(PROBE_APP_ONLY); return; }
    const token = ++epoch.current;
    const live = () => mounted.current && token === epoch.current && !now.current.blocked;
    setNote(""); setProbing(true);
    try {
      const ready = new Promise<void>(resolve => { loaded.current = resolve; });
      await Promise.race([ready, new Promise(r => setTimeout(r, 2000))]);
      await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())));
      const frame = pattern.current;
      if (!frame || !live()) return;
      const facts = await runProbe(frame, e, mode, chromeMajor(navigator as Navigator & { userAgentData?: never }), live);
      if (!live()) return;
      const out = await browserSettings.reportProbe(facts);
      if (mounted.current) setNote((PROBE_REASONS[out.reason] ?? "Failed.") + (out.saved ? "" : " The report file could not be saved."));
    } catch (err) {
      if (mounted.current && token === epoch.current) setError(err instanceof Error ? err.message : "The test could not be recorded.");
    } finally {
      loaded.current = null;
      if (mounted.current && token === epoch.current) setProbing(false);
    }
  };
  const locked = direct !== true && !probe?.passed;
  return <div className="block direct-capture-setting" data-testid="direct-capture-setting">
    <label className="browser-auto-setting">
      <input type="checkbox" data-testid="browser-direct-capture" checked={direct === true}
        disabled={props.blocked || busy || probing || direct === null || locked} onMouseDown={noPointerFocus}
        onChange={async ev => {
          if (!env().trusted(ev.nativeEvent) || props.blocked || busy || direct === null) return;
          const value = ev.target.checked;
          setBusy(true); setError("");
          try { await browserSettings.setDirectCapture(value); }
          catch (err) { setError(err instanceof Error ? err.message : "The setting could not be saved."); }
          finally { setBusy(false); }
        }} />
      Capture Direct pages
    </label>
    <p className="muted">{DIRECT_CAPTURE_HELP}</p>
    <p className="muted" role="status" data-testid="direct-probe-status">
      {probe ? (PROBE_REASONS[probe.reason] ?? "Failed.") + " (" + probe.at + ")" : DIRECT_UNTESTED}
    </p>
    <button type="button" data-testid="browser-direct-probe" disabled={props.blocked || probing}
      onClick={ev => void run(ev.nativeEvent)}>{probing ? "Testing…" : "Test Direct capture"}</button>
    {note ? <p role="status" data-testid="direct-probe-note">{note}</p> : null}
    {error ? <p className="err" role="status">{error}</p> : null}
    {probing ? <iframe ref={pattern} className="direct-probe-pattern" data-testid="direct-probe-pattern"
      title="Direct capture test pattern" sandbox="" tabIndex={-1} srcDoc={patternHtml()}
      style={{ width: PROBE_WIDTH, height: PROBE_HEIGHT, border: 0, display: "block", margin: "8px 0" }}
      onLoad={() => loaded.current?.()} /> : null}
  </div>;
}
