// The approval card's words, shared by the HUD's card (components/Approvals.tsx,
// components/BrowserApproval.tsx) and the phone's answering card
// (mobile/card.ts, phone design P-4): one place decides what each line of a
// card says, so the phone shows exactly the text the desk shows.
//
// Pure: strings and segments, no markup. Every value is untrusted (a model
// wrote most of it, a page wrote the Browser fields) and each surface draws
// it as text: React text on the desk, `textContent` on the phone. A segment
// marked `bdi` is drawn in its own <bdi>, so page text cannot reorder the
// words around it.

import type { BrowserPrivateDetail } from "./browserapproval";

/** One argument's value as the card shows it: a string as typed, anything else as JSON. */
export function argText(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** The attribution line, or "" when there is none. */
export function originText(origin: unknown): string {
  return typeof origin === "string" && origin ? `REQUESTED BY ${origin}` : "";
}

/** The reason line: the layer that asked, in brackets, then the reason; "" when there is no reason. */
export function reasonText(reason: unknown, layer: unknown): string {
  if (typeof reason !== "string" || !reason) return "";
  return `${typeof layer === "string" && layer ? `[${layer}] ` : ""}${reason}`;
}

/** "DENIES AUTOMATICALLY IN 2m" / "45s": the countdown's words. */
export function countdownText(left: number): string {
  return left > 120 ? `${Math.ceil(left / 60)}m` : `${Math.max(0, left)}s`;
}

export interface Seg { text: string; bdi?: boolean }
export interface Para { segs: Seg[]; muted?: boolean; testid?: string; pre?: boolean }
export interface BrowserBody { testid: string; headline: string; paras: Para[]; command: string | null }

/** A HUD-only (Browser) card's body, from its parsed private detail. */
export function browserBody(detail: BrowserPrivateDetail): BrowserBody {
  if (detail.kind === "delegation") {
    return {
      testid: "browser-delegation-details",
      headline: "Allow this Browser tool to reach its safety checks?",
      paras: [
        { segs: [{ text: "Your named rule requires permission before the Browser tool can proceed." }] },
        { segs: [{ text: "This grants no Browser action. The requested action must still pass the Browser safety checks and may require a separate authorization." }] },
      ],
      command: null,
    };
  }
  const b = detail.browser, release = b.action === "release_download";
  const paras: Para[] = [{ segs: [{ text: `Pane ${b.pane} · ` }, { text: b.action.replaceAll("_", " "), bdi: true },
    { text: " · " }, { text: b.category.replaceAll("_", " "), bdi: true }] }];
  if (b.host || b.path) paras.push({ segs: [{ text: "Page: " }, { text: `${b.host}${b.path}`, bdi: true }] });
  if (b.download) {
    paras.push({ segs: [{ text: "File: " }, { text: b.download.name, bdi: true },
      { text: ` · ${b.download.size.toLocaleString()} bytes · ID ` }, { text: b.download.id, bdi: true }] });
  } else if (b.target) {
    paras.push({ segs: [{ text: "The page calls it: " }, { text: b.target, bdi: true }] });
  }
  paras.push({ segs: [{ text: "Preview unavailable" }], muted: true, testid: "browser-approval-preview" });
  paras.push({ segs: [{ text: release ? "This releases the file to you. It does not run it."
    : "Page labels are untrusted page text. Read the exact action below." }] });
  return {
    testid: "browser-approval-details",
    headline: release ? "Release this executable download?" : "Authorize this one Browser action?",
    paras,
    command: detail.command,
  };
}
