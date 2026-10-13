import { useEffect, useState } from "react";

interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export type InstallStatus = "available" | "installed" | "unavailable";
export const INSTALL_MENU = "Chrome's menu → Cast, save and share → Install page as app";

/** Listen for the whole HUD session, including while Settings is closed. */
export class InstallApp {
  private pending: InstallPrompt | null = null;
  private installed: boolean;
  private listeners = new Set<() => void>();
  private media: MediaQueryList;

  constructor(private host: Window) {
    this.media = host.matchMedia("(display-mode: standalone)");
    this.installed = this.media.matches;
  }

  status = (): InstallStatus => this.installed || this.media.matches ? "installed"
    : this.pending ? "available" : "unavailable";
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };
  private changed = () => { for (const fn of this.listeners) fn(); };
  private offered = (event: Event) => {
    event.preventDefault();
    if (!this.installed) this.pending = event as InstallPrompt;
    this.changed();
  };
  private didInstall = () => {
    this.installed = true;
    this.pending = null;
    this.changed();
  };
  connect = () => {
    this.host.addEventListener("beforeinstallprompt", this.offered);
    this.host.addEventListener("appinstalled", this.didInstall);
    this.media.addEventListener("change", this.changed);
    return () => {
      this.host.removeEventListener("beforeinstallprompt", this.offered);
      this.host.removeEventListener("appinstalled", this.didInstall);
      this.media.removeEventListener("change", this.changed);
    };
  };
  async install(blocked: boolean): Promise<void> {
    if (blocked || this.status() !== "available" || !this.pending) return;
    const event = this.pending;
    // A browser offer is single-use. Even dismissal needs a fresh offer.
    this.pending = null;
    this.changed();
    try {
      await event.prompt();
      await event.userChoice;
      // appinstalled (or standalone) is the evidence of installation.
    } catch {
      // A withdrawn offer falls back to the browser's menu instruction.
    }
  }
}

export function useInstallApp(blocked: boolean) {
  const [app] = useState(() => new InstallApp(window));
  const [status, setStatus] = useState(app.status);
  useEffect(() => {
    const unsubscribe = app.subscribe(() => setStatus(app.status()));
    const disconnect = app.connect();
    setStatus(app.status());
    return () => { unsubscribe(); disconnect(); };
  }, [app]);
  return { status, install: () => app.install(blocked) };
}
