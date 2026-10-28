import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { browserApi } from "../api";
import { noPointerFocus } from "../lib/talkkey";
import { browserApprovalDeadline, browserApprovalKey, parseBrowserPrivateApproval, isBrowserPrivateRequest, type BrowserPrivateDetail, type BrowserApprovalPublic } from "../lib/browserapproval";
import { browserBody } from "../lib/approvalCard";
import { useArmed } from "../lib/armed";
import "./BrowserApproval.css";

export const PRIVATE_DETAIL_UNAVAILABLE = "The Browser action details are unavailable. Deny this request.";
/** Only the mounted top card calls this hook. No detail enters the public approval row. */
export function useBrowserApproval(request: BrowserApprovalPublic) {
  const key = browserApprovalKey(request);
  const current = useRef(key); current.current = key;
  const alive = useRef(false);
  useLayoutEffect(() => { alive.current = true; return () => { alive.current = false; }; }, [key]);
  const [result, setResult] = useState<{ key: string; detail: BrowserPrivateDetail | null; failed: boolean } | null>(null);
  const [now, setNow] = useState(Date.now);
  const eligible = isBrowserPrivateRequest(request);
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
      const detail = parseBrowserPrivateApproval(raw, request);
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

// The words are lib/approvalCard.ts's `browserBody`, which the phone's card
// (mobile/card.ts, P-4) draws too: the same text on both surfaces.
export function BrowserApprovalBody({ detail }: { detail: BrowserPrivateDetail }) {
  const body = browserBody(detail);
  return <div className="browserapproval" data-testid={body.testid}>
    <div className="headline">{body.headline}</div>
    {body.paras.map((p, i) => <p key={i} className={p.muted ? "muted" : undefined} data-testid={p.testid}>
      {p.segs.map((s, j) => s.bdi ? <bdi key={j}>{s.text}</bdi> : s.text)}
    </p>)}
    {body.command !== null ? <pre className="arg" data-testid="approval-command"><bdi>{body.command}</bdi></pre> : null}
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
  const chimed = useRef<string | null>(null);
  const busy = deciding === key;
  const armed = useArmed(`${key}:${details.detail ? "detail" : "none"}`);
  useEffect(() => { if (chimed.current !== key) { chimed.current = key; onPresent(); } }, [key, onPresent]);
  const decide = (allow: boolean) => {
    if (latest.current !== key || sent.current === key || (allow && (!armed || !details.canAuthorize()))) return;
    sent.current = key; setDeciding(key);
    onDecide(request.req_id, allow, false);
  };
  return <div className="auth browserauth" data-testid="approval-card" data-req={request.req_id} tabIndex={-1}
    role="alertdialog" aria-modal="true" aria-label="Browser authorization required"
    onKeyDownCapture={event => {
      if ((event.key === "Enter" || event.key === " ") && !(event.target instanceof Element && event.target.closest('[data-testid="approval-deny"]'))) event.preventDefault();
    }}>
    <h3>AUTHORIZATION REQUIRED</h3>
    {/* AS-12: the buttons first; AUTHORIZE armed ARM_MS after the card and again after its detail lands (AS-13). */}
    <div className="btns" data-testid="approval-buttons">
      <button type="button" className="deny" disabled={busy} onClick={() => decide(false)} data-testid="approval-deny">DENY</button>
      <button type="button" disabled={busy || !armed || !details.canAuthorize()} tabIndex={-1} onMouseDown={noPointerFocus}
        onClick={() => decide(true)} data-testid="approval-allow">AUTHORIZE</button>
    </div>
    <div className="tool" data-testid="approval-tool">{request.layer === "always-ask" ? "Browser tool permission" : "Browser action"}</div>
    {details.detail ? <BrowserApprovalBody detail={details.detail} /> : null}
    {details.message ? <p role="status">{details.message}</p> : null}
    <div className="foot">DENIES AUTOMATICALLY IN {details.left}s · ESC TO DENY · HUD ONLY</div>
  </div>;
}
