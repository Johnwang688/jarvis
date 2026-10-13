import { api, browserApi } from "../api";
import { BrowserSoundStore } from "./browserSound";
/** One authoritative state per window; all pane marks share it. */
export const browserSound = new BrowserSoundStore({ status: () => api.status(), set: desired => api.setBrowserSound(desired), sound: (tab, on) => browserApi.write("/tabs/"+tab+"/sound", {on}) });