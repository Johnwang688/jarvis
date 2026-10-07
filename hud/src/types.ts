// The shapes of docs/hud-api.md and jarvis/v2/model.py, as the HUD reads them.
// Nothing here invents a field: where the contract says a value may be absent
// it is optional, and where it says "never computed" (quota) it is nullable.

export type TaskState =
  | "intake" | "clarifying" | "planned" | "running"
  | "verifying" | "done" | "blocked" | "failed" | "cancelled";

export type ProviderName = "claude" | "codex" | "fast";
export type Role = "orchestrator" | "implementer" | "reviewer" | "researcher" | "chat";
export type Profile = "auto" | "ask" | "strict";

export interface Routing {
  chains: Record<string, string[]>;
  models: Record<string, Record<string, string>>;
  no_new_work: number | null;
}

export interface Project {
  id: string;
  name: string;
  root: string;
  created: string;
  profile: Profile;
  routing: Routing;
  discord_channel_id: string | null;
  extra_dirs: string[];
  always_ask: string[];
  inbox: boolean;
}

export interface Platform {
  platform: "wsl" | "windows";
  note: string | null;
}

export interface Thread {
  id: string;
  project_id: string;
  role: Role;
  provider: ProviderName;
  provider_session_id: string | null;
  task_id: string | null;
  title: string;
  created: string;
  updated: string;
  turns: number;
  cost_usd: number;
  tokens: number;
  model: string | null;
  effort: string | null;
}

export interface TaskThread {
  thread_id: string;
  role: Role;
  provider: ProviderName;
  model: string | null;
  state: "open" | "closed";
  turns: number;
  cost_usd: number;
}

export interface OpenQuestion {
  text: string;
  blocking: boolean;
  options: string[];
  answer: string | null;
  assumed: string | null;
}

export interface Spec {
  goal: string;
  deliverable: string;
  acceptance: string[];
  constraints: string[];
  questions: OpenQuestion[];
}

export interface RoutingDecision {
  role: string;
  provider: string;
  reason: string;
  at: string;
  task_id?: string;
}

export interface Status {
  phase: TaskState;
  step: number;
  steps: number;
  started: string | null;
  elapsed_s: number;
  cost_usd: number;
  tokens: number;
  last_tool: string | null;
  last_file: string | null;
  open_question: string | null;
  routing: RoutingDecision[];
}

export interface Report {
  done: string[];
  changed: string[];
  verified: string;
  open: string[];
  next: string;
  cost: Record<string, number>;
}

export interface Task {
  id: string;
  project_id: string;
  brief: string;
  state: TaskState;
  created: string;
  updated: string;
  spec: Spec;
  plan: string[];
  status: Status;
  report: Report | null;
  thread_ids: string[];
  worktree: string | null;
  branch: string | null;
  profile: Profile | null;
  ceilings: Record<string, number>;
}

export interface TreeEntry {
  name: string;
  kind: "file" | "dir";
  size: number;
  mtime: number;
}

export interface Tree {
  path: string;
  entries: TreeEntry[];
}

export interface FileRead {
  path: string;
  content?: string;
  mtime: number;
  size: number;
  protected: boolean;
}

export interface DiffFile {
  path: string;
  status: "A" | "M" | "D" | "R";
  additions: number;
  deletions: number;
}

export interface Diff {
  base: string;
  head: string;
  files: DiffFile[];
  patch: string;
  truncated?: boolean;
}

export interface ProviderUsage {
  state: "available" | "over_threshold" | "cooling" | "unavailable";
  reason: string;
  today: { work_tokens: number; spend_usd: number; equivalent_usd: number };
  allowance: Record<string, number> | null;
  quota: null | { windows: { name: string; used_percent: number; resets_at: string | number }[] };
}

export interface Usage {
  providers: Record<string, ProviderUsage>;
}

export interface Schedule {
  id: string;
  project_id: string;
  brief: string;
  cron: string | null;
  every_s: number | null;
  enabled: boolean;
  last_run_at: string | null;
  last_task_id: string | null;
  next_run_at: string | null;
  created: string;
  /** The scheduler's own sentence. Absent on a record that predates it. */
  describe?: string;
}

/** `POST /schedules/preview` — the backend owns the timezone and the calendar. */
export interface SchedulePreview {
  next: string[];
  describe: string;
}

/** `GET /fs/dirs` — directories only, under $HOME or /mnt/<drive>/ only. */
export interface DirListing {
  path: string;
  parent: string | null;
  dirs: string[];
}

export interface RouteView {
  table: Record<string, any>;
  states: Record<string, string>;
  decisions: RoutingDecision[];
}

export interface ApprovalRequest {
  req_id: string;
  code: string;
  tool: string;
  args: Record<string, unknown>;
  command: string | null;
  reason: string;
  layer: string;
  thread_id: string | null;
  task_id: string | null;
  provider: string | null;
  origin: string;
  asked_at: string;
  allowlistable?: boolean;
  timeout_s?: number;
  remote?: boolean;
}

export interface AvatarDesc {
  slug: string;
  name: string;
  wake: string[];
  wake_source?: string[];
  rings?: string;
  accent?: string;
  banner?: string;
  label?: string;
  description?: string;
  svg?: boolean;
}

export interface VoiceEntry {
  name: string;
  backend: string;
  kind: string;
}

export interface ModelRow {
  id: string;
  name: string;
  selected?: boolean;
  effort?: string | null;
  efforts?: string[];
  prompt_usd?: number | null;
  completion_usd?: number | null;
  intelligence?: number | null;
}

export interface Attachment {
  name: string;
  mime: string;
  data_b64: string;
}

/** One SSE frame. `kind` spans provider events and lifecycle records alike. */
export interface HudEvent {
  kind: string;
  thread_id?: string;
  project_id?: string;
  task_id?: string;
  turn_id?: string;
  data?: Record<string, any>;
  [k: string]: any;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  text: string;
  at?: string;
  /** A system line that carries a cancel affordance (a proposal_reply). */
  proposal?: { task_id: string; until: number };
}

export interface ToolOp {
  call_id: string;
  name: string;
  started: number;
  finished?: number;
  ok?: boolean;
  summary?: string;
}
