import { useCallback, useEffect, useRef, useState } from "react";
import { ago, phoneApi, validTime, type PushView } from "../lib/phone";

/** Settings > Phone > Notifications (phone design §7, P-6): the owner's
 * on/off switch for Web Push, quiet hours (one setting: on/off and two local
 * times; nothing is pushed inside them, and one push says what still waits
 * when they end), the phones that turned notifications on (the push
 * service's host only, never the endpoint), and "Send test notification".
 * Owner-only routes on the HUD listener (`/phone/push`, `/quiet`). A
 * subscription's user agent came from the phone: rendered as text only. */
export function PhoneNotifications({ blocked }: { blocked: boolean }) {
  const [view, setView] = useState<PushView | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const live = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await phoneApi.push();
      if (!live.current) return;
      setView(next);
      if (next.quiet) { setStart(next.quiet.start); setEnd(next.quiet.end); }
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e.message : "The notification settings could not be read.");
    }
  }, []);

  useEffect(() => {
    live.current = true;
    void refresh();
    return () => { live.current = false; };
  }, [refresh]);

  const act = async (what: () => Promise<string | void>) => {
    if (blocked || busy) return;
    setBusy(true); setError(""); setNotice("");
    try { const said = await what(); if (said) setNotice(said); }
    catch (e) { setError(e instanceof Error ? e.message : "That did not work."); }
    finally { setBusy(false); await refresh(); }
  };

  const quiet = view?.quiet ?? null;
  const timesOk = validTime(start) && validTime(end) && start !== end;
  const timesChanged = quiet !== null && (start !== quiet.start || end !== quiet.end);

  return <div className="block phone-notify" data-testid="phone-notify">
    <h3>Notifications</h3>
    <p className="muted">A notification says only how many things wait on you (an approval, a blocking
      question, a proposal, a blocked task), never what they are. At most one a minute.</p>
    {view ? <>
      <label className="phone-notify-row">
        <input type="checkbox" data-testid="phone-notify-enabled" checked={view.enabled}
          disabled={blocked || busy} onChange={(e) => {
            const on = e.currentTarget.checked;
            void act(async () => { await phoneApi.pushEnabled(on); });
          }} />
        <span>Send notifications to the phone</span>
      </label>
      {quiet ? <div className="phone-notify-quiet" data-testid="phone-quiet">
        <label className="phone-notify-row">
          <input type="checkbox" data-testid="phone-quiet-enabled" checked={quiet.enabled}
            disabled={blocked || busy} onChange={(e) => {
              const on = e.currentTarget.checked;
              void act(async () => { await phoneApi.setQuiet({ enabled: on }); });
            }} />
          <span>Quiet hours, from</span>
          <input type="time" data-testid="phone-quiet-start" value={start} disabled={blocked || busy}
            onChange={(e) => setStart(e.currentTarget.value)} />
          <span>to</span>
          <input type="time" data-testid="phone-quiet-end" value={end} disabled={blocked || busy}
            onChange={(e) => setEnd(e.currentTarget.value)} />
          {timesChanged ? <button type="button" data-testid="phone-quiet-save" disabled={blocked || busy || !timesOk}
            onClick={() => void act(async () => { await phoneApi.setQuiet({ start, end }); return "Quiet hours saved."; })}>
            Save</button> : null}
        </label>
        <p className="muted" data-testid="phone-quiet-line">{quiet.enabled
          ? `Nothing is pushed in quiet hours. When they end, one notification says what still waits.${
            quiet.quiet_now ? " Quiet now." : ""}`
          : "Off: notifications are sent at any hour."}</p>
      </div> : null}
      {view.subscriptions.length === 0
        ? <p className="muted" data-testid="phone-notify-none">No phone has turned notifications on. On the
            iPhone, open Jarvis from the Home Screen and tap "Turn on notifications".</p>
        : <ul className="phone-sessions" data-testid="phone-notify-list">
            {view.subscriptions.map((s) => <li key={s.id} className="phone-session" data-testid="phone-notify-sub">
              <span className="phone-ua">{s.ua || "(no user agent)"}</span>
              <span className="muted"> · through {s.service} · since {ago(s.created)}</span>
            </li>)}
          </ul>}
      <button type="button" data-testid="phone-notify-test"
        disabled={blocked || busy || !view.enabled || !view.running || view.subscriptions.length === 0}
        onClick={() => void act(async () => {
          const r = await phoneApi.pushTest();
          return r.sent ? `Sent to ${r.sent} phone${r.sent === 1 ? "" : "s"}.`
            : `Not delivered${r.removed ? `: ${r.removed} subscription${r.removed === 1 ? " was" : "s were"} gone` : ""}.`;
        })}>Send test notification</button>
      {!view.running ? <p className="muted" data-testid="phone-notify-stopped">This daemon does not send
        notifications (it was not started as the owner's daemon).</p> : null}
    </> : <p className="muted">Reading…</p>}
    {notice ? <p className="muted" role="status" data-testid="phone-notify-notice">{notice}</p> : null}
    {error ? <p className="err" role="status" data-testid="phone-notify-error">{error}</p> : null}
  </div>;
}
