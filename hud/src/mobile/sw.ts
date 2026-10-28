// The phone app's service worker (phone design PH-8, §7; P-6), served by the
// remote listener at /m/sw.js (scope /m/) from this build's own file. It does
// two things and nothing else: no fetch handler, no cache, no offline copy
// (the app is never shown as current when it is not, §6).
//
//   push               shows "Jarvis" with "n waiting", or "n waiting - an
//                      approval expires soon" for rank 1 (P-0's spike text).
//                      The push carries {count, rank} and nothing else
//                      (jarvis/v2/push.py), so that is all it can say.
//   notificationclick  focuses the open app and brings it to the Inbox tab,
//                      or opens /m/#inbox.
//
// Self-contained on purpose: no import, so the build emits one plain script
// with nothing to load beside it. The tests import it with a fake `self`.

interface ExtendableEventLike { waitUntil(promise: Promise<unknown>): void }
interface PushEventLike extends ExtendableEventLike { data: { json(): unknown; text(): string } | null }
interface NotificationEventLike extends ExtendableEventLike { notification: { close(): void } }
interface WindowClientLike {
  url: string;
  focus(): Promise<unknown>;
  postMessage(message: unknown): void;
}
interface WorkerScope {
  addEventListener(type: string, listener: (event: never) => void): void;
  skipWaiting(): Promise<void>;
  registration: { showNotification(title: string, options: Record<string, unknown>): Promise<void> };
  clients: {
    claim(): Promise<void>;
    matchAll(options: Record<string, unknown>): Promise<WindowClientLike[]>;
    openWindow(url: string): Promise<unknown>;
  };
  location: { origin: string };
}

const scope = self as unknown as WorkerScope;
const TITLE = "Jarvis";
const INBOX_URL = "/m/#inbox";
const ICON = "/m/icons/orb-192.png";

/** The body for a push's {count, rank}; anything else reads as "something waits". */
function body(data: unknown): string {
  const value = data && typeof data === "object" ? (data as { count?: unknown; rank?: unknown }) : {};
  const count = typeof value.count === "number" && Number.isInteger(value.count) && value.count >= 0 ? value.count : null;
  if (count === null) return "Something is waiting";
  if (count === 0) return "Nothing is waiting now";
  return `${count} waiting` + (value.rank === 1 ? " - an approval expires soon" : "");
}

function parse(event: PushEventLike): unknown {
  try { return event.data ? event.data.json() : null; } catch { return null; }
}

scope.addEventListener("install", (() => { void scope.skipWaiting(); }) as (event: never) => void);
scope.addEventListener("activate", ((event: ExtendableEventLike) => {
  event.waitUntil(scope.clients.claim());
}) as (event: never) => void);

scope.addEventListener("push", ((event: PushEventLike) => {
  event.waitUntil(scope.registration.showNotification(TITLE, {
    body: body(parse(event)), icon: ICON, tag: "jarvis-inbox", renotify: true,
  }));
}) as (event: never) => void);

scope.addEventListener("notificationclick", ((event: NotificationEventLike) => {
  event.notification.close();
  event.waitUntil((async () => {
    const open = await scope.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of open) {
      let path = "";
      try { path = new URL(client.url, scope.location.origin).pathname; } catch { path = ""; }
      if (path.startsWith("/m/")) {
        client.postMessage({ kind: "open-inbox" });
        await client.focus();
        return;
      }
    }
    await scope.clients.openWindow(INBOX_URL);
  })());
}) as (event: never) => void);

// A module for TypeScript (the tests import it); the build emits no export.
export {};
