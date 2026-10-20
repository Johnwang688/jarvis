import { browserApi } from "../api";

// Owner settings receipts only. Unknown/unavailable never enables a control.
export interface ProbeStatus { passed: boolean; reason: string; at: string }
let drive: boolean | null = null;
let direct: boolean | null = null;
let probe: ProbeStatus | null = null;
let request = 0;
const listeners = new Set<() => void>();
const notify = () => { for (const fn of listeners) fn(); };
const commit = (value:unknown) => { drive=typeof value === "boolean" ? value:null; notify(); };
const commitDirect = (value: unknown) => { direct = typeof value === "boolean" ? value : null; notify(); };
const readProbe = (value: unknown): ProbeStatus | null => {
  const p = (value as { probe?: unknown } | null)?.probe as ProbeStatus | null | undefined;
  return p && typeof p.passed === "boolean" && typeof p.reason === "string" && typeof p.at === "string"
    ? { passed: p.passed, reason: p.reason.slice(0, 40), at: p.at.slice(0, 40) } : null;
};
export const browserSettings = {
  subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  drive: () => drive,
  /** B-DIRECT-CAPTURE: true only on the daemon's explicit receipt. */
  directCapture: () => direct,
  probe: () => probe,
  apply(value: unknown) { ++request; if (value === null) direct = null; commit(value); },
  async refresh() {
    const seq = ++request;
    try { const value = await browserApi.get<{agents_may_drive?: unknown; direct_capture?: unknown}>("/settings");
      if (seq === request) { direct = typeof value.direct_capture === "boolean" ? value.direct_capture : null; commit(value.agents_may_drive); }
    } catch { if (seq === request) { direct = null; commit(null); } }
  },
  async setDrive(value: boolean) {
    const seq=++request;
    try {
      const saved = await browserApi.write<{agents_may_drive?: unknown}>("/settings", {agents_may_drive: value}, "PATCH");
      if (typeof saved.agents_may_drive !== "boolean") throw Error("The setting could not be read back.");
      if(seq===request)commit(saved.agents_may_drive);
    } catch(error) { if(seq===request)commit(null); throw error; }
  },
  async setDirectCapture(value: boolean) {
    // Off first: whatever the answer, nothing captures until a receipt says on.
    const before = direct;
    direct = value ? before : null; notify();
    try {
      const saved = await browserApi.write<{direct_capture?: unknown}>("/settings", {direct_capture: value}, "PATCH");
      if (typeof saved.direct_capture !== "boolean") throw Error("The setting could not be read back.");
      commitDirect(saved.direct_capture);
    } catch (error) { commitDirect(value ? before : null); throw error; }
  },
  async refreshProbe() {
    try { probe = readProbe(await browserApi.get<unknown>("/direct-capture/probe")); }
    catch { probe = null; }
    notify();
  },
  async reportProbe(facts: unknown): Promise<ProbeStatus & { saved: boolean }> {
    const out = await browserApi.write<ProbeStatus & { saved?: boolean }>("/direct-capture/probe", facts);
    probe = readProbe({ probe: out });
    notify();
    if (!probe) throw Error("The probe result could not be read back.");
    return { ...probe, saved: out.saved === true };
  },
};
