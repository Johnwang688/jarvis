import { apiFetch, type PhoneApiName } from "../api";
import { credentialJSON, prepareCreation, prepareRequest } from "./webauthn";

const readOptions = { credentials: "same-origin", cache: "no-store", redirect: "error" } as const;

export function postAuth(name: Exclude<PhoneApiName, "me">, body: object = {}, signal?: AbortSignal) {
  return apiFetch(name, {
    ...readOptions, method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: requestSignal(signal),
  });
}

function requestSignal(signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(15_000);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function unlockSeconds(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

export async function readSession(signal: AbortSignal): Promise<number | null> {
  const response = await apiFetch("me", { ...readOptions, signal: requestSignal(signal) });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Phone session unavailable");
  const body = await response.json();
  if (body?.signed_in !== true) throw new Error("Invalid phone session");
  return unlockSeconds(body.unlock_s);
}

export async function signIn(signal: AbortSignal): Promise<void> {
  const options = await postAuth("login/options", {}, signal);
  if (!options.ok) throw new Error("Passkey options unavailable");
  const credential = await navigator.credentials.get({
    publicKey: prepareRequest(await options.json()), signal,
  });
  if (!credential) throw new Error("No passkey assertion");
  const verified = await postAuth("login/verify", { credential: credentialJSON(credential as PublicKeyCredential) }, signal);
  if (!verified.ok) throw new Error("Passkey refused");
}

export async function pair(code: string, signal: AbortSignal): Promise<void> {
  const options = await postAuth("pair/options", { code }, signal);
  if (!options.ok) throw new Error("Pairing options unavailable");
  const credential = await navigator.credentials.create({
    publicKey: prepareCreation(await options.json()), signal,
  });
  if (!credential) throw new Error("No passkey created");
  const verified = await postAuth("pair/verify", { code, credential: credentialJSON(credential as PublicKeyCredential) }, signal);
  if (!verified.ok) throw new Error("Pairing refused");
}

export async function unlockPhone(signal: AbortSignal): Promise<number> {
  const options = await postAuth("unlock/options", {}, signal);
  if (!options.ok) throw new Error("Unlock options unavailable");
  const { publicKey } = await options.json();
  const credential = await navigator.credentials.get({ publicKey: prepareRequest(publicKey), signal });
  if (!credential) throw new Error("No passkey assertion");
  const verified = await postAuth("unlock", { assertion: credentialJSON(credential as PublicKeyCredential) }, signal);
  if (!verified.ok) throw new Error("Unlock refused");
  const body = await verified.json();
  const seconds = unlockSeconds(body?.unlock_s);
  if (body?.unlocked !== true || !seconds) throw new Error("Invalid unlock result");
  return seconds;
}
