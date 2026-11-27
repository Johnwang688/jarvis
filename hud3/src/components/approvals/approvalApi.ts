import { apiFetch } from "../../api";

export interface ApprovalRow {
  label: string;
  value: string;
  source?: "page";
}

export interface ApprovalRequest {
  id: string;
  digest: string;
  tool: string;
  card: { title: string; rows: ApprovalRow[] };
  level: string;
  why: string;
  asked_at: number;
  expires_in: number;
}

// A malformed presentation can still be denied using the verified envelope.
export type ListedApproval = Omit<ApprovalRequest, "card"> & {
  card: ApprovalRequest["card"] | null;
};

export type Decision = "approve" | "deny";

const readOptions = { cache: "no-store", redirect: "error" } as const;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseApproval(value: unknown): ListedApproval {
  if (!record(value)) throw new Error("Invalid approval request");
  const { id, digest, tool, card, level, why, asked_at, expires_in } = value;
  if (typeof id !== "string" || !id || typeof digest !== "string" || !digest
    || typeof level !== "string" || typeof why !== "string"
    || typeof asked_at !== "number" || !Number.isFinite(asked_at)
    || typeof expires_in !== "number" || expires_in < 0 || !Number.isFinite(expires_in * 1000)) {
    throw new Error("Invalid approval request");
  }
  const display = record(card) && typeof card.title === "string" && Array.isArray(card.rows)
    && card.rows.every((row): row is ApprovalRow =>
      record(row) && typeof row.label === "string" && typeof row.value === "string"
        && (row.source === undefined || row.source === "page"))
    ? { title: card.title, rows: card.rows } : null;
  return { id, digest, tool: typeof tool === "string" ? tool : "", card: display, level, why, asked_at, expires_in };
}

export async function readApprovals(signal: AbortSignal, sessionId?: string | null): Promise<ListedApproval[]> {
  const response = await apiFetch("approvals", { ...readOptions, signal });
  if (!response.ok) throw new Error("Approval list unavailable");
  const value: unknown = await response.json();
  if (!record(value) || !Array.isArray(value.approvals)) {
    throw new Error("Invalid approval list");
  }
  if (sessionId !== undefined && value.session_id !== undefined && value.session_id !== sessionId) return [];
  return value.approvals.map(parseApproval);
}

export async function answerApproval({ id, digest }: Pick<ApprovalRequest, "id" | "digest">,
  decision: Decision, signal: AbortSignal) {
  const response = await apiFetch("approvalsAnswer", {
    ...readOptions, method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, digest, decision }), signal,
  });
  if (response.status === 404 || response.status === 409) return "gone";
  if (!response.ok) throw new Error("Approval answer failed");
  const value: unknown = await response.json();
  if (!record(value) || value.id !== id || value.state !== (decision === "approve" ? "approved" : "denied")) {
    throw new Error("Invalid approval answer");
  }
  return "recorded";
}
