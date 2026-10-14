import { browserApi } from "../api";

// Owner settings receipts only. Unknown/unavailable never enables a control.
let drive: boolean | null = null;
let request = 0;
const listeners = new Set<() => void>();
const commit = (value:unknown) => { drive=typeof value === "boolean" ? value:null; for(const notify of listeners)notify(); };
export const browserSettings = {
  subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  drive: () => drive,
  apply(value: unknown) { ++request; commit(value); },
  async refresh() {
    const seq = ++request;
    try { const value = await browserApi.get<{agents_may_drive?: unknown}>("/settings");
      if (seq === request) commit(value.agents_may_drive);
    } catch { if (seq === request) commit(null); }
  },
  async setDrive(value: boolean) {
    const seq=++request;
    try {
      const saved = await browserApi.write<{agents_may_drive?: unknown}>("/settings", {agents_may_drive: value}, "PATCH");
      if (typeof saved.agents_may_drive !== "boolean") throw Error("The setting could not be read back.");
      if(seq===request)commit(saved.agents_may_drive);
    } catch(error) { if(seq===request)commit(null); throw error; }
  },
};
