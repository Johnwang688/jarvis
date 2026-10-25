import { useCallback, useEffect, useRef, useState } from "react";
import { ago, countdown, day, phoneApi, PhoneError, type PhoneRemote, type PhoneView } from "../lib/phone";
import { qrPath } from "../lib/qr";
import "./PhoneSettings.css";

/** Settings > Phone (phone design PH-6, P-2): pair a phone from the desk, see
 * the paired passkeys and their sessions, revoke a passkey or end a session.
 * The routes are owner-only on the HUD listener. Everything a phone sent (its
 * user agent) and Tailscale's identity headers are untrusted text: rendered as
 * React text only, never markup, never a link. */
export function PhoneSettings({ blocked }: { blocked: boolean }) {
  const [view, setView] = useState<PhoneView | null>(null);
  const [remote, setRemote] = useState<PhoneRemote | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pairing, setPairing] = useState<{ url: string; until: number; paired: number } | null>(null);
  const [left, setLeft] = useState(0);
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);
  const live = useRef(true);

  // `/status` (the listener's state) on opening and after each action; the
  // 2 s poll while a window is open reads `/phone` alone, which carries the
  // same `remote` block, so it never runs the providers' health checks.
  const refresh = useCallback(async (withStatus = true) => {
    try {
      const [next, status] = await Promise.all([
        phoneApi.view(), withStatus ? phoneApi.remote().catch(() => null) : Promise.resolve(null)]);
      if (!live.current) return null;
      setView(next);
      setRemote((was) => status ?? next.remote ?? was);
      return next;
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e.message : "The phone settings could not be read.");
      return null;
    }
  }, []);

  useEffect(() => {
    live.current = true;
    void refresh();
    return () => { live.current = false; };
  }, [refresh]);

  // While a window is open: the countdown, and a look every 2 s for the
  // registration that closes it.
  useEffect(() => {
    if (!pairing) return;
    const tick = () => {
      const seconds = (pairing.until - performance.now()) / 1000;
      setLeft(seconds);
      if (seconds <= 0) {
        setPairing(null);
        setNotice("The pairing window closed.");
      }
    };
    tick();
    const timer = window.setInterval(tick, 250);
    const poll = window.setInterval(async () => {
      const next = await refresh(false);
      if (!next || next.pairing.open) return;
      setPairing(null);
      setNotice(next.passkeys.length > pairing.paired ? "Paired. The phone can sign in now." : "The pairing window closed.");
    }, 2000);
    return () => { window.clearInterval(timer); window.clearInterval(poll); };
  }, [pairing, refresh]);

  const act = async (what: () => Promise<unknown>) => {
    if (blocked || busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await what(); }
    catch (e) { setError(e instanceof PhoneError || e instanceof Error ? e.message : "That did not work."); }
    finally { setBusy(false); setConfirm(null); await refresh(); }
  };

  const open = () => act(async () => {
    const opened = await phoneApi.pair();
    if (typeof opened.url !== "string" || !opened.url.startsWith("https://") || !(opened.expires_in > 0))
      throw new Error("The pairing window could not be read back.");
    setPairing({ url: opened.url, until: performance.now() + opened.expires_in * 1000,
      paired: view ? view.passkeys.length : 0 });
  });
  const cancel = () => act(async () => { await phoneApi.cancel(); setPairing(null); setNotice("Pairing cancelled."); });

  const passkeys = view ? view.passkeys : [];
  const full = view ? passkeys.length >= view.max_passkeys : false;
  const listening = remote ? remote.bound : false;
  const qr = pairing ? qrPath(pairing.url) : null;

  return <div className="phone-settings" data-testid="phone-settings">
    <div className="block">
      <h3>Remote listener</h3>
      {remote === null ? <p className="muted">Waiting for the daemon's status.</p>
        : remote.bound ? <p data-testid="phone-listener" data-bound="true">
            Listening on 127.0.0.1:{String(remote.port)} for the tailnet (tailscale serve).</p>
          : <p data-testid="phone-listener" data-bound="false" className="phone-warn">
            {remote.host_configured ? "Not running: the port could not be bound."
              : "Off: no tailnet name is configured (JARVIS_REMOTE_HOST)."}</p>}
    </div>

    <div className="block">
      <h3>Pair a phone</h3>
      <p className="muted">Pairing registers a passkey on the phone. An iCloud Keychain passkey syncs to
        every Apple device on that Apple ID, and each of them can sign in. At most {String(view ? view.max_passkeys : 3)} passkeys.</p>
      {pairing && qr ? <div className="phone-pairing" data-testid="phone-pairing">
        <svg className="phone-qr" data-testid="phone-qr" viewBox={`0 0 ${qr.size} ${qr.size}`}
          role="img" aria-label="Pairing QR code" shapeRendering="crispEdges">
          <rect width={qr.size} height={qr.size} fill="#ffffff" />
          <path d={qr.d} fill="#000000" />
        </svg>
        <div className="phone-pairing-text">
          <p>Scan with the iPhone's camera, or open this address on it. It works once.</p>
          <p className="phone-url" data-testid="phone-url">{pairing.url}</p>
          <p data-testid="phone-countdown" className="phone-countdown">{countdown(left)} left</p>
          <button type="button" data-testid="phone-cancel" disabled={blocked || busy} onClick={cancel}>Cancel</button>
        </div>
      </div>
        : <button type="button" data-testid="phone-pair" disabled={blocked || busy || !view || !listening || full || view.state !== "ok"}
          onClick={open}>Pair a phone</button>}
      {full && !pairing ? <p className="muted">Three passkeys are paired. Revoke one to pair another.</p> : null}
      {view && !pairing && view.pairing.open ? <p className="muted" data-testid="phone-window-open">
        A pairing window is open ({countdown(view.pairing.expires_in)} left). Pair again for a new code, which ends it.</p> : null}
      {notice ? <p className="muted" role="status" data-testid="phone-notice">{notice}</p> : null}
    </div>

    <div className="block">
      <h3>Paired passkeys</h3>
      {view && view.state !== "ok" ? <p className="err" role="status">The passkey file cannot be read. Pairing and phone sign-in are off.</p> : null}
      {view && passkeys.length === 0 ? <p className="muted" data-testid="phone-none">No passkeys are paired.</p> : null}
      <ul className="phone-passkeys">
        {passkeys.map((p) => <li key={p.key} className="phone-passkey" data-testid="phone-passkey" data-key={p.key}>
          <div className="phone-passkey-head">
            <span className="phone-passkey-name">Passkey {p.key.slice(2, 8)}</span>
            <span className="muted"> · paired {day(p.created)}{p.backed_up ? " · syncs through iCloud Keychain" : ""}
              {p.last_used ? ` · last sign-in ${ago(p.last_used)}` : ""}</span>
            {confirm === p.key
              ? <span className="phone-confirm">
                  <span>Revoke it and end {String(p.sessions.length)} session{p.sessions.length === 1 ? "" : "s"}?</span>
                  <button type="button" className="phone-danger" data-testid="phone-revoke-confirm"
                    disabled={blocked || busy} onClick={() => act(() => phoneApi.revoke(p.key))}>Revoke</button>
                  <button type="button" data-testid="phone-revoke-keep" disabled={busy} onClick={() => setConfirm(null)}>Keep</button>
                </span>
              : <button type="button" data-testid="phone-revoke" disabled={blocked || busy}
                  onClick={() => setConfirm(p.key)}>Revoke</button>}
          </div>
          {p.sessions.length === 0 ? <p className="muted phone-sessions-none">No sessions.</p> :
            <ul className="phone-sessions">
              {p.sessions.map((s) => <li key={s.id} className="phone-session" data-testid="phone-session" data-id={s.id}>
                <span className="phone-ua" data-testid="phone-ua">{s.ua || "(no user agent)"}</span>
                <span className="muted"> · last seen {ago(s.last_seen)}</span>
                {s.advisory.login ? <span className="muted phone-advisory" data-testid="phone-advisory">
                  {" · Tailscale says "}{s.advisory.login}{" (unverified)"}</span> : null}
                <button type="button" data-testid="phone-end" disabled={blocked || busy}
                  onClick={() => act(() => phoneApi.end(s.id))}>End</button>
              </li>)}
            </ul>}
        </li>)}
      </ul>
      {error ? <p className="err" role="status" data-testid="phone-error">{error}</p> : null}
    </div>
  </div>;
}
