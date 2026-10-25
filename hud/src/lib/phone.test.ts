import { describe, expect, it } from "vitest";
import { ago, countdown } from "./phone";
import { qrModules, qrPath } from "./qr";
import vendored from "./vendor/qrcodegen.ts?raw";
import { qrcodegen } from "./vendor/qrcodegen";

describe("the pairing QR code (PH-6)", () => {
  const url = "https://jarvis-box.tail-test.ts.net/m/pair#" + "A".repeat(43);

  it("is a well-formed symbol: square, a version's size, three finder patterns", () => {
    const m = qrModules(url);
    const n = m.length;
    expect(m.every((row) => row.length === n)).toBe(true);
    expect((n - 17) % 4).toBe(0);
    const version = (n - 17) / 4;
    expect(version).toBeGreaterThanOrEqual(5);
    expect(version).toBeLessThanOrEqual(10);
    // A finder pattern: a dark 7x7 ring, a light ring, a dark 3x3 core.
    const finder = (x0: number, y0: number) => {
      for (let y = 0; y < 7; y++) for (let x = 0; x < 7; x++) {
        const ring = x === 0 || y === 0 || x === 6 || y === 6;
        const core = x >= 2 && x <= 4 && y >= 2 && y <= 4;
        expect(m[y0 + y][x0 + x]).toBe(ring || core);
      }
    };
    finder(0, 0); finder(n - 7, 0); finder(0, n - 7);
    // Timing patterns alternate between the finders.
    for (let i = 8; i < n - 8; i++) {
      expect(m[6][i]).toBe(i % 2 === 0);
      expect(m[i][6]).toBe(i % 2 === 0);
    }
  });

  it("carries medium error correction in its format bits", () => {
    const level = (m: boolean[][]) => {
      // Format bits 14..9 along row 8 (x = 0..5), then 7, 8, then column 8 upward.
      const bits: boolean[] = [];
      for (let x = 0; x <= 5; x++) bits.push(m[8][x]);
      bits.push(m[8][7], m[8][8], m[7][8]);
      for (let y = 5; y >= 0; y--) bits.push(m[y][8]);
      let value = 0;
      for (const b of bits) value = (value << 1) | (b ? 1 : 0);
      // Unmask (0x5412) and take the top two data bits: the level.
      return ((value ^ 0x5412) >> 13) & 0x3;
    };
    expect(level(qrModules(url))).toBe(0b00);       // 00 = Medium in the QR format word
    // The reading itself, checked against the other levels.
    const grid = (ecc: qrcodegen.QrCode.Ecc) => {
      const code = qrcodegen.QrCode.encodeText(url, ecc);
      return Array.from({ length: code.size }, (_, y) => Array.from({ length: code.size }, (_, x) => code.getModule(x, y)));
    };
    expect(level(grid(qrcodegen.QrCode.Ecc.LOW))).toBe(0b01);
    expect(level(grid(qrcodegen.QrCode.Ecc.QUARTILE))).toBe(0b11);
    expect(level(grid(qrcodegen.QrCode.Ecc.HIGH))).toBe(0b10);
  });

  it("is one path of unit squares inside a quiet zone, and changes with the code", () => {
    const a = qrPath(url);
    const b = qrPath(url.replace(/A{43}$/, "B".repeat(43)));
    expect(a.size).toBe(qrModules(url).length + 8);
    expect(a.d).toMatch(/^(M\d+ \d+h1v1h-1z)+$/);
    expect(a.d).not.toContain("M0 0");
    expect(a.d).not.toBe(b.d);
  });

  it("is vendored, fetched from nowhere", () => {
    expect(vendored).toContain("Copyright (c) Project Nayuki. (MIT License)");
    expect(vendored).not.toMatch(/\bfetch\(|import\(|https?:\/\/(?!github\.com|www\.nayuki\.io)/);
  });
});

describe("Phone settings text helpers", () => {
  it("counts down in minutes and seconds", () => {
    expect(countdown(300)).toBe("5:00");
    expect(countdown(299.2)).toBe("5:00");
    expect(countdown(61)).toBe("1:01");
    expect(countdown(0)).toBe("0:00");
    expect(countdown(-5)).toBe("0:00");
  });

  it("says how long ago", () => {
    const now = 1_900_000_000_000;
    expect(ago(now / 1000 - 5, now)).toBe("just now");
    expect(ago(now / 1000 - 600, now)).toBe("10 min ago");
    expect(ago(now / 1000 - 7200, now)).toBe("2 h ago");
    expect(ago(now / 1000 - 3 * 86400, now)).toBe("3 d ago");
  });
});
