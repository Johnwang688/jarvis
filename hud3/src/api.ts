const paths = {
  status: ["/v3/status", "/m/api/status"],
  feed: ["/v3/owner/feed", "/m/api/feed"],
  feedAnswer: ["/v3/owner/feed/answer", "/m/api/feed/answer"],
  feedFeedback: ["/v3/owner/feed/feedback", "/m/api/feed/feedback"],
  proposal: ["/v3/owner/proposal", "/m/api/proposal"],
  chat: ["/v3/owner/chat", "/m/api/chat"],
  chatEvents: ["/v3/owner/chat/events", "/m/api/chat/events"],
  chatSend: ["/v3/owner/chat/send", "/m/api/chat/send"],
  chatStop: ["/v3/owner/chat/stop", "/m/api/chat/stop"],
  chatNew: ["/v3/owner/chat/new", "/m/api/chat/new"],
  approvals: ["/v3/owner/approvals", "/m/api/approvals"],
  approvalsAnswer: ["/v3/owner/approvals/answer", "/m/api/approvals/answer"],
  work: ["/v3/owner/work", "/m/api/work"],
} as const;

const phonePaths = {
  me: "/m/api/me",
  "login/options": "/m/api/login/options",
  "login/verify": "/m/api/login/verify",
  "pair/options": "/m/api/pair/options",
  "pair/verify": "/m/api/pair/verify",
  logout: "/m/api/logout",
  lock: "/m/api/lock",
  "unlock/options": "/m/api/unlock/options",
  unlock: "/m/api/unlock",
} as const;

export type PhoneApiName = keyof typeof phonePaths;
export type ApiName = keyof typeof paths | PhoneApiName;
type Surface = "window" | "phone";

let current: Surface = "window";
let read = false;

export function surface(): Surface {
  return current;
}

export function setSurface(next: Surface) {
  if (read) throw new Error("The API surface has already been used");
  current = next;
}

export function apiPath(name: ApiName): string {
  read = true;
  if (Object.hasOwn(paths, name)) return paths[name as keyof typeof paths][current === "phone" ? 1 : 0];
  if (current === "phone" && Object.hasOwn(phonePaths, name)) return phonePaths[name as PhoneApiName];
  throw new Error("API route unavailable on this surface");
}

type PhoneNotice = "locked" | "session-expired" | "write";
const listeners = new Set<(notice: PhoneNotice) => void>();

export function onPhoneNotice(listener: (notice: PhoneNotice) => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function apiFetch(name: ApiName, options: RequestInit): Promise<Response> {
  const observers = current === "phone" ? [...listeners] : [];
  const response = await fetch(apiPath(name), options);
  if (observers.length && !options.signal?.aborted) {
    let notice: PhoneNotice | undefined;
    const write = options.method === "POST" && name !== "chatEvents" && name !== "proposal";
    if (write && response.status === 423) {
      const body = await response.clone().json().catch(() => null);
      if (body?.error === "locked" && body.unlock === true) notice = "locked";
    } else if (response.status === 401 && (Object.hasOwn(paths, name) || name === "me" || name === "logout")) {
      notice = "session-expired";
    } else if (write && response.ok && Object.hasOwn(paths, name)) {
      notice = "write";
    }
    if (notice && !options.signal?.aborted) {
      for (const listener of observers) {
        if (listeners.has(listener)) listener(notice);
      }
    }
  }
  return response;
}
