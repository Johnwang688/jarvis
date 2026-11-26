import { apiFetch } from "../../api";

interface WorkProject {
  name: string;
}

export interface WorkThread {
  thread_id: string;
  project: string;
  title: string;
  message?: boolean;
  status: string;
  waiting_on_owner: boolean;
  started_at: number;
  updated_at: number;
}

export interface WorkSnapshot {
  projects: WorkProject[];
  threads: WorkThread[];
}

export type WorkResult = WorkSnapshot | "unreachable" | "unreadable";

const projectName = /^[a-z0-9][a-z0-9-]{0,31}$/;
const threadStatus = /^[a-z_]{1,32}$/;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isProject(value: unknown): value is WorkProject {
  return record(value) && typeof value.name === "string" && projectName.test(value.name);
}

function isThread(value: unknown): value is WorkThread {
  if (!record(value)) return false;
  const { thread_id, project, title, message, status, waiting_on_owner, started_at, updated_at } = value;
  return typeof thread_id === "string" && thread_id.length >= 1 && thread_id.length <= 256
    && typeof project === "string" && projectName.test(project)
    && typeof title === "string" && title.length >= 1 && title.length <= 200
    && (message === undefined || typeof message === "boolean")
    && typeof status === "string" && threadStatus.test(status)
    && typeof waiting_on_owner === "boolean"
    && typeof started_at === "number" && Number.isFinite(started_at)
    && typeof updated_at === "number" && Number.isFinite(updated_at);
}

function readSnapshot(value: unknown): WorkSnapshot | "unreadable" {
  if (!record(value)) return "unreadable";
  const { projects, threads } = value;
  if (!Array.isArray(projects) || projects.length > 32 || !projects.every(isProject)
    || !Array.isArray(threads) || threads.length > 50 || !threads.every(isThread)
    || new Set(projects.map((project) => project.name)).size !== projects.length
    || new Set(threads.map((thread) => thread.thread_id)).size !== threads.length) {
    return "unreadable";
  }
  return { projects, threads };
}

export async function readWork(signal: AbortSignal): Promise<WorkResult> {
  const response = await apiFetch("work", { signal, cache: "no-store", redirect: "error" });
  if (!response.ok) return response.status === 503 ? "unreadable" : "unreachable";
  const value: unknown = await response.json().catch(() => null);
  return readSnapshot(value);
}
