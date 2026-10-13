import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { browserApi } from "../api";
import { noPointerFocus } from "../lib/talkkey";
import { browserApprovalDeadline, browserApprovalKey, parseBrowserApproval, type BrowserApprovalDetail, type BrowserApprovalPublic } from "../lib/browserapproval";
import "./BrowserApproval.css";

export const PRIVATE_DETAIL_UNAVAILABLE = "The Browser action details are unavailable. Deny this request.";
/** Only the mounted top card calls this hook. No detail enters the public approval row. */
export function useBrowserApproval(request: BrowserApprovalPublic) {
  const key = browserApprovalKey(request);
  const current = useRef(key); current.current = key;
  const alive = useRef(false);
  useLayoutEffect(() => { alive.current = true; return () => { alive.current = false; }; }, [key]);
  const [result, setResult] = useState<{ key: string; detail: BrowserApprovalDetail | null; failed: boolean } | null>(null);
  const [now, setNow] = useState(Date.now);
  const eligible = request.hud_only === true && request.tool === "hud_browser_act" && request.layer === "browser-gate"
    && /^[A-Za-z0-9_-]{12}$/.test(request.req_id);
  const deadline = useMemo(() => {
    const at = Date.parse(request.asked_at), received = Date.now();
    return at > received + 5000 ? 0 : Math.min(browserApprovalDeadline(request), received + 120000);
  }, [key]);
  useEffect(() => {
    let gone = false;
    const controller = new AbortController();
    if (deadline <= Date.now() || !eligible) return;
    void browserApi.get<unknown>("/approvals/" + encodeURIComponent(request.req_id), controller.signal).then(raw => {
      if (gone || current.current !== key) return;
      const detail = parseBrowserApproval(raw, request);
      setResult({ key, detail, failed: !detail });
    }).catch(() => { if (!gone && current.current === key) setResult({ key, detail: null, failed: true }); });
    return () => { gone = true; controller.abort(); };
  }, [key, deadline, eligible]);
  useEffect(() => {
    setNow(Date.now());
    if (deadline <= Date.now()) return;
    const timer = setInterval(() => { const time = Date.now(); setNow(time); if (time >= deadline) clearInterval(timer); }, 250);
    return () => clearInterval(timer);
  }, [key, deadline]);
  const selected = result?.key === key ? result : null;
  const detail = selected?.detail ?? null;
  const expired = now >= deadline;
  const canAuthorize = () => alive.current && current.current === key && !!detail && Date.now() < deadline;
  return { detail, expired, canAuthorize, left: Math.max(0, Math.ceil((deadline - now) / 1000)),
    message: expired ? "This Browser request expired." : !eligible || selected?.failed ? PRIVATE_DETAIL_UNAVAILABLE : detail ? "" : "Loading private Browser action details…" };
}

export function BrowserApprovalBody({ detail }: { detail: BrowserApprovalDetail }) {
  const b = detail.browser, release = b.action === "release_download";
  return <div className="browserapproval" data-testid="browser-approval-details">
    <div className="headline">{release ? "Release this executable download?" : "Authorize this one Browser action?"}</div>
    <p>Pane {b.pane} · <bdi>{b.action.replaceAll("_", " ")}</bdi> · <bdi>{b.category.replaceAll("_", " ")}</bdi></p>
    {b.host || b.path ? <p>Page: <bdi>{b.host}{b.path}</bdi></p> : null}
    {b.download ? <p>File: <bdi>{b.download.name}</bdi> · {b.download.size.toLocaleString()} bytes · ID <bdi>{b.download.id}</bdi></p> :
      b.target ? <p>The page calls it: <bdi>{b.target}</bdi></p> : null}
    <p className="muted" data-testid="browser-approval-preview">Preview unavailable</p>
    <p>{release ? "This releases the file to you. It does not run it." : "Page labels are untrusted page text. Read the exact action below."}</p>
    <pre className="arg" data-testid="approval-command"><bdi>{detail.command}</bdi></pre>
  </div>;
}

/** The veil supplies inertness/focus trapping; this card permits one deliberate click. */
export function BrowserApprovalCard({ request, onDecide, onPresent }: {
  request: BrowserApprovalPublic;
  onDecide: (reqId: string, allow: boolean, always?: boolean) => void;
  onPresent: () => void;
}) {
  const details = useBrowserApproval(request);
  const key = browserApprovalKey(request);
  const latest = useRef(key); latest.current = key;
  const [deciding, setDeciding] = useState<string | null>(null);
  const sent = useRef<string | null>(null);
  const busy = deciding === key;
  useEffect(() => { onPresent(); }, [key, onPresent]);
  const decide = (allow: boolean) => {
    if (latest.current !== key || sent.current === key || (allow && !details.canAuthorize())) return;
    sent.current = key; setDeciding(key);
    onDecide(request.req_id, allow, false);
  };
  return <div className="auth" data-testid="approval-card" data-req={request.req_id} tabIndex={-1}
    role="alertdialog" aria-modal="true" aria-label="Browser authorization required"
    onKeyDownCapture={event => {
      if ((event.key === "Enter" || event.key === " ") && !(event.target instanceof Element && event.target.closest('[data-testid="approval-deny"]'))) event.preventDefault();
    }}>
    <h3>AUTHORIZATION REQUIRED</h3>
    <div className="tool" data-testid="approval-tool">Browser action</div>
    {details.detail ? <BrowserApprovalBody detail={details.detail} /> : null}
    {details.message ? <p role="status">{details.message}</p> : null}
    <div className="btns">
      <button type="button" className="deny" disabled={busy} onClick={() => decide(false)} data-testid="approval-deny">DENY</button>
      <button type="button" disabled={busy || !details.canAuthorize()} tabIndex={-1} onMouseDown={noPointerFocus}
        onClick={() => decide(true)} data-testid="approval-allow">AUTHORIZE</button>
    </div>
    <div className="foot">DENIES AUTOMATICALLY IN {details.left}s · ESC TO DENY · HUD ONLY</div>
  </div>;
}
