// Writing from the phone (phone design §5, §6, §11 questions 1 and 2; P-5;
// jarvis/v2/remote_write.py): a chat's message box, the "New chat" and
// "New task" sheets, and a task's Start, Resume and Cancel.
//
//   - **Nothing is sent without a tap.** The message box is a textarea:
//     Enter is a new line on the phone, never Send. Send is a button, at
//     least 44 px tall.
//   - **Nothing is shown as sent before Jarvis said so.** A message appears
//     in the transcript only when its 2xx came back and the log was read
//     again; until then the box says "Sending…". Offline, nothing is sent and
//     the box says so: there is no write queue.
//   - **Every task verb asks first.** Start and Resume take a confirmation
//     tap; Cancel takes a confirmation and then the passkey (Face ID or the
//     passcode), because a cancelled task can never resume.
//   - Every string from the daemon is set with textContent.
//
// Pure DOM building from data, so the tests build it in jsdom.

import "./write.css";
import { oneLine } from "../lib/inbox";
import type { Project, TaskView } from "./model";

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => !!v && typeof v === "object" && !Array.isArray(v);

/** remote_write.MESSAGE_CHARS / BRIEF_CHARS: the box stops there too. */
export const MESSAGE_CHARS = 20000;
export const BRIEF_CHARS = 20000;
export const PROVIDERS = ["fast", "claude", "codex"] as const;
export type Provider = (typeof PROVIDERS)[number];

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

function button(label: string, onClick: () => void, cls: string, id: string, disabled = false): HTMLButtonElement {
  const b = testid(el("button", "tap " + cls, label), id);
  b.type = "button";
  b.disabled = disabled;
  b.addEventListener("click", () => { if (!b.disabled) onClick(); });
  return b;
}

function note(text: string, id: string, cls = "m-notice"): HTMLElement {
  const line = testid(el("p", cls, text), id);
  line.setAttribute("role", "status");
  return line;
}

/** A refusal's words (`{"error": ...}`), as a sentence. */
export function refusal(status: number, body: Json, what = "Not sent"): string {
  const word = typeof body.error === "string" ? oneLine(body.error, 300) : "";
  if (status === 429) return "Too many from this phone in the last minute. Wait a moment.";
  return word ? word[0].toUpperCase() + word.slice(1) + "." : `${what} (${status}).`;
}

/** A write whose answer never came back (the connection dropped after it
 * left, or the proxy answered 502/504): it may have landed. */
export const MAYBE_SENT = "May not have been sent. Check the chat before trying again.";
export const MAYBE_DONE = "May not have gone through. Check the task before trying again.";

/** The phone's id for one draft (`client_id`): the server answers a repeat
 * of the same id with the first answer and does nothing twice. Bound to the
 * draft's exact words: edited words are a new message and get a new id. */
export function clientIdFor(ids: Map<string, { id: string; text: string }>, key: string, text: string): string {
  const found = ids.get(key);
  if (found && found.text === text) return found.id;
  const c = (globalThis as { crypto?: Crypto }).crypto;
  const id = c && typeof c.randomUUID === "function" ? c.randomUUID()
    : Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  ids.set(key, { id, text });
  return id;
}

/** What `POST threads/<id>/messages` answered, in words. */
export function sentNote(body: Json): string {
  if (body.status === "queued") {
    const n = typeof body.position === "number" ? body.position : 0;
    return n > 1 ? `Queued: it runs after the current turn (${n} waiting).` : "Queued: it runs after the current turn.";
  }
  return "Sent.";
}

// -- the message box ----------------------------------------------------------------

export interface Composer {
  draft: string;
  busy: boolean;
  note: string;
  online: boolean;
  send: (text: string) => void;
  edit: (text: string) => void;
  /** P-7: the mic button (./talk.ts), whose words land in this box unsent. */
  mic?: HTMLElement | null;
  /** The box grew or shrank: the app keeps the newest message in view if it was. */
  around?: (change: () => void) => void;
}

/** The box's most lines before it scrolls inside itself. */
export const COMPOSE_LINES = 5;

/** One line tall, growing with the words to `COMPOSE_LINES`, then scrolling.
 * Set through the CSSOM (C1's CSP refuses style attributes, not this). */
export function fitLines(text: HTMLTextAreaElement): void {
  const css = typeof getComputedStyle === "function" ? getComputedStyle(text) : null;
  const px = (v: string | undefined) => parseFloat(v ?? "") || 0;
  const line = px(css?.lineHeight) || 23;
  // Box-sizing is border-box (mobile.css): the height counts the borders, scrollHeight does not.
  const borders = px(css?.borderTopWidth) + px(css?.borderBottomWidth);
  const most = line * COMPOSE_LINES + px(css?.paddingTop) + px(css?.paddingBottom) + borders;
  text.style.height = "auto";
  const want = Math.min(text.scrollHeight + borders, most);
  if (want > borders) text.style.height = `${Math.ceil(want)}px`;
  else text.style.removeProperty("height");
}

/** A chat's message box, Claude/Codex style: one slim bar (the box, the mic,
 * a round Send) pinned above the tab bar, its notes above it. Send's rules are
 * P-5's as before: a tap only, off while empty, offline or sending. */
/** Each drawn box's own redraw: `updateComposer` draws it again in place. */
const redraws = new WeakMap<HTMLElement, (c: Composer) => void>();

export function composerView(c: Composer): HTMLElement {
  const box = testid(el("div", "m-compose"), "m-compose");
  const text = testid(el("textarea", "m-text m-compose-text"), "m-compose-text");
  text.setAttribute("aria-label", "Message");
  text.placeholder = "Message";
  text.rows = 1;
  text.maxLength = MESSAGE_CHARS;
  // A new line, never a send: the phone's return key says so.
  text.setAttribute("enterkeyhint", "enter");
  // The listeners read the box's current state, so a redraw in place needs no new ones.
  let cur = c;
  const send = button("", () => {
    const value = text.value.trim();
    if (value) cur.send(value);
  }, "m-send-round", "m-compose-send");
  // The words stay the button's own text (screen readers, the checks); the arrow is drawn by CSS.
  const label = el("span", "m-sr");
  send.append(label);
  const around = (change: () => void) => (cur.around ?? ((f: () => void) => f()))(change);
  const sync = () => {
    cur.edit(text.value);
    send.disabled = cur.busy || !cur.online || !text.value.trim();
  };
  text.addEventListener("input", () => { sync(); around(() => fitLines(text)); });
  // While sending, nothing is typed into the box. Not readOnly: toggling that on the
  // focused box can drop the iPhone's keyboard (delta review of #331, LOW c).
  text.addEventListener("beforeinput", (e) => { if (cur.busy) e.preventDefault(); });
  // A tap on Send leaves the focus (and the keyboard) in the box.
  for (const type of ["pointerdown", "mousedown"]) send.addEventListener(type, (e) => e.preventDefault());
  const bar = el("div", "m-bar");
  const draw = (next: Composer) => {
    cur = next;
    if (text.value !== next.draft) text.value = next.draft;
    if (next.busy) text.setAttribute("aria-readonly", "true");
    else text.removeAttribute("aria-readonly");
    label.textContent = next.busy ? "Sending…" : "Send";
    send.dataset.busy = String(next.busy);
    sync();
    // The notes above the bar, drawn again; the box, the mic and Send stay the same nodes.
    while (box.firstChild && box.firstChild !== bar) box.firstChild.remove();
    const notes: HTMLElement[] = [];
    if (next.note) notes.push(note(next.note, "m-compose-note"));
    if (next.busy) notes.push(note("Sending…", "m-compose-sending", "m-notice m-sending"));
    if (!next.online) notes.push(note("Offline: nothing can be sent until Jarvis is reachable.", "m-compose-offline", "m-notice m-off"));
    box.prepend(...notes);
    if (next.mic && next.mic.parentNode !== bar) bar.insertBefore(next.mic, send);
    if (text.isConnected) around(() => fitLines(text));
  };
  bar.append(text, send);
  box.append(bar);
  draw(c);
  redraws.set(box, draw);
  // Sized once it is on the page (a draft kept across a redraw keeps its lines).
  const fit = () => { if (text.isConnected) around(() => fitLines(text)); };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(fit);
  else queueMicrotask(fit);
  return box;
}

/** Draw a box from `composerView` again in place: the same textarea, so its
 * focus and the phone's keyboard stay up across a send (review of #331,
 * LOW 6). False for anything else (the caller then draws a new one). */
export function updateComposer(box: Element, c: Composer): boolean {
  const draw = box instanceof HTMLElement ? redraws.get(box) : undefined;
  if (!draw) return false;
  draw(c);
  return true;
}

// -- what the new-chat sheet offers (`GET /m/api/thread-models`) ----------------------

export interface ProviderChoice { name: Provider; label: string; available: boolean; models: { id: string; name: string }[] }

export function parseModels(raw: unknown): ProviderChoice[] {
  const providers = isObj(raw) && isObj(raw.providers) ? raw.providers : {};
  const out: ProviderChoice[] = [];
  for (const name of PROVIDERS) {
    const p = providers[name];
    if (!isObj(p)) continue;
    const models = Array.isArray(p.models) ? p.models.filter(isObj)
      .filter((m) => typeof m.id === "string" && m.id)
      .map((m) => ({ id: oneLine(m.id, 200), name: oneLine(m.name, 120) || oneLine(m.id, 120) })) : [];
    out.push({ name, label: oneLine(p.label, 40) || name, available: p.available === true, models });
  }
  return out;
}

// -- the sheets ----------------------------------------------------------------------

export interface ChatSheet {
  kind: "chat";
  projectId: string;
  provider: Provider;
  model: string;
  text: string;
  busy: boolean;
  note: string;
  /** A chat that was opened although its first message was refused. */
  openedId: string | null;
}

export interface TaskSheet {
  kind: "task";
  projectId: string;
  brief: string;
  busy: boolean;
  note: string;
}

export type Sheet = ChatSheet | TaskSheet;

function select(id: string, label: string, options: { value: string; text: string; disabled?: boolean }[],
                value: string, onChange: (v: string) => void, disabled: boolean): HTMLElement {
  const wrap = el("label", "m-field");
  wrap.append(el("span", "label", label));
  const s = testid(el("select", "m-select"), id);
  for (const o of options) {
    const opt = el("option", "", o.text);
    opt.value = o.value;
    opt.disabled = !!o.disabled;
    s.append(opt);
  }
  s.value = value;
  s.disabled = disabled;
  s.addEventListener("change", () => onChange(s.value));
  wrap.append(s);
  return wrap;
}

export interface SheetActions {
  close: () => void;
  submit: () => void;
  change: () => void;
  openChat?: (id: string) => void;
}

export function chatSheetView(sheet: ChatSheet, projects: Project[], models: ProviderChoice[] | null,
                              online: boolean, a: SheetActions): HTMLElement {
  const view = testid(el("section", "view m-sheet"), "m-new-chat");
  const bar = el("div", "subbar");
  bar.append(button("‹ Chats", a.close, "back", "m-sheet-close"), el("h2", "subtitle", "New chat"));
  view.append(bar);
  view.append(select("m-new-project", "Project", projects.map((p) => ({ value: p.id, text: p.name })),
    sheet.projectId, (v) => { sheet.projectId = v; a.change(); }, sheet.busy));
  if (models === null) {
    view.append(el("p", "muted", "Loading the providers…"));
  } else {
    view.append(select("m-new-provider", "Provider", models.map((m) => ({
      value: m.name, text: m.available ? m.label : `${m.label} (unavailable)`, disabled: !m.available })),
      sheet.provider, (v) => { sheet.provider = v as Provider; sheet.model = ""; a.change(); }, sheet.busy));
    const chosen = models.find((m) => m.name === sheet.provider);
    view.append(select("m-new-model", "Model", [{ value: "", text: "Default" },
      ...(chosen?.models ?? []).map((m) => ({ value: m.id, text: m.name }))],
      sheet.model, (v) => { sheet.model = v; }, sheet.busy));
  }
  const text = testid(el("textarea", "m-text"), "m-new-text");
  text.setAttribute("aria-label", "First message");
  text.placeholder = "First message";
  text.rows = 4;
  text.maxLength = MESSAGE_CHARS;
  text.setAttribute("enterkeyhint", "enter");
  text.value = sheet.text;
  text.readOnly = sheet.busy;
  const usable = () => !!models?.find((m) => m.name === sheet.provider && m.available);
  const go = button(sheet.busy ? "Starting…" : "Start chat", a.submit, "quiet m-send", "m-new-start");
  const sync = () => {
    sheet.text = text.value;
    go.disabled = sheet.busy || !online || !sheet.projectId || !usable() || !text.value.trim();
  };
  text.addEventListener("input", sync);
  sync();
  view.append(text, go);
  if (sheet.note) view.append(note(sheet.note, "m-sheet-note"));
  if (!online) view.append(note("Offline: nothing can be sent until Jarvis is reachable.", "m-sheet-offline", "m-notice m-off"));
  if (sheet.openedId && a.openChat) {
    const id = sheet.openedId;
    view.append(button("Open the new chat", () => a.openChat!(id), "link", "m-sheet-open"));
  }
  return view;
}

export function taskSheetView(sheet: TaskSheet, projects: Project[], online: boolean, a: SheetActions): HTMLElement {
  const view = testid(el("section", "view m-sheet"), "m-new-task");
  const bar = el("div", "subbar");
  bar.append(button("‹ Tasks", a.close, "back", "m-sheet-close"), el("h2", "subtitle", "New task"));
  view.append(bar);
  view.append(select("m-new-project", "Project", projects.map((p) => ({ value: p.id, text: p.name })),
    sheet.projectId, (v) => { sheet.projectId = v; a.change(); }, sheet.busy));
  const text = testid(el("textarea", "m-text"), "m-new-brief");
  text.setAttribute("aria-label", "What the task should do");
  text.placeholder = "What the task should do";
  text.rows = 5;
  text.maxLength = BRIEF_CHARS;
  text.setAttribute("enterkeyhint", "enter");
  text.value = sheet.brief;
  text.readOnly = sheet.busy;
  const go = button(sheet.busy ? "Creating…" : "Create task", a.submit, "quiet m-send", "m-new-create");
  const sync = () => {
    sheet.brief = text.value;
    go.disabled = sheet.busy || !online || !sheet.projectId || !text.value.trim();
  };
  text.addEventListener("input", sync);
  sync();
  view.append(text, go);
  view.append(el("p", "muted small", "It waits for Start: nothing runs until you press it."));
  if (sheet.note) view.append(note(sheet.note, "m-sheet-note"));
  if (!online) view.append(note("Offline: nothing can be sent until Jarvis is reachable.", "m-sheet-offline", "m-notice m-off"));
  return view;
}

// -- a task's verbs --------------------------------------------------------------------

export type Verb = "start" | "resume" | "cancel";

const TERMINAL: ReadonlySet<string> = new Set(["done", "failed", "cancelled"]);

/** The verbs a task in this state takes from the phone. */
export function verbsFor(state: string): Verb[] {
  const out: Verb[] = [];
  if (state === "intake") out.push("start");
  if (state === "blocked") out.push("resume");
  if (state && !TERMINAL.has(state)) out.push("cancel");
  return out;
}

const LABEL: Record<Verb, string> = { start: "Start", resume: "Resume", cancel: "Cancel task" };
const ASK: Record<Verb, string> = {
  start: "Start this task? It runs with the project's permissions; anything that needs you still asks.",
  resume: "Resume this task where it stopped?",
  cancel: "Cancel this task? A cancelled task cannot be resumed. Your passkey confirms it.",
};
const CONFIRM: Record<Verb, string> = { start: "Start it", resume: "Resume it", cancel: "Cancel with passkey" };

export interface TaskControls {
  confirming: Verb | null;
  busy: boolean;
  note: string;
  online: boolean;
  ask: (verb: Verb) => void;
  confirm: (verb: Verb) => void;
  back: () => void;
}

export function taskControlsView(task: TaskView, c: TaskControls, allowed: readonly Verb[] = verbsFor(task.state)): HTMLElement {
  const box = testid(el("div", "m-task-controls"), "m-task-controls");
  if (c.confirming && allowed.includes(c.confirming)) {
    const verb = c.confirming;
    box.append(testid(el("p", "m-ask", ASK[verb]), "m-task-ask"));
    const row = el("div", "m-confirm-row");
    row.append(button("Not now", c.back, "quiet m-btn", "m-task-back", c.busy),
      button(c.busy ? "Working…" : CONFIRM[verb], () => c.confirm(verb), `quiet m-btn ${verb}`, "m-task-confirm",
        c.busy || !c.online));
    box.append(row);
  } else {
    for (const verb of allowed) {
      box.append(button(LABEL[verb], () => c.ask(verb), `quiet m-btn ${verb}`, "m-task-" + verb, c.busy || !c.online));
    }
  }
  if (c.note) box.append(note(c.note, "m-task-note"));
  if (!c.online && allowed.length) box.append(note("Offline: nothing can be sent until Jarvis is reachable.", "m-task-offline", "m-notice m-off"));
  return box;
}
