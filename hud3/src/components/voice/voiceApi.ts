import { apiFetch } from "../../api";

export const STATES = {
  resting: "Resting", listening: "Listening", working: "Working",
  speaking: "Speaking", asking: "Asking", unavailable: "Unavailable",
} as const;

interface Setting {
  value: number;
  min: number;
  max: number;
}

export type VoiceSettings = Record<"speed" | "follow_up_s" | "end_silence_ms", Setting>;
export type VoiceSetting = keyof VoiceSettings;

export interface VoiceTurn {
  heard_at: number;
  text: string;
  tools: { name: string; ok: boolean }[];
  approval_asked: boolean;
  sentence_count: number;
  silence_ms: number | null;
  decode_ms: number | null;
  first_audio_ms: number | null;
  first_say_s: number | null;
  end_s: number | null;
}

interface Engine {
  name: string | null;
  reason: string;
}

export interface VoiceSnapshot {
  state: keyof typeof STATES;
  reason?: string;
  problem: { text: string; hint: string; at: number } | null;
  transcriber: Engine;
  voice: Engine;
  detector: string;
  gpu_warm: boolean;
  counters: Record<string, number>;
  helper: { connected: boolean; listening: boolean; version?: string; canceller?: boolean; level?: number };
  brain: { name: string; state: string; turns: number };
  settings: VoiceSettings;
  turns: VoiceTurn[];
}

type Route = "voice" | "voiceSettings" | "voiceHelper";

async function request(route: Route, options: RequestInit) {
  const response = await apiFetch(route, { cache: "no-store", redirect: "error", ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message = body?.message ?? body?.error;
    throw new Error(typeof message === "string" ? message : "Voice request failed.");
  }
  return body;
}

export async function readVoice(signal: AbortSignal): Promise<VoiceSnapshot> {
  const data = await request("voice", { signal });
  if (!data || !Object.hasOwn(STATES, data.state) || !data.helper || !data.transcriber
      || !data.voice || !data.settings?.speed || !data.settings?.follow_up_s
      || !data.settings?.end_silence_ms || !Array.isArray(data.turns)) {
    throw new Error("Voice status could not be read.");
  }
  return data;
}

function post(route: "voiceSettings" | "voiceHelper", body: object) {
  return request(route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(5000),
  });
}

export async function saveVoiceSetting(name: VoiceSetting, value: number): Promise<VoiceSettings> {
  const data = await post("voiceSettings", { [name]: value });
  return data.settings;
}

export async function controlHelper(action: "start" | "stop"): Promise<string> {
  const data = await post("voiceHelper", { action });
  return data.message;
}
