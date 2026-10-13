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
  /** B1, display only: who made the link. Never sent back on a PATCH. */
  discord_channel_origin?: "created" | "linked" | null;
  extra_dirs: string[];
  always_ask: string[];
  inbox: boolean;
  /** When the owner archived it; such a project is only in `/archive`. */
  archived?: string | null;
  /**
   * Where `root` resolves (the daemon's `projects.resolved_root`): the folder
   * a file's `abs` sits under. Display data only — never sent back on a PATCH.
   */
  root_real?: string | null;
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
  /** Chat titles (2026-10-12): who named it — "" nobody yet, "auto" the
   * daemon's titler, "owner" a rename. Absent from an older daemon. */
  title_source?: "" | "auto" | "owner";
  created: string;
  updated: string;
  turns: number;
  cost_usd: number;
  tokens: number;
  model: string | null;
  effort: string | null;
  /** The folder the thread was opened in. A move never changes it. */
  cwd?: string | null;
  /** When the owner archived this thread; such a thread is only in `/archive`. */
  archived?: string | null;
  /** PR C: where this chat lives on Discord — `"dm"`, `"discord:<thread id>"`,
   * `"dm:retired"`, or null before its first message is mirrored. */
  surface?: string | null;
  /** PR C: the same place, named for the chat header (present with a surface). */
  discord?: ThreadDiscord | null;
  /** Live, never stored (2026-10-08): a turn is running on this thread right
   * now, and how many owner messages wait behind it. The window reconciles
   * its busy state against `running`, so a missed `turn_finished` cannot
   * wedge it. Absent from an older daemon. */
  running?: boolean;
  queued?: number;
  /** Live, never stored (WP-H): when the running turn began (ISO), present
   * only while `running`. The thinking line counts from it after a reload. */
  running_since?: string | null;
}

export interface ThreadDiscord {
  kind: "thread" | "dm";
  /** The channel's name without `#` (or "DM"), for "On Discord: #school › name". */
  channel: string | null;
  /** The Discord thread's name. */
  name: string;
  /** `https://discord.com/channels/<guild>/<thread>`, or null (DM, no guild file). */
  url: string | null;
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
  /** The project root this task was started under (decisions B5). */
  root?: string | null;
}

export interface TreeEntry {
  name: string;
  kind: "file" | "dir";
  size: number;
  mtime: number;
  /** ED-2: a withheld credential (never opened; a folder never walked) or an env file. */
  policy?: "withheld" | "env";
}

export interface Tree {
  path: string;
  entries: TreeEntry[];
}

/** `GET /projects/{id}/files` (ED-7): quick open's list. */
export interface FileList {
  files: string[];
  /** The credential env files among them (a lock; Jarvis never reads them). */
  env: string[];
  truncated: boolean;
  source: "git" | "walk";
}

/** `GET /projects/{id}/git/status` (ED-9): the explorer's colours, project-relative. */
export interface GitStatus {
  repo: boolean;
  /** HEAD's commit id; null before the first commit or outside a repository. */
  head: string | null;
  branch: string | null;
  /** File path → modified, added, untracked, ignored, renamed, deleted or conflict. */
  files: Record<string, string>;
  /** A folder git reported whole (untracked or ignored); "" is the project's own folder. */
  dirs: Record<string, string>;
  truncated: boolean;
  note?: string;
}

/** `GET /projects/{id}/git/base?path=` (ED-9): what HEAD holds for a file, for the gutter. */
export interface GitBase {
  path: string;
  /** ok, new (not in HEAD), no_repo, no_head, binary, too_large, not_a_file or unavailable. */
  state: string;
  head: string | null;
  /** HEAD's text, only when `state` is ok. */
  text: string | null;
  size?: number;
  renamed?: boolean;
  /** Not on disk (deleted from the working tree): `text` is what HEAD holds. */
  deleted?: boolean;
}

export interface SearchQuery {
  query: string;
  /** The searching view: its newer search cancels its older one on the daemon. */
  key?: string;
  regex?: boolean;
  /** Case-sensitive. */
  case?: boolean;
  word?: boolean;
  glob?: string;
  /** Respect `.gitignore` (the default). */
  ignore?: boolean;
}

export interface SearchMatch {
  path: string;
  line: number;
  text: string;
  /** Match spans in `text`, UTF-16 offsets. */
  ranges: [number, number][];
}

export interface SearchResult {
  matches: SearchMatch[];
  files: number;
  truncated: boolean;
  timed_out: boolean;
  /** A newer search from the same view took over: drop this one. */
  cancelled?: boolean;
  /** ripgrep could not read some files: what came back is a partial answer. */
  partial?: boolean;
  limit: number;
}

export interface FsResult {
  ok: true;
  op: string;
  path: string;
  abs: string;
  from?: string;
  from_abs?: string;
  dir?: boolean;
  kind?: string;
  trash?: { where: string; location: string };
}

export interface FileRead {
  path: string;
  content?: string;
  mtime?: number;
  size?: number;
  protected: boolean;
  /** ED-2: where the file resolves — one buffer per file on disk. */
  abs?: string;
  /** ED-2: the first 128 bits of the bytes' SHA-256: what a save is made against. */
  etag?: string;
  encoding?: string | null;
  eol?: string | null;
  /** ED-2 (`jarvis/v2/editor_policy.py`): text, env, gate, safety, binary or withheld. */
  kind?: string;
  /**
   * Whether the editor may save it: the HUD follows this, never the kind
   * alone (an env file that is not UTF-8 is `env` and not editable). Absent
   * reads as no.
   */
  editable?: boolean;
  /** The sentence beside the editor's lock (null: editable, and Jarvis may read it). */
  readonly?: string | null;
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

/** `GET /discord` and the `discord_status` SSE event (S1 + PR A). States,
 * counts and times only — never a token, a URL or a message body. */
export interface DiscordReporter {
  state: "ok" | "degraded" | "down";
  reason: string;
  counters: Record<string, number>;
  dropped: number;
  last_error: { op: string; status: number | null; code: number | null; at: number | null } | null;
}

export interface DiscordStatus {
  connected: boolean;
  commands: { state: "ok" | "failed" | "pending" | "off"; count: number; synced_at: number | null; error: string | null };
  reporter: DiscordReporter | null;
  /** B1: is `jarvis auth discord-guild` done, and the server's id. */
  guild?: { configured: boolean; id: string | null };
  /** B1: the channel linker. `null` when no Discord surface is running. */
  linker?: {
    state: "ok" | "degraded" | "unconfigured";
    reason: string;
    pending_renames: number;
    /** Review fix 2: archive/restore moves waiting out a 429 or an outage. */
    pending_moves?: number;
    awaiting_approval: number;
  } | null;
  /** PR C: the chat mirror — posts still to send, and the last failure. */
  mirror?: {
    state: "ok" | "degraded" | "down";
    reason: string;
    queued: number;
    last_error: { op: string; status: number | null; code: number | null; at: number | null } | null;
  } | null;
  /** B1: the bot's server-wide permissions, by name. */
  permissions?: {
    missing: string[];
    excess: string[];
    administrator: boolean;
    checked_at: number | null;
  } | null;
}

/** `GET /projects/{id}/discord` (B1): one project's channel light. */
export type ChannelState =
  | "linked_ok" | "unlinked" | "not_found" | "no_access" | "wrong_guild"
  | "missing_permissions" | "folder_missing" | "unconfigured" | "unreachable";

export interface ProjectChannel {
  channel_id: string | null;
  origin: "created" | "linked" | null;
  name: string | null;
  category: string | null;
  state: ChannelState;
  missing: string[];
  checked_at: number | null;
  rename_pending: boolean;
}

/** `POST /discord/backfill` (B1). */
export interface BackfillResult {
  created: number;
  results: { project_id: string; name: string; channel_id: string | null; status: "created" | "failed"; error?: string }[];
  skipped: { project_id: string; name: string; reason: string }[];
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
  /** Saved routing that does not run as written right now — a model the
   * Codex catalog lacks, a clamped effort, a malformed part — each in words
   * (PR #20 review). routing.json itself is never rewritten. */
  notes?: string[];
}

export interface ApprovalRequest {
  /** Public marker only; private details stay in the active card component. */
  hud_only?: boolean;
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
  /** Shown first and loud: a Codex approval that widens its sandbox. */
  headline?: string;
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
  /** Whether the model accepts images (catalogue and roster rows). */
  vision?: boolean;
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

export type ToolOutcome = "running" | "ok" | "failed" | "denied" | "stopped";
export interface ToolCall {
  call_id: string;
  turn_id: string;
  name: string;
  label: string;
  outcome: ToolOutcome;
  at: string;
  finished_at?: string;
}
export interface ToolEntry extends ToolCall { role: "tool" }

export interface ChatMessage {
  role: "user" | "assistant" | "system" | "tool";
  text: string;
  at?: string;
  /** A grouped transcript row; never contains raw args or result summaries. */
  calls?: ToolCall[];
  turn_id?: string;
  /** A system line that carries a cancel affordance (a proposal_reply). */
  proposal?: { task_id: string; until: number };
  /** PR C: the owner's message came from Discord (a thread or the DM). */
  via?: "discord" | "dm";
  /** The daemon's id for an owner message that reached a running turn. */
  message_id?: string;
  /** This window's id for its own message, until the daemon names it. */
  local?: string;
  /** What became of a message sent while a turn ran (2026-10-08): steered
   * into it, waiting to run after it, or dropped when the owner stopped. */
  mark?: MessageMark;
}

export type MessageMark = "steering" | "queued" | "not sent";

/** `POST /threads/{id}/send` (202). `status` is absent from an older daemon. */
export interface SendResult {
  /** `dropped`: the owner stopped the turn while this steer was on its way
   * and the turn would not take it — not sent, its words handed back. */
  status?: "started" | "steered" | "queued" | "dropped";
  reason?: string;
  turn_id: string;
  message_id?: string;
  /** `interrupt` when the provider could not steer and its turn was stopped
   * for this message instead. */
  mode?: "native" | "interrupt" | null;
  position?: number;
}

export interface ToolOp {
  turn_id?: string;
  label?: string;
  at?: string;
  call_id: string;
  name: string;
  started: number;
  finished?: number;
  ok?: boolean;
  summary?: string;
}

// --- projects: impact, archive, trash (decisions part B) --------------------

export interface ProjectImpact {
  project_id: string;
  name: string;
  root: string;
  inbox: boolean;
  archived: string | null;
  /** What the confirmation was read from; archive and delete send it back. */
  token: string;
  /** Unfinished tasks and running turns: archiving waits for them. */
  blockers: string[];
  chat_threads: { count: number; archived: number };
  tasks: { total: number; finished: number; active: { id: string; brief: string; state: TaskState }[] };
  task_threads: number;
  running_turns: { thread_id: string; title: string }[];
  schedules: { id: string; brief: string; describe: string; enabled: boolean }[];
  worktrees: { task_id: string; path: string; branch: string | null; exists: boolean }[];
  /** What stays on the current folder if the root changes (B5). */
  on_root: { threads: number; tasks: { id: string; brief: string; state: TaskState }[] };
  discord_channel_id: string | null;
}

export interface TrashInfo {
  location: string;
  retention_days: number;
  entries: number;
}

export interface ArchivedProject extends Project {
  threads: Thread[];
  task_threads: number;
  tasks: { id: string; brief: string; state: TaskState; worktree: string | null; branch: string | null }[];
  schedules: number;
}

export interface ArchiveView {
  projects: ArchivedProject[];
  threads: (Thread & { project_name: string })[];
  trash: TrashInfo;
}

export interface DeleteResult {
  deleted: string;
  trash: { where: "linux" | "windows" | "staged"; location?: string; path?: string; error?: string };
  removed?: { threads: number; tasks: number; schedules: number };
  left_on_disk?: string[];
}
