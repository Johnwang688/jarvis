import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { validAuthFields, type BrowserAuth as Challenge } from "../lib/browserauth";
import "./BrowserAuth.css";

export function BrowserAuth({ challenge, blocked, onAnswer, onCancel }: {
  challenge: Challenge; blocked: boolean;
  onAnswer: (id: string, username: string, secret: string) => boolean;
  onCancel: (id: string) => boolean;
}) {
  const latest = useRef({ id: challenge.id, blocked }); latest.current = { id: challenge.id, blocked };
  const alive = useRef(false), invalidated = useRef<string | null>(null);
  const [username, setUsername] = useState(""), [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [now, setNow] = useState(Date.now);
  const deadline = useMemo(() => Date.now() + challenge.expiresIn * 1000, [challenge.id]);
  const sent = useRef<string | null>(null);
  useLayoutEffect(() => {
    alive.current = true; setUsername(""); setSecret(""); setBusy(false); setError(""); sent.current = null; invalidated.current = null;
    return () => { alive.current = false; };
  }, [challenge.id]);
  useLayoutEffect(() => {
    if (blocked) { invalidated.current = challenge.id; setUsername(""); setSecret(""); }
  }, [challenge.id, blocked]);
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => { const at = Date.now(); setNow(at); if (at >= deadline) clearInterval(timer); }, 250);
    return () => clearInterval(timer);
  }, [deadline]);
  const eligible = () => alive.current && latest.current.id === challenge.id && !latest.current.blocked
    && invalidated.current !== challenge.id && sent.current !== challenge.id && Date.now() < deadline;
  const finish = (cancel: boolean) => {
    if (!eligible() || (!cancel && !validAuthFields(username, secret))) return;
    sent.current = challenge.id; setBusy(true); setError("");
    try {
      if (!(cancel ? onCancel(challenge.id) : onAnswer(challenge.id, username, secret))) {
        sent.current = null; setBusy(false); setError("Authentication could not be sent. Try again after reconnecting.");
      }
    } catch { sent.current = null; setBusy(false); setError("Authentication could not be sent. Try again after reconnecting."); }
    finally { setUsername(""); setSecret(""); }
  };
  const disabled = blocked || busy || invalidated.current === challenge.id || now >= deadline;
  return <form className="browserauthform pad" data-testid="browser-auth" autoComplete="off"
    onSubmit={event => { event.preventDefault(); finish(false); }} onKeyDown={event => {
      if (event.key === "Escape" && !latest.current.blocked) { event.preventDefault(); event.stopPropagation(); finish(true); }
      if (event.key === "Enter" && !(event.target instanceof HTMLInputElement) && !(event.target instanceof HTMLButtonElement)) event.preventDefault();
    }}>
    <strong>This site requires authentication</strong>
    <p>Server: <bdi>{challenge.origin}</bdi></p>
    {challenge.realm ? <p>The server calls it: <bdi>{challenge.realm}</bdi></p> : null}
    <label>Username<input data-testid="browser-auth-user" value={username} onChange={e => setUsername(e.target.value)} disabled={disabled}
      autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={1024} /></label>
    <label>Password<input data-testid="browser-auth-secret" type="password" value={secret} onChange={e => setSecret(e.target.value)} disabled={disabled}
      autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={4096} /></label>
    <div className="row"><button type="button" disabled={disabled} onClick={() => finish(true)}>Cancel</button>
      <button type="submit" disabled={disabled || !validAuthFields(username, secret)}>Sign in</button></div>
    <p className="muted" role="status">{now >= deadline ? "Authentication expired. Reload to retry." : invalidated.current === challenge.id ? "Authentication changed. Wait for a fresh request." : busy ? "Waiting for the server…" : `Expires in ${Math.max(0, Math.ceil((deadline - now) / 1000))}s`}</p>
    {error ? <p className="err" role="status">{error}</p> : null}
  </form>;
}
