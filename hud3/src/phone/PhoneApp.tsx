import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Orb } from "../Orb";
import { Dashboard } from "./Dashboard";
import { pair, readSession, signIn } from "./auth";

// The fragment is consumed by the entry point, before the first render or request.
export function takePairingCode(place: Location, navigation: History): string | undefined {
  if (place.pathname !== "/m/pair") return undefined;
  const code = place.hash.slice(1);
  navigation.replaceState(null, "", "/m/pair");
  return /^[A-Za-z0-9_-]{20,200}$/.test(code) ? code : "";
}

function Gate({ heading, children }: { heading: string; children?: ReactNode }) {
  return (
    <main className="phone-shell phone-gate">
      <section className="agent-panel" aria-labelledby="phone-heading">
        <Orb />
        <h1 id="phone-heading">{heading}</h1>
        {children}
      </section>
    </main>
  );
}

function PasskeyButton({ action, label, failure }: {
  action: (signal: AbortSignal) => Promise<void>;
  label: string;
  failure: string;
}) {
  const operation = useRef<AbortController>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => () => operation.current?.abort(), []);

  async function run() {
    if (operation.current) return;
    const request = new AbortController();
    operation.current = request;
    setError(false);
    setBusy(true);
    try {
      await action(request.signal);
    } catch {
      if (!request.signal.aborted) setError(true);
    } finally {
      operation.current = undefined;
      if (!request.signal.aborted) setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="phone-primary" disabled={busy} onClick={() => void run()}>{label}</button>
      {busy && <p role="status">Waiting for passkey…</p>}
      {error && <p className="phone-error" role="status">{failure}</p>}
    </>
  );
}

function SignIn({ paired, onSignedIn }: { paired: boolean; onSignedIn: () => void }) {
  return (
    <Gate heading="Sign in to Jarvis">
      {paired && <p>Your passkey is ready.</p>}
      <PasskeyButton label="Sign in with passkey" failure="Could not sign in with your passkey."
        action={async (signal) => {
          await signIn(signal);
          if (!signal.aborted) onSignedIn();
        }} />
    </Gate>
  );
}

function Pair({ code, onPaired }: { code: string; onPaired: () => void }) {
  return (
    <Gate heading="Pair this phone">
      {code ? <PasskeyButton label="Create passkey" failure="Could not pair this phone. Use a new pairing link if this one has expired."
        action={async (signal) => {
          await pair(code, signal);
          if (!signal.aborted) onPaired();
        }} /> : <p>Open a new pairing link from the desk.</p>}
    </Gate>
  );
}

type Session = { state: "loading" | "signed-out" | "error" } | { state: "signed-in"; seconds: number };

export function PhoneApp({ pairingCode }: { pairingCode?: string }) {
  const [code, setCode] = useState(pairingCode);
  const [paired, setPaired] = useState(false);
  const [session, setSession] = useState<Session>({ state: "loading" });
  const [check, setCheck] = useState(pairingCode === undefined ? 0 : undefined);
  const checkSession = () => {
    setSession({ state: "loading" });
    setCheck((current) => (current ?? 0) + 1);
  };
  const signedOut = useCallback(() => {
    setPaired(false);
    setSession({ state: "signed-out" });
  }, []);

  useEffect(() => {
    if (check === undefined) return;
    const request = new AbortController();
    void readSession(request.signal).then((seconds) => {
      if (!request.signal.aborted) {
        setSession(seconds === null ? { state: "signed-out" } : { state: "signed-in", seconds });
      }
    }).catch(() => {
      if (!request.signal.aborted) setSession({ state: "error" });
    });
    return () => request.abort();
  }, [check]);

  if (code !== undefined) {
    return <Pair code={code} onPaired={() => {
      history.replaceState(null, "", "/m/");
      setPaired(true);
      setCode(undefined);
      setSession({ state: "signed-out" });
    }} />;
  }
  if (session.state === "signed-in") return <Dashboard initialSeconds={session.seconds} onSignedOut={signedOut} />;
  if (session.state === "signed-out") return <SignIn paired={paired} onSignedIn={checkSession} />;
  if (session.state === "error") {
    return <Gate heading="Jarvis is unavailable"><button type="button" onClick={checkSession}>Try again</button></Gate>;
  }
  return <Gate heading="Jarvis"><p role="status">Checking sign-in…</p></Gate>;
}
