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

// RNNoise (voice rework V14, plan §3.4): the noise-suppression stage, off by
// default. The WebAssembly binary is vendored (`hud/vendor/rnnoise/`, BSD-3-Clause
// for RNNoise, Apache-2.0 for Jitsi's build; both licences travel with it) and
// built into `dist/assets/` with a content hash. It is the binary inside
// `@jitsi/rnnoise-wasm` 0.2.1's `rnnoise-sync.js`, decoded unchanged
// (`hud/vendor/rnnoise/extract_wasm.py`); that tarball is
// `npm-jitsi-rnnoise-wasm-0.2.1` in tests/voice_v0/models.json. The worker checks
// the fetched bytes' length and SHA-256 before compiling them.

export const RNNOISE_VERSION = "xiph/rnnoise 0.2 via @jitsi/rnnoise-wasm 0.2.1";
export const RNNOISE_SHA256 = "4f513a50613de74378331237886138eab52fa2650e8b1a41eb587d932d9b8850";
export const RNNOISE_BYTES = 1440118;
/** RNNoise runs on 48 kHz audio in frames of 480 samples (10 ms)... */
export const RNNOISE_FRAME = 480;
/** ...which is this many samples (10 ms) at the HUD's 16 kHz. */
export const RNNOISE_FRAME_16K = 160;
