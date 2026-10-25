// The phone shell's entry (phone surface design PH-9). P-2: pairing and
// passkey sign-in, nothing more; P-3 builds the Inbox, Chats and Tasks tabs on
// it. It shares code with the HUD only by importing it; the desktop's panes,
// editor, terminals and Browser never enter this bundle.
//
//   /m/pair#<code>  registers a passkey (the code came from the desk's QR
//                   code, in the fragment, which is never sent), then signs in;
//   /m/             "Sign in with passkey" when signed out, "Signed in ·
//                   paired" with Sign out when signed in.
//
// Every passkey call follows a tap: iOS asks for Face ID or the passcode only
// from a user gesture. Text from the server is set with textContent only.
import "./mobile.css";
import { credentialJSON, prepareCreation, prepareRequest } from "./webauthn";

type Json = Record<string, unknown>;

async function post(path: string, body: Json = {}): Promise<{ status: number; body: Json }> {
  const res = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let value: Json = {};
  try { value = (await res.json()) as Json; } catch { value = {}; }
  return { status: res.status, body: value };
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function root(): HTMLElement {
  return document.getElementById("app") as HTMLElement;
}

function screen(title: string, lines: string[], actions: HTMLElement[] = [], tone = ""): void {
  const card = el("section", "card" + (tone ? " " + tone : ""));
  card.dataset.screen = title;
  card.append(el("h1", "card-title", title));
  for (const line of lines) card.append(el("p", "card-line", line));
  for (const action of actions) card.append(action);
  root().replaceChildren(card);
}

function button(label: string, onClick: () => void, cls = "primary"): HTMLButtonElement {
  const b = el("button", "btn " + cls, label);
  b.type = "button";
  b.addEventListener("click", () => {
    if (b.disabled) return;
    b.disabled = true;
    onClick();
  });
  return b;
}

function why(status: number, body: Json): string {
  if (status === 401) return "Not accepted.";
  if (status === 409) return typeof body.error === "string" && body.error
    ? body.error[0].toUpperCase() + body.error.slice(1) + "." : "Try again.";
  if (status === 503) return "Passkeys are unavailable on the PC right now.";
  return `Something went wrong (${status}).`;
}

function failure(error: unknown): string {
  const name = error && typeof error === "object" && "name" in error ? String((error as { name: unknown }).name) : "";
  if (name === "NotAllowedError") return "Cancelled, or the passkey was not verified.";
  if (name === "InvalidStateError") return "This phone already holds a passkey for Jarvis.";
  return "The passkey could not be used.";
}

// -- signing in -------------------------------------------------------------------

async function signIn(): Promise<void> {
  try {
    const options = await post("/m/api/login/options");
    if (options.status !== 200) return signedOut(why(options.status, options.body));
    const credential = await navigator.credentials.get({ publicKey: prepareRequest(options.body) });
    if (!credential) return signedOut("The passkey could not be used.");
    const verified = await post("/m/api/login/verify", { credential: credentialJSON(credential as PublicKeyCredential) });
    if (verified.status !== 200) return signedOut(why(verified.status, verified.body));
    signedIn();
  } catch (error) {
    signedOut(failure(error));
  }
}

function signedOut(message = ""): void {
  const lines = ["Sign in with the passkey you paired at the desk. Face ID or the passcode confirms it is you."];
  if (message) lines.push(message);
  screen("Jarvis", lines, [button("Sign in with passkey", () => void signIn())], message ? "warn" : "");
}

function signedIn(): void {
  screen("Signed in · paired", ["This phone can reach Jarvis. The inbox, chats and tasks come next."],
    [button("Sign out", () => void signOut(), "quiet")], "ok");
}

async function signOut(): Promise<void> {
  try { await post("/m/api/logout"); } catch { /* the cookie ends with the session either way */ }
  signedOut("Signed out.");
}

// -- pairing -------------------------------------------------------------------

async function pair(code: string): Promise<void> {
  try {
    const options = await post("/m/api/pair/options", { code });
    if (options.status !== 200) return pairFailed(options.status === 401
      ? "This pairing code is not valid any more. Open a new one at the desk." : why(options.status, options.body));
    const credential = await navigator.credentials.create({ publicKey: prepareCreation(options.body) });
    if (!credential) return pairFailed("The passkey could not be created.");
    const verified = await post("/m/api/pair/verify", { code, credential: credentialJSON(credential as PublicKeyCredential) });
    if (verified.status !== 200) return pairFailed(why(verified.status, verified.body));
    screen("Paired", ["The passkey is saved. Sign in with it now."],
      [button("Sign in with passkey", () => void signIn())], "ok");
  } catch (error) {
    pairFailed(failure(error));
  }
}

function pairFailed(message: string): void {
  screen("Not paired", [message, "Open a new pairing window at the desk: Settings, Phone, Pair a phone."], [], "warn");
}

function pairing(code: string): void {
  screen("Pair this phone", [
    "This creates a passkey for Jarvis on this phone. Face ID or the passcode protects it.",
    "An iCloud Keychain passkey also syncs to your other Apple devices.",
  ], [button("Create passkey", () => void pair(code))]);
}

// -- start -----------------------------------------------------------------------

async function start(): Promise<void> {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    screen("Jarvis", ["This browser has no passkeys. Open it in Safari on the iPhone."], [], "warn");
    return;
  }
  if (location.pathname === "/m/pair") {
    const code = location.hash.slice(1);
    // The code leaves the address bar and the history at once.
    history.replaceState(null, "", "/m/pair");
    if (!code || !/^[A-Za-z0-9_-]{20,200}$/.test(code)) return pairFailed("This pairing link has no code.");
    return pairing(code);
  }
  try {
    const res = await fetch("/m/api/me", { credentials: "same-origin" });
    if (res.status === 200) return signedIn();
  } catch { /* offline: signed out below */ }
  signedOut();
}

void start();
