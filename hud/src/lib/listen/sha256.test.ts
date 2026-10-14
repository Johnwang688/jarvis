// @vitest-environment node
import { createHash, randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";
import { sha256Hex, sha256HexSync } from "./sha256";

const node = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

describe("sha256", () => {
  it("matches the published vectors", () => {
    const enc = new TextEncoder();
    expect(sha256HexSync(enc.encode(""))).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(sha256HexSync(enc.encode("abc"))).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(sha256HexSync(enc.encode("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")))
      .toBe("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
  });

  it("agrees with node's on every length around the block and padding boundaries", () => {
    for (let n = 0; n <= 200; n++) {
      const b = new Uint8Array(randomBytes(n));
      expect(sha256HexSync(b), `length ${n}`).toBe(node(b));
    }
  });

  it("agrees on a model-sized buffer", () => {
    const b = new Uint8Array(randomBytes(2_327_524));
    expect(sha256HexSync(b)).toBe(node(b));
  });

  it("uses the same answer with and without crypto.subtle (an insecure-origin window has none)", async () => {
    const b = new Uint8Array(randomBytes(10_000));
    const want = node(b);
    expect(await sha256Hex(b)).toBe(want);
    const real = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", { value: {}, configurable: true });
    try { expect(await sha256Hex(b)).toBe(want); }
    finally { Object.defineProperty(globalThis, "crypto", { value: real, configurable: true }); }
  });
});
