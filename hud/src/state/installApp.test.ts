import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { GeneralSettings } from "../components/Pickers";
import { InstallApp, INSTALL_MENU } from "./installApp";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function setup(standalone = false) {
  const host = new EventTarget();
  const media = Object.assign(new EventTarget(), { matches: standalone });
  Object.assign(host, { matchMedia: vi.fn(() => media) });
  const app = new InstallApp(host as unknown as Window);
  const disconnect = app.connect();
  return { app, host, media, disconnect };
}
function offer(host: EventTarget, outcome = "dismissed") {
  const event = Object.assign(new Event("beforeinstallprompt", { cancelable: true }), {
    prompt: vi.fn().mockResolvedValue(undefined), userChoice: Promise.resolve({ outcome }),
  });
  host.dispatchEvent(event);
  return event;
}

afterEach(() => vi.restoreAllMocks());
describe("HUD app installation", () => {
  it("captures and prevents the offer before Settings opens; prompts only once", async () => {
    const { app, host, disconnect } = setup();
    const update = vi.fn(); app.subscribe(update);
    expect(app.status()).toBe("unavailable");
    const event = offer(host);
    expect(event.defaultPrevented).toBe(true);
    expect(app.status()).toBe("available");
    expect(update).toHaveBeenCalled();
    await app.install(false); await app.install(false);
    expect(event.prompt).toHaveBeenCalledTimes(1);
    expect(app.status()).toBe("unavailable");
    disconnect(); offer(host);
    expect(app.status()).toBe("unavailable");
  });
  it("keeps the offer inert under an approval and usable afterwards", async () => {
    const { app, host } = setup(); const event = offer(host);
    await app.install(true);
    expect(event.prompt).not.toHaveBeenCalled();
    expect(app.status()).toBe("available");
    await app.install(false);
    expect(event.prompt).toHaveBeenCalledTimes(1);
  });
  it("shows Installed in standalone mode and after appinstalled", async () => {
    expect(setup(true).app.status()).toBe("installed");
    const { app, host, media } = setup();
    media.matches = true; media.dispatchEvent(new Event("change"));
    expect(app.status()).toBe("installed");
    media.matches = false;
    const event = offer(host, "accepted");
    await app.install(false);
    expect(app.status()).toBe("unavailable");
    host.dispatchEvent(new Event("appinstalled"));
    offer(host); await app.install(false);
    expect(app.status()).toBe("installed");
    expect(event.prompt).toHaveBeenCalledTimes(1);
  });
  it("handles a withdrawn offer without an unhandled rejection", async () => {
    const { app, host } = setup(); const event = offer(host);
    event.prompt.mockRejectedValue(new Error("withdrawn"));
    await app.install(false);
    expect(app.status()).toBe("unavailable");
  });
  it("Settings disables the install row under approvals and shows plain fallback text", () => {
    const container = document.createElement("div");
    const root = createRoot(container); const onInstall = vi.fn();
    const render = (installStatus: "available" | "installed" | "unavailable", blocked: boolean) => {
      act(() => root.render(createElement(GeneralSettings, {
         onInstall, installStatus, blocked,
      })));
    };
    try {
      render("available", true);
      const button = container.querySelector('[data-testid="install-app"] button') as HTMLButtonElement;
      expect(button.disabled).toBe(true); button.click(); expect(onInstall).not.toHaveBeenCalled();
      render("available", false); button.click(); expect(onInstall).toHaveBeenCalledTimes(1);
      render("installed", false);
      expect(container.querySelector('[data-testid="install-app"]')?.textContent).toBe("Installed");
      render("unavailable", false);
      const row = container.querySelector('[data-testid="install-app"]');
      expect(row?.textContent).toBe(INSTALL_MENU); expect(row?.querySelector("button, a")).toBeNull();
    } finally { act(() => root.unmount()); }
  });
});
