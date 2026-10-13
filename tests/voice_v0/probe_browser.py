"""WASM cost in headless Chromium: Silero VAD, RNNoise, sherpa-onnx KWS.

    JARVIS_VOICE_V0=1 PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
        python tests/voice_v0/probe_browser.py [--seconds 20] [--only silero,rnnoise,kws,combo,vadquality]

A local static server on an ephemeral 127.0.0.1 port serves the harness
(tests/voice_v0/browser/), the pinned onnxruntime-web / RNNoise / sherpa-onnx npm
packages and the pinned models. Each module runs in its own Web Worker, single
threaded (the HUD is not cross-origin isolated, so no SharedArrayBuffer threads).

CPU is measured two ways:
  * in-worker busy time (performance.now around each frame), divided by audio time;
  * browser-process CPU time from CDP SystemInfo.getProcessInfo across a PACED
    run in which the main thread posts one frame per real-time interval, exactly
    like an AudioWorklet port would. The same run with a do-nothing worker is
    subtracted, so the number is the module's incremental cost, including message
    passing and GC. Reported as % of ONE core.

`vadquality` is a separate experiment: Silero speech probabilities on synthetic
speech + generated noise / competing Kokoro voices, with and without RNNoise in
front, scored against energy labels from the clean speech. It answers "does
RNNoise raise VAD precision" on synthetic degradations only.

Headless Chromium here runs on the Linux/WSL box, not the owner's Windows Chrome;
treat the numbers as same-engine (V8, same WASM tier) but different-OS.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

import common
import make_noisy
import models

HERE = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    routes: dict[str, Path] = {}

    def translate_path(self, path):
        path = path.split("?", 1)[0]
        for prefix, root in self.routes.items():
            if path.startswith(prefix):
                rel = path[len(prefix):].lstrip("/")
                p = (root / rel).resolve()
                if root.resolve() in p.parents or p == root.resolve():
                    return str(p)
        return "/nonexistent"

    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm",
                      ".onnx": "application/octet-stream", ".f32": "application/octet-stream", "": "application/octet-stream"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def serve(audio_dir: Path):
    Handler.routes = {
        "/harness/": HERE / "browser",
        "/ort/": models.ensure("npm-onnxruntime-web-1.30.0") / "x" / "package" / "dist",
        "/rnnoise/": models.ensure("npm-jitsi-rnnoise-wasm-0.2.1") / "x" / "package" / "dist",
        "/sherpa/": models.ensure("npm-sherpa-onnx-1.13.8") / "x" / "package",
        "/models/": models.ROOT,
        "/audio/": audio_dir,
    }
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def bench_audio(seconds: float = 60.0) -> np.ndarray:
    """Dictation-like 16 kHz bench signal: Kokoro sentences separated by 400-900 ms pauses."""
    clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    rng = np.random.default_rng(7)
    out, n, i = [], 0, 0
    while n < seconds * common.SR:
        c = clips[(i * 11) % len(clips)]
        x = common.read_wav(common.FIXTURES / c["file"])
        gap = np.zeros(int(rng.uniform(0.4, 0.9) * common.SR), np.float32)
        out += [x, gap]
        n += len(x) + len(gap)
        i += 1
    return np.concatenate(out)[: int(seconds * common.SR)]


def to_48k(x: np.ndarray) -> np.ndarray:
    from scipy.signal import resample_poly

    return resample_poly(x, 3, 1).astype(np.float32)


def cpu_total(cdp) -> tuple[float, dict]:
    info = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
    return sum(p["cpuTime"] for p in info), {f'{p["type"]}:{p["id"]}': p["cpuTime"] for p in info}


class Browser:
    def __init__(self, base: str, pw):
        self.base = base
        self.browser = pw.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        self.version = self.browser.version
        self.cdp = self.browser.new_browser_cdp_session()
        self.page = self.browser.new_page()
        self.page.goto(f"{self.base}/harness/harness.html")
        self.page.wait_for_function("window.harnessReady === true")

    def prepare(self, spec):
        return self.page.evaluate("(s) => window.prepare(s)", spec)

    def run(self, jid):
        return self.page.evaluate("(i) => window.run(i)", jid)

    def burst(self, spec):
        spec = {**spec, "mode": "burst"}
        jid = self.prepare(spec)["id"]
        return self.run(jid)

    def paced_cpu(self, specs: list[dict], settle_s: float = 2.0) -> dict:
        """Run `specs` concurrently, paced; return CPU delta and the job outputs."""
        ids = [self.prepare({**s, "mode": "paced"})["id"] for s in specs]
        self.page.wait_for_timeout(int(settle_s * 1000))
        c0, per0 = cpu_total(self.cdp)
        import time

        t0 = time.perf_counter()
        outs = self.page.evaluate("(ids) => Promise.all(ids.map((i) => window.run(i)))", ids)
        wall = time.perf_counter() - t0
        c1, per1 = cpu_total(self.cdp)
        return {"cpu_s": c1 - c0, "wall_s": wall, "pct_of_core": 100 * (c1 - c0) / wall, "outs": outs,
                "per_process_s": {k: per1[k] - per0.get(k, 0) for k in per1}}

    def close(self):
        self.browser.close()


def audio_url_name(d: Path, name: str, x: np.ndarray) -> str:
    (d / name).write_bytes(x.astype("<f4").tobytes())
    return f"/audio/{name}"


def specs(base_url_audio16: str, audio48: str, seconds: float, silero_ver="silero-vad-v5.1.2") -> dict:
    kw_dir = models.extracted("kws-zipformer-gigaspeech-3.3M")
    rel = kw_dir.relative_to(models.ROOT)
    return {
        "null16": {"kind": "null", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds},
        "null48": {"kind": "null", "audio": audio48, "rate": 48000, "frame": 480, "seconds": seconds},
        "silero5": {"kind": "silero", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                    "model": f"/models/silero-vad-v5.1.2/silero_vad.onnx"},
        "silero6": {"kind": "silero", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                    "model": f"/models/silero-vad-v6.2.3/silero_vad.onnx"},
        "rnnoise": {"kind": "rnnoise", "audio": audio48, "rate": 48000, "frame": 480, "seconds": seconds},
        "kws_fp32": {"kind": "kws", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                     "int8": False, "modelBase": f"/models/{rel.as_posix()}", "keywords": "/audio/keywords.txt"},
        "kws_int8": {"kind": "kws", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                     "int8": True, "modelBase": f"/models/{rel.as_posix()}", "keywords": "/audio/keywords.txt"},
    }


def summarize_job(r: dict, base: dict, audio_s: float) -> dict:
    res = r["outs"][0]["result"]
    fm = np.array(res["frame_ms"]) if res.get("frame_ms") else np.array([0.0])
    return {
        "pct_of_core_total": r["pct_of_core"], "pct_of_core_minus_null_worker": r["pct_of_core"] - base["pct_of_core"],
        "cpu_s": r["cpu_s"], "wall_s": r["wall_s"],
        "worker_busy_pct_of_wall": 100 * res["busy_ms"] / 1000 / r["outs"][0]["cadence"]["wall_ms"] * 1000,
        "frame_ms": {"p50": float(np.percentile(fm, 50)), "p95": float(np.percentile(fm, 95)), "max": float(fm.max()),
                     "frame_budget_ms": 1000 * r["outs"][0]["spec"]["frame"] / r["outs"][0]["spec"]["rate"]},
        "cadence": r["outs"][0]["cadence"], "init": r["outs"][0]["init"], "errors": r["outs"][0]["errors"],
        "per_process_s": r["per_process_s"],
    }


def vad_quality(b: Browser, d: Path) -> dict:
    """Silero p(speech) on degraded speech, without and with RNNoise first (see module docstring)."""
    clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    picked = [clips[i] for i in range(0, len(clips), 14)][:24]
    audio = {c["file"]: common.read_wav(common.FIXTURES / c["file"]) for c in picked}
    gap = np.zeros(int(0.8 * common.SR), np.float32)
    clean = np.concatenate([np.concatenate([audio[c["file"]], gap]) for c in picked])
    # energy ground truth on the clean signal: 32 ms frames above -50 dBFS, dilated by 3 frames (96 ms)
    n = len(clean) // 512
    en = np.array([common.rms_db(clean[i * 512:(i + 1) * 512]) for i in range(n)])
    truth = en > -50
    kernel = np.ones(7, bool)
    truth = np.convolve(truth.astype(int), kernel.astype(int), mode="same") > 0

    conds = {}
    for name, fn in make_noisy.conditions().items():
        parts = []
        for i, c in enumerate(picked):
            x = fn(audio[c["file"]], i, picked, audio)
            parts += [x, gap]
        conds[name] = np.concatenate(parts)
    # pure-noise segments (no target speech) to count false accepts on noise alone
    nz = {"pink_noise_only": make_noisy.pink(len(clean), 3) * 0.05, "white_noise_only": make_noisy.white(len(clean), 4) * 0.05,
          "tv_only": 0.5 * np.concatenate([make_noisy.babble(picked, audio, i, len(audio[c["file"]]) + len(gap))
                                           for i, c in enumerate(picked)])[: len(clean)]}
    res = {"frames": int(n), "truth_speech_frames": int(truth.sum()), "conditions": {}}

    def silero(x: np.ndarray) -> np.ndarray:
        name = f"vq_{abs(hash(x.tobytes())) % 10**8}.f32"
        url = audio_url_name(d, name, x)
        out = b.burst({"kind": "silero", "audio": url, "rate": 16000, "frame": 512, "seconds": 0,
                       "model": "/models/silero-vad-v5.1.2/silero_vad.onnx"})
        return np.array(list(out["result"]["probs"].values()) if isinstance(out["result"]["probs"], dict) else out["result"]["probs"])

    def rnn(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x48 = to_48k(x)
        url = audio_url_name(d, f"rn_{abs(hash(x.tobytes())) % 10**8}.f32", x48)
        out = b.burst({"kind": "rnnoise", "audio": url, "rate": 48000, "frame": 480, "seconds": 0})
        from scipy.signal import resample_poly

        o = np.array(list(out["result"]["out"].values()) if isinstance(out["result"]["out"], dict) else out["result"]["out"], np.float32)
        v = np.array(list(out["result"]["vad"].values()) if isinstance(out["result"]["vad"], dict) else out["result"]["vad"], np.float32)
        return resample_poly(o, 1, 3).astype(np.float32), v

    def score(p: np.ndarray, thr=0.5) -> dict:
        m = min(len(p), len(truth))
        pred, t = p[:m] > thr, truth[:m]
        tp, fp, fn = int((pred & t).sum()), int((pred & ~t).sum()), int((~pred & t).sum())
        return {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1), "false_accept_frames": fp,
                "nonspeech_frames": int((~t).sum())}

    for name, x in conds.items():
        p_plain = silero(x)
        den, rn_vad = rnn(x)
        p_den = silero(den)
        res["conditions"][name] = {"silero": score(p_plain), "rnnoise_then_silero": score(p_den),
                                   "rnnoise_own_vad(thr0.5)": score(np.repeat(rn_vad, 1)[: len(truth) * 3: 3] if len(rn_vad) >= len(truth) * 3 else rn_vad[: len(truth)])}
        res["conditions"][name]["_denoised_wav"] = None
        common.write_wav(common.WORK / "denoised" / f"{name}.wav", den) if (common.WORK / "denoised").mkdir(exist_ok=True) is None else None
        common.write_wav(common.WORK / "denoised" / f"{name}_noisy.wav", np.clip(x, -1, 1))
    for name, x in nz.items():
        p_plain = silero(x.astype(np.float32))
        den, _ = rnn(x.astype(np.float32))
        p_den = silero(den)
        res["conditions"][name] = {"silero_frames_above_0.5_pct": float(100 * np.mean(p_plain > 0.5)),
                                   "rnnoise_then_silero_frames_above_0.5_pct": float(100 * np.mean(p_den > 0.5)),
                                   "note": "no target speech in this signal; every accepted frame is a false accept"}
    return res


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 browser probe (needs fixtures, models, Playwright Chromium)")
        return 0
    from playwright.sync_api import sync_playwright

    arg = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True) for a in argv if a.startswith("--")}
    seconds = float(arg.get("--seconds", 20))
    only = set(str(arg.get("--only", "silero,rnnoise,kws,combo,vadquality")).split(","))
    d = Path(tempfile.mkdtemp(prefix="voice-v0-audio-", dir=common.WORK))
    x16 = bench_audio(60)
    u16 = audio_url_name(d, "bench16.f32", x16)
    u48 = audio_url_name(d, "bench48.f32", to_48k(x16))
    # keywords file for the browser KWS (same tokens as the native probe)
    import probe_kws_native as kn

    kn.write_keywords(str(d / "keywords.txt"), 1.0, 0.25)
    S = specs(u16, u48, seconds)
    srv = serve(d)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    res = {"seconds_per_run": seconds, "audio": "60 s Kokoro dictation bench (synthetic), looped", "runs": {}}
    with sync_playwright() as pw:
        b = Browser(base, pw)
        res["chromium"] = b.version
        res["machine"] = {"cpu": "Intel Core Ultra 9 275HX (24 threads), WSL2, shared with other agents",
                          "note": "headless Chromium on Linux, single-thread WASM workers"}
        try:
            base16 = b.paced_cpu([S["null16"]])
            base48 = b.paced_cpu([S["null48"]])
            res["runs"]["baseline_null_worker_16k"] = {k: base16[k] for k in ("pct_of_core", "cpu_s", "wall_s")}
            res["runs"]["baseline_null_worker_48k"] = {k: base48[k] for k in ("pct_of_core", "cpu_s", "wall_s")}
            idle = b.paced_cpu([{**S["null16"], "seconds": 0.001}], settle_s=seconds)
            res["runs"]["idle_page_for_same_wall"] = {"pct_of_core": idle["pct_of_core"]}
            plan = {"silero": ["silero5", "silero6"], "rnnoise": ["rnnoise"], "kws": ["kws_int8", "kws_fp32"]}
            for group, names in plan.items():
                if group not in only:
                    continue
                for nm in names:
                    r = b.paced_cpu([S[nm]])
                    base = base48 if S[nm]["rate"] == 48000 else base16
                    res["runs"][nm] = summarize_job(r, base, seconds)
                    print(nm, f'{res["runs"][nm]["pct_of_core_minus_null_worker"]:.1f}% of a core', file=sys.stderr)
                    # burst for exact per-frame stats + KWS hits on the bench
                    bu = b.burst(S[nm])
                    br = bu["result"]
                    res["runs"][nm]["burst_throughput_x_realtime"] = br["audio_s"] / (br["wall_ms"] / 1000)
                    fm = np.array(br["frame_ms"])
                    res["runs"][nm]["burst_frame_ms"] = {"p50": float(np.percentile(fm, 50)), "p95": float(np.percentile(fm, 95)),
                                                         "max": float(fm.max())}
                    if "hits" in br:
                        res["runs"][nm]["kws_hits_on_bench"] = br["hits"]
            if "combo" in only:
                r = b.paced_cpu([S["rnnoise"], S["silero5"], S["kws_int8"]])
                res["runs"]["combo_rnnoise_silero_kws_int8"] = {
                    "pct_of_core_total": r["pct_of_core"],
                    "pct_of_core_minus_three_null_workers": r["pct_of_core"] - (base48["pct_of_core"] + 2 * base16["pct_of_core"]),
                    "cadence": [o["cadence"] for o in r["outs"]],
                    "worker_frame_ms_p95": [float(np.percentile(o["result"]["frame_ms"], 95)) for o in r["outs"]]}
                r2 = b.paced_cpu([S["silero5"], S["kws_int8"]])
                res["runs"]["combo_silero_kws_int8_no_rnnoise"] = {
                    "pct_of_core_total": r2["pct_of_core"],
                    "pct_of_core_minus_two_null_workers": r2["pct_of_core"] - 2 * base16["pct_of_core"],
                    "cadence": [o["cadence"] for o in r2["outs"]]}
            if "vadquality" in only:
                res["vad_quality"] = vad_quality(b, d)
        finally:
            b.close()
            srv.shutdown()
    common.emit("browser_cpu", res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
