// Lowering the Remote Browser's sound while Jarvis speaks (V15): the HUD's half.
//
// The daemon lowers the Remote Browser's own Pulse streams by about 15 dB, but
// only under a short lease (3 s) that this class keeps alive: the first
// request when speech turns active, a renewal every second while it stays so,
// and `on: false` the moment it ends. A closed window, a lost connection or a
// crash simply stops the renewals, and the daemon restores by itself. Nothing
// here ever asks for more than the duck: it cannot unmute, and it does not
// know which streams exist. Direct frames are not affected (they play inside
// this window under the browser's own rules).
//
// Requests never overlap (a slow daemon is not buried in renewals, and an `off`
// cannot overtake an earlier `on`), and a failed one is dropped: the next
// renewal tries again and, failing that, the lease expires.

import type { Clock } from "./queue";

/** The daemon's lease is 3 s; renewing at a third of it survives two lost requests. */
export const RENEW_MS = 1000;

export interface DuckDeps {
  /** One lease request. `keepalive`: the window may be going away. */
  send(on: boolean, keepalive?: boolean): Promise<unknown>;
  clock: Clock;
}

export class DuckLease {
  private enabled: boolean;
  private speaking = false;
  /** A request that may have granted a lease went out and no `off` has followed. */
  private held = false;
  private timer: unknown = null;
  private pumping = false;
  private dirty = false;
  private keepalive = false;

  constructor(private readonly deps: DuckDeps, enabled = true) {
    this.enabled = enabled;
  }

  /** The Settings switch ("Lower Browser sound while Jarvis speaks"). */
  setEnabled(on: boolean): void {
    this.enabled = on;
    this.update();
  }

  isEnabled(): boolean {
    return this.enabled;
  }

  /** The speech controller's `active`: playing, buffering, or inside the hangover. */
  setSpeaking(active: boolean): void {
    this.speaking = active;
    this.update();
  }

  /** The window is going away: end the lease now (a best-effort request that may outlive the page). */
  release(): void {
    this.speaking = false;
    this.keepalive = true;      // from here on a request may outlive the page
    this.update();
  }

  dispose(): void {
    this.release();
  }

  private wanted(): boolean {
    return this.enabled && this.speaking;
  }

  private update(): void {
    if (this.wanted()) {
      if (this.timer === null) {
        this.schedule();
        this.request();
      }
    } else {
      this.cancel();
      if (this.held) this.request();
    }
  }

  private schedule(): void {
    this.timer = this.deps.clock.setTimeout(() => {
      this.timer = null;
      if (!this.wanted()) return;
      this.schedule();
      this.request();
    }, RENEW_MS);
  }

  private cancel(): void {
    if (this.timer !== null) this.deps.clock.clearTimeout(this.timer);
    this.timer = null;
  }

  /** Ask for the state wanted now; one request at a time, the newest wish wins. */
  private request(): void {
    this.dirty = true;
    if (this.pumping) return;
    void this.pump();
  }

  private async pump(): Promise<void> {
    this.pumping = true;
    try {
      while (this.dirty) {
        this.dirty = false;
        const on = this.wanted();
        if (!on && !this.held) break;
        this.held = on;
        try {
          await this.deps.send(on, this.keepalive || undefined);
        } catch {
          /* non-fatal: the next renewal retries, and the daemon's lease expires on its own */
        }
      }
    } finally {
      this.pumping = false;
    }
  }
}
