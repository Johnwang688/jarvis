import { api } from "../api";
import { WINDOW_ID } from "../lib/windowId";
import { DevServerStore } from "./devservers";

/** Exactly one manager per live HUD window, shared by SSE and terminal receipts. */
export const devservers = new DevServerStore(WINDOW_ID, {
  list: window => api.devServers(window),
  claim: (id, body) => api.claimDevServer(id, body),
  act: (id, action, window) => api.actDevServer(id, action, window),
});
