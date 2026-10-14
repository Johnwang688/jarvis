// What the ML VAD loads, pinned (voice rework V5, plan §3.2).
//
// The model is vendored (`hud/vendor/silero-vad/`, MIT) and built into
// `dist/assets/` with a content hash by Vite, so it is served from the HUD's
// own origin and nothing is fetched from a CDN. The worker still checks the
// bytes it fetched against this digest before it builds a session: a file that
// was swapped, truncated or served wrong falls back to the energy detector with
// a stated reason, and never runs. The same digest is `silero-vad-v5.1.2` in
// tests/voice_v0/models.json (V0's parity check ran against that file), and
// `tests/face/hud_v2_vad_check.py` + `pins.test.ts` hold the three together.

export const SILERO_VERSION = "snakers4/silero-vad v5.1.2";
export const SILERO_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f";
export const SILERO_BYTES = 2327524;
/** Silero v5 at 16 kHz takes exactly this many samples (32 ms) per call... */
export const SILERO_FRAME = 512;
/** ...preceded by this many samples of the previous frame (its context). */
export const SILERO_CONTEXT = 64;
/** The recurrent state, [2, 1, 128]. */
export const SILERO_STATE = 2 * 128;
