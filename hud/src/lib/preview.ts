// What the Preview tab is allowed to iframe.
//
// The HUD is the approval surface. An iframe that could reach `/approvals`
// would let an agent approve itself, which is the frontend half of v1's
// `is_face_origin()` rule — so the HUD's own origin is refused here whatever
// spelling it arrives in, and the backend refuses to proxy it as well.
//
// Chrome resolves `foo.localhost` to 127.0.0.1, so the loopback test is by
// hostname family rather than by string equality, exactly as v1's
// `is_face_origin` matches all of loopback including `*.localhost`.

export const FACE_PORT = 8402;
export const WORKSHOP_PORT = 8403;

export interface PreviewVerdict {
  ok: boolean;
  url: string;
  reason: string;
}

function isLoopbackHost(host: string): boolean {
  const h = host.toLowerCase();
  if (h === "localhost" || h.endsWith(".localhost")) return true;
  if (h === "::1" || h === "[::1]") return true;
  if (h === "0.0.0.0") return true;
  return /^127\.\d+\.\d+\.\d+$/.test(h);
}

/**
 * Judge a URL the owner typed into the preview box.
 *
 * Allowed: the workshop origin (`http://127.0.0.1:8403/p/<project>/…`) and any
 * `http://localhost:<port>/…` dev server. Refused: the HUD's own origin, and
 * anything that is not loopback http — a preview pane is for looking at what
 * was just built on this machine, not a browser.
 */
export function judgePreviewUrl(raw: string, facePort = FACE_PORT): PreviewVerdict {
  const text = (raw || "").trim();
  if (!text) return { ok: false, url: "", reason: "Enter a preview URL." };
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return { ok: false, url: "", reason: "Not a URL." };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, url: "", reason: `Refused: ${url.protocol} is not a page.` };
  }
  if (!isLoopbackHost(url.hostname)) {
    return { ok: false, url: "", reason: "Refused: the preview pane only loads local servers." };
  }
  const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
  if (port === facePort) {
    return {
      ok: false,
      url: "",
      reason:
        "Refused: that is the HUD's own origin. The window that gates approvals " +
        "is never loaded in a frame inside itself.",
    };
  }
  return { ok: true, url: url.href, reason: "" };
}

/**
 * The workshop port the serving daemon reported on `/status`.
 *
 * Only a daemon too old to report one falls back to the default: the HUD and
 * its daemon ship together, so that is a stale daemon, not a test. A test's
 * mock reports its own port, which is the point — a hard-coded 8403 sent the
 * HUD suite's fixture project to the owner's live daemon.
 */
export function workshopPortFrom(status: unknown): number {
  const port = (status as { workshop_port?: unknown } | null)?.workshop_port;
  return typeof port === "number" && Number.isInteger(port) && port > 0 && port < 65536
    ? port
    : WORKSHOP_PORT;
}

/** The workshop URL for a file inside a project (docs/hud-api.md, Preview origin). */
export function workshopUrl(projectId: string, rel: string, port = WORKSHOP_PORT): string {
  const clean = rel.replace(/^\/+/, "");
  return `http://127.0.0.1:${port}/p/${encodeURIComponent(projectId)}/${clean}`;
}
