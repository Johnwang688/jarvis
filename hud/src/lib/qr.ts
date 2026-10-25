// A QR code as one SVG path, for the Phone settings' pairing code (phone
// design PH-6). The encoder is vendored (./vendor/qrcodegen.ts, Project
// Nayuki, MIT); nothing is fetched. Medium error correction: the URL is about
// 100 characters, which fits a version 6 or 7 symbol that a phone camera
// reads from a laptop screen at arm's length.
import { qrcodegen } from "./vendor/qrcodegen";

export interface QrPath {
  /** Modules per side, the quiet zone included. */
  size: number;
  /** One SVG path: a 1x1 square per dark module, in module units. */
  d: string;
}

/** The QR code for `text`, with a quiet zone of `border` modules. */
export function qrPath(text: string, border = 4): QrPath {
  const code = qrcodegen.QrCode.encodeText(text, qrcodegen.QrCode.Ecc.MEDIUM);
  const parts: string[] = [];
  for (let y = 0; y < code.size; y++) {
    for (let x = 0; x < code.size; x++) {
      if (code.getModule(x, y)) parts.push(`M${x + border} ${y + border}h1v1h-1z`);
    }
  }
  return { size: code.size + border * 2, d: parts.join("") };
}

/** The module grid itself (true = dark), for the tests. */
export function qrModules(text: string): boolean[][] {
  const code = qrcodegen.QrCode.encodeText(text, qrcodegen.QrCode.Ecc.MEDIUM);
  const rows: boolean[][] = [];
  for (let y = 0; y < code.size; y++) {
    const row: boolean[] = [];
    for (let x = 0; x < code.size; x++) row.push(code.getModule(x, y));
    rows.push(row);
  }
  return rows;
}
