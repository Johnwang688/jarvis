// WebAuthn's JSON on the wire, as the P-0 spike sent it from the owner's
// iPhone (tests/spikes/p0_phone.py `credJSON`) and `webauthn` 3.0.1 reads it:
// every binary field base64url without padding, `clientExtensionResults`,
// `authenticatorAttachment`, `transports` on a registration, `userHandle` on
// an assertion when the authenticator gives one.

export function bytesToB64url(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let text = "";
  for (const b of bytes) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlToBytes(s: string): Uint8Array<ArrayBuffer> {
  let t = s.replace(/-/g, "+").replace(/_/g, "/");
  while (t.length % 4) t += "=";
  const raw = atob(t);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

type Json = Record<string, any>;

/** The server's creation options (`options_to_json`) as the browser takes them. */
export function prepareCreation(o: Json): PublicKeyCredentialCreationOptions {
  return {
    ...o,
    challenge: b64urlToBytes(o.challenge),
    user: { ...o.user, id: b64urlToBytes(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c: Json) => ({ ...c, id: b64urlToBytes(c.id) })),
  } as unknown as PublicKeyCredentialCreationOptions;
}

/** The server's request options as the browser takes them. */
export function prepareRequest(o: Json): PublicKeyCredentialRequestOptions {
  return {
    ...o,
    challenge: b64urlToBytes(o.challenge),
    allowCredentials: (o.allowCredentials || []).map((c: Json) => ({ ...c, id: b64urlToBytes(c.id) })),
  } as unknown as PublicKeyCredentialRequestOptions;
}

/** A credential from create() or get(), serialized as the spike did. */
export function credentialJSON(c: PublicKeyCredential): Json {
  const r = c.response as AuthenticatorAttestationResponse & AuthenticatorAssertionResponse;
  const out: Json = {
    id: c.id, rawId: bytesToB64url(c.rawId), type: c.type,
    clientExtensionResults: c.getClientExtensionResults(),
    authenticatorAttachment: c.authenticatorAttachment,
    response: { clientDataJSON: bytesToB64url(r.clientDataJSON) },
  };
  if ("attestationObject" in r && r.attestationObject) {
    out.response.attestationObject = bytesToB64url(r.attestationObject);
    out.response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
  }
  if ("authenticatorData" in r && r.authenticatorData) {
    out.response.authenticatorData = bytesToB64url(r.authenticatorData);
    out.response.signature = bytesToB64url(r.signature);
    if (r.userHandle) out.response.userHandle = bytesToB64url(r.userHandle);
  }
  return out;
}
