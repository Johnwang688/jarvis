/** Owner-only Browser details. Never put this object into the general approval store. */
export const BROWSER_CATEGORIES = ["login", "signup", "oauth", "two_factor", "purchase", "payment", "send_or_post", "delete", "account_security", "executable_download", "unrelated_to_request", "injection_suspected", "ordinary", "upload", "no_target", "unjudged"] as const;
export const BROWSER_ACTIONS = ["click", "type", "press", "scroll", "select", "upload", "navigate", "back", "forward", "release_download"] as const;
export interface BrowserApprovalPublic {
  req_id: string; tool: string; layer: string; hud_only?: boolean;
  code: string; thread_id: string | null; asked_at: string; timeout_s?: number;
}
export interface BrowserApprovalDetail {
  kind: "action"; reqId: string; command: string;
  browser: {
    tab_id: string; action_id: string; pane: number; page_identity: number; document_generation: number;
    action: typeof BROWSER_ACTIONS[number]; category: typeof BROWSER_CATEGORIES[number];
    host: string; path: string; target: string; text: string;
    download?: { id: string; name: string; size: number };
  };
}
export interface BrowserDelegationDetail { kind: "delegation"; reqId: string }
export type BrowserPrivateDetail = BrowserApprovalDetail | BrowserDelegationDetail;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown, max: number): v is string => typeof v === "string" && v.length <= max * 2 && [...v].length <= max;
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const id = (v: unknown, n: number): v is string => typeof v === "string" && new RegExp(`^[0-9a-f]{${n}}$`).test(v);
export function browserApprovalDeadline(request: BrowserApprovalPublic): number {
  const at = Date.parse(request.asked_at);
  return Number.isFinite(at) && at > 0 && typeof request.timeout_s === "number" && Number.isFinite(request.timeout_s)
    && request.timeout_s > 0 && request.timeout_s <= 120 ? at + request.timeout_s * 1000 : 0;
}
export function browserApprovalKey(request: BrowserApprovalPublic): string {
  return JSON.stringify([request.req_id, request.tool, request.layer, request.hud_only, request.code, request.thread_id, request.asked_at, request.timeout_s]);
}
export function isBrowserPrivateRequest(expected: BrowserApprovalPublic): boolean {
  return expected.hud_only === true && expected.tool === "hud_browser_act"
    && ["browser-gate", "always-ask"].includes(expected.layer) && /^[A-Za-z0-9_-]{12}$/.test(expected.req_id);
}
function matchingEnvelope(raw: unknown, expected: BrowserApprovalPublic): raw is Record<string, unknown> {
  return isBrowserPrivateRequest(expected) && !!browserApprovalDeadline(expected) && record(raw)
    && raw.hud_only === true && raw.allowlistable === false && raw.req_id === expected.req_id && raw.tool === expected.tool
    && raw.layer === expected.layer && raw.code === expected.code && raw.thread_id === expected.thread_id
    && raw.asked_at === expected.asked_at && raw.timeout_s === expected.timeout_s;
}
export function parseBrowserPrivateApproval(raw: unknown, expected: BrowserApprovalPublic): BrowserPrivateDetail | null {
  if (expected.layer === "browser-gate") return parseBrowserApproval(raw, expected);
  if (expected.layer !== "always-ask" || !matchingEnvelope(raw, expected)) return null;
  const b = raw.browser;
  if (raw.command !== null || !record(raw.args) || Object.keys(raw.args).length !== 0
    || !record(b) || Object.keys(b).length !== 2 || b.kind !== "delegation" || b.tool !== "hud_browser_act") return null;
  // This passes only a named tool rule. No action or page facts were judged here.
  return { kind: "delegation", reqId: expected.req_id };
}
export function parseBrowserApproval(raw: unknown, expected: BrowserApprovalPublic): BrowserApprovalDetail | null {
  if (expected.layer !== "browser-gate" || !matchingEnvelope(raw, expected)) return null;
  const b = raw.browser;
  if (!record(b) || !id(b.tab_id, 8) || !id(b.action_id, 32) || !integer(b.pane) || b.pane < 1 || b.pane > 8
    || !integer(b.page_identity) || !integer(b.document_generation)
    || !(BROWSER_ACTIONS as readonly unknown[]).includes(b.action) || !(BROWSER_CATEGORIES as readonly unknown[]).includes(b.category)
    || !text(b.host, 200) || !text(b.path, 400) || !text(b.target, 120) || !text(b.text, b.action === "type" ? 10000 : 8192)) return null;
  const rect = b.rect;
  if (rect !== null && (!record(rect) || !["x", "y", "width", "height"].every(k => typeof rect[k] === "number" && Number.isFinite(rect[k]) && Math.abs(rect[k] as number) <= 100000)
    || (rect.width as number) < 0 || (rect.height as number) < 0)) return null;
  if (b.action !== "type" && b.action !== "select" && b.text !== "") return null;
  if (!record(raw.args) || !text(raw.command, 160000)) return null;
  let command: unknown; try { command = JSON.parse(raw.command); } catch { return null; }
  if (!record(command)) return null;
  const args = raw.args, cmd = command;
  const same = (keys: string[]) => Object.keys(args).length === keys.length && Object.keys(cmd).length === keys.length
    && keys.every(k => Object.prototype.hasOwnProperty.call(args, k) && Object.prototype.hasOwnProperty.call(cmd, k) && cmd[k] === args[k]);
  let download: BrowserApprovalDetail["browser"]["download"];
  if (b.action === "release_download") {
    const d = b.download;
    if (b.category !== "executable_download" || b.rect !== null || !record(d) || !id(d.id, 8) || !text(d.name, 200)
      || new TextEncoder().encode(d.name).length > 200 || !integer(d.size)
      || !same(["action", "download_id", "name", "size"]) || args.action !== b.action || args.download_id !== d.id || args.name !== d.name || args.size !== d.size) return null;
    download = { id: d.id, name: d.name, size: d.size };
  } else {
    if (b.download !== undefined || !same(["action", "pane", "target", "text", "file"]) || args.action !== b.action || args.pane !== String(b.pane)
      || !text(args.target, 8192) || !text(args.text, b.action === "type" ? 10000 : 8192) || !text(args.file, 4096)
      || ((b.action === "type" || b.action === "select") && b.text !== args.text)) return null;
  }
  // Deliberately omit raw args/reason/headline/rect. Consent shows the exact full command;
  // fixed application wording describes it, and this browser has no proven preview pixels.
  return { kind: "action", reqId: expected.req_id, command: raw.command, browser: {
    tab_id: b.tab_id, action_id: b.action_id, pane: b.pane, page_identity: b.page_identity, document_generation: b.document_generation,
    action: b.action as BrowserApprovalDetail["browser"]["action"], category: b.category as BrowserApprovalDetail["browser"]["category"],
    host: b.host, path: b.path, target: b.target, text: b.text, ...(download ? { download } : {}),
  } };
}
