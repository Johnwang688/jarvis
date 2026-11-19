import { api } from "../api";
import { WINDOW_ID } from "./windowId";

const REGION_NAME = "Jarvis protected area";
const REGION_SELECTOR = `[role="group"][aria-label="${REGION_NAME}"]`;
const HEARTBEAT_MS = 1500;

export const approvalRegion = { role: "group", "aria-label": REGION_NAME, "data-hud-approval": "" };
export const editorRegion = { role: "group", "aria-label": REGION_NAME };

function displayed(region: Element): boolean {
  if (!region.isConnected) return false;
  for (let node: Element | null = region; node; node = node.parentElement) {
    if (getComputedStyle(node).display === "none") return false;
  }
  return true;
}

export function startHudSensitiveReports(): () => void {
  if (!WINDOW_ID) return () => {};
  let previous = "";
  let sending = false;
  let queued: { client_id: string; cards: number; editors: number } | undefined;

  async function send() {
    if (sending) return;
    sending = true;
    while (queued) {
      const report = queued;
      queued = undefined;
      // Serialize writes so an earlier heartbeat cannot replace a later change.
      await api.reportHudSensitive(report, AbortSignal.timeout(1200)).catch(() => {});
    }
    sending = false;
  }

  function publish(heartbeat = false) {
    let cards = 0;
    let editors = 0;
    for (const region of document.querySelectorAll(REGION_SELECTOR)) {
      if (!displayed(region)) continue;
      if (region.hasAttribute("data-hud-approval")) cards++;
      else editors++;
    }
    const current = `${cards}:${editors}`;
    if (!heartbeat && current === previous) return;
    previous = current;
    queued = { client_id: WINDOW_ID, cards, editors };
    void send();
  }

  const changed = () => publish();
  const observer = new MutationObserver(changed);
  observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true,
    attributeFilter: ["role", "aria-label", "data-hud-approval", "style", "class", "hidden"] });
  const interval = window.setInterval(() => publish(true), HEARTBEAT_MS);
  window.addEventListener("resize", changed);
  document.addEventListener("visibilitychange", changed);
  publish(true);
  return () => {
    observer.disconnect();
    window.clearInterval(interval);
    window.removeEventListener("resize", changed);
    document.removeEventListener("visibilitychange", changed);
    queued = undefined;
  };
}
