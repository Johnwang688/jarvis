import type { MicInput } from "./micInput";
import type { VoiceAgentStatus } from "../api";

export type MicSource = "waiting" | "browser" | "windows" | "stopped";

export function microphoneSource(status: VoiceAgentStatus | null, checked: boolean, switching: boolean, paused: boolean): MicSource {
  if (switching || !checked) return "waiting";
  if (status?.shared_mic) return "windows";
  if (status && (status.shared_capable || status.can_stop || ["connected", "ready", "starting", "stopping", "connect_failed"].includes(status.state))) return "waiting";
  if (paused || status?.hud_mic_paused) return "stopped";
  return "browser";
}

// HTTP read boundaries need not match Windows callbacks, samples, or VAD frames.
export function pcmFrames(deliver: (frame: Float32Array) => void) {
  let frame = new Float32Array(512);
  let filled = 0;
  let low = -1;
  return (bytes: Uint8Array) => {
    for (const byte of bytes) {
      if (low === -1) { low = byte; continue; }
      const value = low | (byte << 8);
      frame[filled++] = (value >= 32768 ? value - 65536 : value) / 32768;
      low = -1;
      if (filled === frame.length) {
        deliver(frame);
        frame = new Float32Array(512);
        filled = 0;
      }
    }
  };
}

export function startSharedMic(onFrame: (frame: Float32Array) => void, onHealth: (live: boolean) => void): MicInput {
  const listeners = new Set<(frame: Float32Array) => void>();
  let stopped = false;
  let controller: AbortController | undefined;
  let retry: ReturnType<typeof setTimeout>;
  let available: boolean | undefined;
  const health = (live: boolean) => {
    if (!stopped && live !== available) { available = live; onHealth(live); }
  };
  const connect = async () => {
    const request = new AbortController();
    controller = request;
    let deadline: ReturnType<typeof setTimeout>;
    const arm = () => {
      clearTimeout(deadline);
      deadline = setTimeout(() => request.abort(), 3500);
    };
    // A disconnected response is a new audio timeline. Never complete a frame
    // with samples from the other side of a gap.
    const push = pcmFrames(frame => {
      health(true);
      onFrame(frame);
      for (const fn of listeners) { try { fn(frame); } catch { /* observer only */ } }
    });
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      arm();
      const res = await fetch("/voice-agent/microphone", {
        signal: request.signal, mode: "same-origin", cache: "no-store", redirect: "error",
      });
      if (!res.ok || !res.body || res.headers.get("Content-Type") !== "application/octet-stream") return;
      reader = res.body.getReader();
      while (!stopped) {
        const { value, done } = await reader.read();
        if (done || stopped) break;
        arm();
        push(value);
      }
    } catch { /* Reconnect only while this input is still selected. */ }
    finally {
      clearTimeout(deadline!);
      request.abort();
      reader?.releaseLock();
      if (!stopped) {
        health(false);
        retry = setTimeout(() => { void connect(); }, 500);
      }
    }
  };
  void connect();
  return {
    path: "windows_shared", fallbackReason: "", sampleRate: 16000,
    addFrameListener: fn => { listeners.add(fn); return () => { listeners.delete(fn); }; },
    stop: () => { stopped = true; clearTimeout(retry); controller?.abort(); listeners.clear(); },
  };
}
