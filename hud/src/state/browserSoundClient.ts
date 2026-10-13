import { api } from "../api";
import { BrowserSoundStore } from "./browserSound";
/** One authoritative state per window; all pane marks share it. */
export const browserSound = new BrowserSoundStore({ status: () => api.status(), set: desired => api.setBrowserSound(desired) });