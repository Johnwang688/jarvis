import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { b64urlToBytes, bytesToB64url, credentialJSON, prepareCreation, prepareRequest } from "./webauthn";

const CODE = "c0de_C0DE-" + "x".repeat(33);

function buf(text: string): ArrayBuffer {
  return new TextEncoder().encode(text).buffer as ArrayBuffer;
}

/** What Safari hands back, in shape. */
function fakeCredential(kind: "create" | "get") {
  const response: Record<string, unknown> = { clientDataJSON: buf('{"type":"webauthn.' + kind + '"}') };
  if (kind === "create") {
    response.attestationObject = buf("att");
    response.getTransports = () => ["internal", "hybrid"];
  } else {
    response.authenticatorData = buf("auth");
    response.signature = buf("sig");
    response.userHandle = buf("owner");
  }
  return {
    id: bytesToB64url(new Uint8Array([1, 2, 255])), rawId: new Uint8Array([1, 2, 255]).buffer, type: "public-key",
    authenticatorAttachment: "platform", getClientExtensionResults: () => ({}), response,
  } as unknown as PublicKeyCredential;
}

describe("WebAuthn JSON, as the P-0 spike sent it", () => {
  it("round-trips base64url without padding", () => {
    for (const bytes of [[], [0], [255, 254], [1, 2, 3, 4, 5], Array.from({ length: 64 }, (_, i) => i * 4)]) {
      const text = bytesToB64url(new Uint8Array(bytes));
      expect(text).not.toMatch(/[+/=]/);
      expect(Array.from(b64urlToBytes(text))).toEqual(bytes);
    }
  });

  it("serializes a registration and an assertion field for field", () => {
    const reg = credentialJSON(fakeCredential("create"));
    expect(reg).toEqual({
      id: "AQL_", rawId: "AQL_", type: "public-key", clientExtensionResults: {}, authenticatorAttachment: "platform",
      response: { clientDataJSON: bytesToB64url(buf('{"type":"webauthn.create"}')),
        attestationObject: bytesToB64url(buf("att")), transports: ["internal", "hybrid"] },
    });
    const get = credentialJSON(fakeCredential("get"));
    expect(get.response).toEqual({ clientDataJSON: bytesToB64url(buf('{"type":"webauthn.get"}')),
      authenticatorData: bytesToB64url(buf("auth")), signature: bytesToB64url(buf("sig")),
      userHandle: bytesToB64url(buf("owner")) });
  });

  it("turns the server's options into buffers", () => {
    const creation = prepareCreation({ challenge: "AAEC", rp: { id: "h", name: "Jarvis" },
      user: { id: "AQI", name: "owner" }, excludeCredentials: [{ id: "AQL_", type: "public-key" }] });
    expect(Array.from(creation.challenge as Uint8Array)).toEqual([0, 1, 2]);
    expect(Array.from(creation.user.id as Uint8Array)).toEqual([1, 2]);
    expect(Array.from(creation.excludeCredentials![0].id as Uint8Array)).toEqual([1, 2, 255]);
    const request = prepareRequest({ challenge: "AAEC", rpId: "h", allowCredentials: [], userVerification: "required" });
    expect(request.userVerification).toBe("required");
    expect(request.allowCredentials).toEqual([]);
  });
});

describe("the phone shell's pairing and sign-in (P-2)", () => {
  let calls: { path: string; body: any }[];
  let answers: Record<string, { status: number; body: unknown }>;
  let create: ReturnType<typeof vi.fn>;
  let get: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '<main class="shell" id="app"><p class="shell-line">Jarvis · phone</p></main>';
    calls = [];
    answers = {
      "/m/api/me": { status: 401, body: { error: "unauthorized" } },
      "/m/api/pair/options": { status: 200, body: { challenge: "AAEC", rp: { id: "h", name: "Jarvis" },
        user: { id: "AQI", name: "owner" }, excludeCredentials: [] } },
      "/m/api/pair/verify": { status: 200, body: { paired: true } },
      "/m/api/login/options": { status: 200, body: { challenge: "AAEC", rpId: "h", allowCredentials: [] } },
      "/m/api/login/verify": { status: 200, body: { signed_in: true } },
      "/m/api/logout": { status: 200, body: { signed_in: false } },
    };
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      calls.push({ path, body: init && init.body ? JSON.parse(String(init.body)) : null });
      const a = answers[path] || { status: 404, body: {} };
      return new Response(JSON.stringify(a.body), { status: a.status });
    }));
    create = vi.fn(async () => fakeCredential("create"));
    get = vi.fn(async () => fakeCredential("get"));
    Object.defineProperty(navigator, "credentials", { configurable: true, value: { create, get } });
    vi.stubGlobal("PublicKeyCredential", class {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    history.replaceState(null, "", "/");
  });

  const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)); };
  const screen = () => document.querySelector<HTMLElement>("[data-screen]")?.dataset.screen;
  const tap = async () => { document.querySelector<HTMLButtonElement>("button")!.click(); await settle(); };

  it("pairs from /m/pair#code, drops the code from the address at once, then signs in", async () => {
    history.replaceState(null, "", "/m/pair#" + CODE);
    await import("./main");
    await settle();
    expect(location.hash).toBe("");
    expect(location.pathname).toBe("/m/pair");
    expect(screen()).toBe("Pair this phone");
    expect(create).not.toHaveBeenCalled();              // only on a tap
    await tap();
    expect(calls.map((c) => c.path)).toEqual(["/m/api/pair/options", "/m/api/pair/verify"]);
    expect(calls[0].body).toEqual({ code: CODE });
    expect(calls[1].body.code).toBe(CODE);
    expect(calls[1].body.credential.response.attestationObject).toBe(bytesToB64url(buf("att")));
    const options = create.mock.calls[0][0].publicKey;
    expect(Array.from(options.challenge)).toEqual([0, 1, 2]);
    expect(screen()).toBe("Paired");
    expect(get).not.toHaveBeenCalled();
    await tap();
    expect(calls.map((c) => c.path).slice(2)).toEqual(["/m/api/login/options", "/m/api/login/verify"]);
    expect(screen()).toBe("Signed in · paired");
  });

  it("says a refused code plainly and never calls the authenticator", async () => {
    answers["/m/api/pair/options"] = { status: 401, body: { error: "unauthorized" } };
    history.replaceState(null, "", "/m/pair#" + CODE);
    await import("./main");
    await settle();
    await tap();
    expect(create).not.toHaveBeenCalled();
    expect(screen()).toBe("Not paired");
    expect(document.body.textContent).toContain("not valid any more");
  });

  it("refuses a pairing link with no code", async () => {
    history.replaceState(null, "", "/m/pair");
    await import("./main");
    await settle();
    expect(screen()).toBe("Not paired");
    expect(calls.map((c) => c.path)).toEqual([]);
  });

  it("shows sign-in when signed out, and the signed-in card with Sign out", async () => {
    history.replaceState(null, "", "/m/");
    await import("./main");
    await settle();
    expect(screen()).toBe("Jarvis");
    expect(document.querySelector("button")!.textContent).toBe("Sign in with passkey");
    await tap();
    expect(screen()).toBe("Signed in · paired");
    expect(document.querySelector("button")!.textContent).toBe("Sign out");
    await tap();
    expect(calls.map((c) => c.path).slice(-1)).toEqual(["/m/api/logout"]);
    expect(screen()).toBe("Jarvis");
  });

  it("opens signed in when the session is live", async () => {
    answers["/m/api/me"] = { status: 200, body: { signed_in: true } };
    history.replaceState(null, "", "/m/");
    await import("./main");
    await settle();
    expect(screen()).toBe("Signed in · paired");
  });

  it("puts server text in as text, never markup", async () => {
    answers["/m/api/login/options"] = { status: 409, body: { error: "<img src=x onerror=alert(1)>" } };
    history.replaceState(null, "", "/m/");
    await import("./main");
    await settle();
    await tap();
    expect(document.querySelector("img")).toBeNull();
    expect(document.body.textContent).toContain("<img src=x");
  });
});
