// A new VEX project from the VS Code extension's templates (VX-3; editor
// plan §2.9 "New VEX project", decisions V-4): the dialog's rules, pure, so
// vitest holds them.
//
// The daemon decides everything (`jarvis/v2/vex.py`: `check_new_project`,
// `new_project`). The window never makes up a path: the template list is
// the daemon's (`GET /vex/templates`), and **the exact folder the dialog
// shows is the daemon's own answer** to the very body Create would send
// (`POST /vex/projects/check`) — the path the `project_folder` card will
// name. The name rule here only keeps the window from asking for what the
// daemon would refuse; a refusal it sends anyway is shown in its own words.

import type { Language } from "./vex";

/** The daemon's `PROJECT_NAME`: it reaches `make` as `P=` and cmd.exe recipes, so no space and no `-` first. */
export const PROJECT_NAME = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/;
export const PROJECT_NAME_MAX = 64;
/** The daemon's `TEMPLATE_FILE`, without `.zip`: `cpp_v5_vsc_*` or `py_v5_vsc_*`. */
const TEMPLATE_ID = /^(cpp|py)_v5_vsc_[a-z0-9_]+$/;
const REQUEST_ID = /^[0-9a-f]{8}$/;

export interface Template {
  id: string;
  language: Language;
  label: string;
}

export interface NewForm {
  name: string;
  language: Language;
  template: string;
  /** The parent folder as typed; empty, or the default, means `vex.json`'s project folder. */
  location: string;
}

/** Exactly the keys `POST /vex/projects` and its check take — never a path to run or an argument. */
export interface NewBody {
  name: string;
  language: Language;
  template: string;
  location?: string;
}

/** The daemon's answer to the check: what Create would make, nothing asked or made yet. */
export interface NewCheck {
  /** The exact folder: the path the card will name. */
  path: string;
  windows_path: string | null;
  state: "missing" | "empty";
  /** A card comes up (a new folder); an existing empty folder is used without asking (O5). */
  asks: boolean;
  describe: string;
}

export type NewState = "asking" | "created" | "declined" | "failed";

export interface NewRequest {
  id: string;
  name: string;
  path: string;
  state: NewState;
  project_id: string | null;
  message: string;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown, max = 400): string => (typeof v === "string" ? v.slice(0, max) : "");

/** The daemon's list, kept only where each row is a template of the language its id says. */
export function parseTemplates(raw: unknown): Template[] {
  if (!Array.isArray(raw)) return [];
  const out: Template[] = [];
  const seen = new Set<string>();
  for (const row of raw.slice(0, 200)) {
    if (!isObj(row) || typeof row.id !== "string" || !TEMPLATE_ID.test(row.id) || seen.has(row.id)) continue;
    const language: Language = row.id.startsWith("cpp_") ? "cpp" : "python";
    if (row.language !== language) continue;
    seen.add(row.id);
    out.push({ id: row.id, language, label: str(row.label, 80) || row.id });
  }
  return out;
}

export function templatesOf(list: readonly Template[], language: Language): Template[] {
  return list.filter((t) => t.language === language);
}

/** The template a language starts on: the one chosen if it is still offered, else the first. */
export function pickTemplate(list: readonly Template[], language: Language, chosen: string): string {
  const offered = templatesOf(list, language);
  return offered.some((t) => t.id === chosen) ? chosen : offered[0]?.id ?? "";
}

/** The name's refusal, in the daemon's terms, or null when it would pass. */
export function projectNameRefusal(name: string): string | null {
  if (!name) return "Give the project a name.";
  if (name.length > PROJECT_NAME_MAX) return `The name can be at most ${PROJECT_NAME_MAX} characters.`;
  if (name.startsWith("-")) return "The name can't start with -.";
  if (!PROJECT_NAME.test(name)) return "Use only letters, digits, _ and - (VEX's make refuses spaces).";
  return null;
}

/**
 * The body Create sends — and the check, so the path shown is the one asked
 * about. `location` is left out when it is empty or the default, so the
 * daemon uses `vex.json`'s project folder itself.
 */
export function newBody(form: NewForm, defaultFolder: string | null): NewBody {
  const body: NewBody = { name: form.name, language: form.language, template: form.template };
  const where = form.location.trim();
  if (where && where !== (defaultFolder || "").trim()) body.location = where;
  return body;
}

/** One body as a key: a check's answer counts only for the inputs it was asked about. */
export function bodyKey(body: NewBody): string {
  return JSON.stringify([body.name, body.language, body.template, body.location ?? null]);
}

export function parseNewCheck(raw: unknown): NewCheck | null {
  if (!isObj(raw) || typeof raw.path !== "string" || !raw.path.startsWith("/")) return null;
  const state = raw.state === "missing" || raw.state === "empty" ? raw.state : null;
  if (!state) return null;
  return {
    path: raw.path.slice(0, 1000),
    windows_path: typeof raw.windows_path === "string" ? raw.windows_path.slice(0, 1000) : null,
    state,
    asks: raw.asks !== false,
    describe: str(raw.describe, 200),
  };
}

const STATES: readonly NewState[] = ["asking", "created", "declined", "failed"];

export function parseNewRequest(raw: unknown): NewRequest | null {
  if (!isObj(raw) || typeof raw.id !== "string" || !REQUEST_ID.test(raw.id)) return null;
  const state = (STATES as readonly unknown[]).includes(raw.state) ? (raw.state as NewState) : null;
  if (!state) return null;
  return {
    id: raw.id,
    name: str(raw.name, 100),
    path: str(raw.path, 1000),
    state,
    project_id: typeof raw.project_id === "string" && raw.project_id ? raw.project_id.slice(0, 100) : null,
    message: str(raw.message, 600),
  };
}

/**
 * Whether Create may be pressed: VEX is set up, the name passes, a template
 * is chosen, the daemon's check answered **for exactly these inputs**, no
 * request is on its way, and no approval card is up.
 */
export function createReady(o: {
  configured: boolean;
  form: NewForm;
  defaultFolder: string | null;
  check: { key: string; value: NewCheck } | null;
  pending: boolean;
  blocked: boolean;
}): boolean {
  if (!o.configured || o.pending || o.blocked) return false;
  if (projectNameRefusal(o.form.name) || !o.form.template) return false;
  return !!o.check && o.check.key === bodyKey(newBody(o.form, o.defaultFolder));
}

/** The line under the path while a request is out, and after it. */
export function requestLine(r: NewRequest): string {
  switch (r.state) {
    case "asking":
      return `Approve or Deny on the card to create ${r.path}.`;
    case "created":
      return r.message || `Made project ${r.name}.`;
    case "declined":
      return r.message || `Not approved, so nothing was made (${r.path}).`;
    default:
      return r.message || "The project was not made.";
  }
}
