// The serving daemon's ports and frame hardening, read from `/status` once
// per page (WP-E). The Preview tab judges every URL with them, and a
// terminal's "Open in Preview" judges a link with them before it offers to.
//
// **Never a constant in place of an answer.** A hard-coded 8403 once sent the
// HUD suite's fixture project to the owner's live daemon. Until `/status`
// answers the state is `pending` and nothing that needs the ports runs; a
// `/status` that fails five times is `failed`, which judges with the defaults
// (8402 and 8405 refused, as always) and never lets a page keep its origin —
// "the daemon did not say it is frame-hardened" is not "it is".

import { useEffect, useSyncExternalStore } from "react";
import { api } from "../api";
import { DEFAULT_PORTS, daemonPortsFrom, type DaemonPorts } from "../lib/preview";

export interface PortsState {
  status: "pending" | "ok" | "failed";
  ports: DaemonPorts;
}

/** The port this window was served from. */
function selfPort(): number | null {
  if (typeof location === "undefined") return null;
  const n = Number(location.port || (location.protocol === "https:" ? 443 : 80));
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : null;
}

let state: PortsState = { status: "pending", ports: { ...DEFAULT_PORTS, self: selfPort() } };
let started = false;
const listeners = new Set<() => void>();

function set(next: PortsState) {
  state = next;
  for (const fn of Array.from(listeners)) fn();
}

/** Ask `/status` (once per page), retrying a failure up to five times. */
export function loadDaemonPorts() {
  if (started) return;
  started = true;
  const ask = (attempt: number) => {
    api
      .status()
      .then((status) => set({ status: "ok", ports: daemonPortsFrom(status, selfPort()) }))
      .catch(() => {
        if (attempt < 5) setTimeout(() => ask(attempt + 1), 1000 * (attempt + 1));
        else set({ status: "failed", ports: { ...DEFAULT_PORTS, self: selfPort() } });
      });
  };
  ask(0);
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** What the daemon reported, as it arrives. */
export function useDaemonPorts(): PortsState {
  useEffect(loadDaemonPorts, []);
  return useSyncExternalStore(subscribe, () => state);
}

/** The same, read once (outside React). */
export function daemonPortsNow(): PortsState {
  loadDaemonPorts();
  return state;
}
