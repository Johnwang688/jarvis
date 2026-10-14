"""WASM cost in headless Chromium: Silero VAD, RNNoise, sherpa-onnx KWS.

    JARVIS_VOICE_V0=1 PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
        python tests/voice_v0/probe_browser.py [--seconds 20] [--only silero,rnnoise,kws,combo,vadquality]

A local static server on an ephemeral 127.0.0.1 port serves the harness
(tests/voice_v0/browser/), the pinned onnxruntime-web / RNNoise / sherpa-onnx npm
packages and the pinned models. Each module runs in its own Web Worker, single
threaded (the HUD is not cross-origin isolated, so no SharedArrayBuffer threads).

KWS: sherpa-onnx v1.13.8 publishes no browser KWS WASM (its release assets carry TTS, VAD
and speech-enhancement builds only), and the npm "sherpa-onnx" package is the Node
build (unconditional require("path") plus NODERAWFS), which does not initialise in a
worker. So KWS cost is measured with that same WASM binary under Node, which uses the
same V8 WASM tiers as Chrome, on a single thread (kws_node_wasm.js). Add the harness's
null-worker baseline for message passing to estimate the in-worker figure.

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

import common  # noqa: F401,I001  (first: pins BLAS threads before numpy loads)
import numpy as np

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


def renderer_rss_mb(cdp) -> float:
    """Sum of VmRSS of the renderer process(es) (CDP gives their pids)."""
    total = 0.0
    for pr in cdp.send("SystemInfo.getProcessInfo")["processInfo"]:
        if pr["type"] == "renderer":
            try:
                for line in Path(f"/proc/{pr['id']}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) / 1024
            except OSError:
                pass
    return total


def cpu_total(cdp) -> tuple[float, dict]:
    info = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
    return sum(p["cpuTime"] for p in info), {f'{p["type"]}:{p["id"]}': p["cpuTime"] for p in info}


class Browser:
    def __init__(self, base: str, pw):
        self.base = base
        # Everything the pages run (onnxruntime-web, RNNoise, the harness) comes from the loopback server; the
        # resolver rule makes any other host unresolvable, so downloaded code cannot reach the network.
        self.browser = pw.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"])
        self.version = self.browser.version
        self.cdp = self.browser.new_browser_cdp_session()
        self.page = self.browser.new_page()
        self.page.goto(f"{self.base}/harness/harness.html")
        self.page.wait_for_function("window.harnessReady === true")

    def prepare(self, spec):
        return self.page.evaluate("(s) => window.prepare(s)", spec)

    def burst(self, spec):
        jid = self.prepare({**spec, "mode": "burst"})["id"]
        return self.page.evaluate("(i) => window.burst(i)", jid)

    def paced_cpu(self, specs: list[dict], segments: int = 6, seg_s: float = 5.0, settle_s: float = 2.0) -> dict:
        """Run `specs` concurrently, paced, in segments; CPU delta per segment from CDP."""
        import time

        rss0 = renderer_rss_mb(self.cdp)
        ids = [self.prepare({**s, "mode": "paced"})["id"] for s in specs]
        self.page.wait_for_timeout(int(settle_s * 1000))
        segs, cad = [], []
        for _ in range(segments):
            c0, _ = cpu_total(self.cdp)
            t0 = time.perf_counter()
            r = self.page.evaluate("([ids, s]) => Promise.all(ids.map((i) => window.pacedSegment(i, s)))", [ids, seg_s])
            wall = time.perf_counter() - t0
            c1, _ = cpu_total(self.cdp)
            segs.append({"cpu_s": c1 - c0, "wall_s": wall, "pct": 100 * (c1 - c0) / wall})
            cad.append(r)
        rss1 = renderer_rss_mb(self.cdp)
        outs = [self.page.evaluate("(i) => window.finish(i)", i) for i in ids]
        pcts = [s["pct"] for s in segs]
        steady = pcts[1:] if len(pcts) > 2 else pcts
        return {"segments_pct": pcts, "first_segment_pct": pcts[0], "steady_pct_median": float(np.median(steady)),
                "steady_pct_max": float(np.max(steady)), "pct_of_core": float(np.median(steady)),
                "renderer_rss_mb_before": rss0, "renderer_rss_mb_running": rss1,
                "cpu_s": sum(s["cpu_s"] for s in segs), "wall_s": sum(s["wall_s"] for s in segs),
                "cadence": cad, "outs": outs}

    def close(self):
        self.browser.close()


def audio_url_name(d: Path, name: str, x: np.ndarray) -> str:
    (d / name).write_bytes(x.astype("<f4").tobytes())
    return f"/audio/{name}"


def specs(base_url_audio16: str, audio48: str, seconds: float, silero_ver="silero-vad-v5.1.2") -> dict:
    return {
        "null16": {"kind": "null", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds},
        "null48": {"kind": "null", "audio": audio48, "rate": 48000, "frame": 480, "seconds": seconds},
        "silero5": {"kind": "silero", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                    "model": f"/models/silero-vad-v5.1.2/silero_vad.onnx"},
        "silero6": {"kind": "silero", "audio": base_url_audio16, "rate": 16000, "frame": 512, "seconds": seconds,
                    "model": f"/models/silero-vad-v6.2.3/silero_vad.onnx"},
        "rnnoise": {"kind": "rnnoise", "audio": audio48, "rate": 48000, "frame": 480, "seconds": seconds},
    }


def summarize_job(r: dict, base: dict) -> dict:
    out0 = r["outs"][0]
    res = out0["result"]
    fm = np.array(res["frame_ms"]) if res.get("frame_ms") else np.array([0.0])
    return {
        "pct_of_core_steady_median": r["steady_pct_median"],
        "pct_of_core_steady_max": r["steady_pct_max"],
        "pct_of_core_first_segment": r["first_segment_pct"],
        "pct_of_core_steady_minus_null_worker": r["steady_pct_median"] - base["steady_pct_median"],
        "segments_pct": r["segments_pct"],
        "renderer_rss_mb_delta_vs_before_prepare": r["renderer_rss_mb_running"] - r["renderer_rss_mb_before"],
        "worker_busy_pct_of_wall": 100 * res["busy_ms"] / (r["wall_s"] * 1000),
        "frame_ms": {"p50": float(np.percentile(fm, 50)), "p95": float(np.percentile(fm, 95)), "max": float(fm.max()),
                     "frame_budget_ms": 1000 * out0["spec"]["frame"] / out0["spec"]["rate"],
                     "note": "performance.now() is coarsened to 0.1 ms"},
        "late_frames": sum(c[0]["late_frames"] for c in r["cadence"]), "max_late_ms": max(c[0]["max_late_ms"] for c in r["cadence"]),
        "init": out0["init"], "errors": out0["errors"],
    }


def kws_under_node(d: Path, allow_no_netns: bool = False) -> dict:
    """sherpa-onnx's own KWS WASM build, run under Node (V8). See module docstring for why not Chromium.

    Fails closed: pinned third-party npm code only runs with no network. Without an unprivileged network
    namespace the step is skipped (and the reason recorded) unless --allow-no-netns was given.
    """
    import shutil
    import subprocess

    out = {}
    node = shutil.which("node")
    if not node:
        msg = "node not found on PATH; install Node 22 to measure the KWS WASM build"
        print("kws node: skipped:", msg, file=sys.stderr)
        return {"skipped": msg}
    # Pinned third-party npm code executes here: empty environment, and no network where the kernel allows
    # an unprivileged network namespace. (Node's --permission model breaks Emscripten's process.binding.)
    jail = ["unshare", "-rn"] if shutil.which("unshare") and subprocess.run(
        ["unshare", "-rn", "true"], capture_output=True).returncode == 0 else []
    if not jail and not allow_no_netns:
        msg = ("no unprivileged network namespace (`unshare -rn true` failed); not running downloaded npm code "
               "without network confinement. Pass --allow-no-netns to run it under `env -i` only")
        print("kws node: skipped:", msg, file=sys.stderr)
        return {"skipped": msg}
    pkg = models.ensure("npm-sherpa-onnx-1.13.8") / "x" / "package"
    model = models.extracted("kws-zipformer-gigaspeech-3.3M")
    out["confinement"] = ("env -i" + (" + unshare -rn (no network)" if jail else " only (no network namespace available)"))
    for flag in ("fp32", "int8"):
        p = subprocess.run([*jail, "env", "-i", "JARVIS_VOICE_V0=1", node, str(HERE / "kws_node_wasm.js"), str(pkg),
                            str(model), str(d / "keywords.txt"), str(d / "bench16.f32"), flag],
                           capture_output=True, text=True, timeout=600)
        out[flag] = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {"error": p.stderr[-800:]}
        print("kws node", flag, {k: v for k, v in out[flag].items() if k != "hits"}, file=sys.stderr)
    return out


def _f32(b64: str) -> np.ndarray:
    import base64

    return np.frombuffer(base64.b64decode(b64), dtype="<f4").copy()


def vad_quality(b: Browser, d: Path, n_clips: int = 36) -> dict:
    """Silero p(speech) on degraded speech, without and with RNNoise first (see module docstring)."""
    from scipy.signal import resample_poly

    clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    picked = [clips[(i * 9) % len(clips)] for i in range(n_clips)]   # spread over voices and sentences
    audio = {c["file"]: common.read_wav(common.FIXTURES / c["file"]) for c in picked}
    gap = np.zeros(int(0.8 * common.SR), np.float32)
    clean = np.concatenate([np.concatenate([audio[c["file"]], gap]) for c in picked])
    # energy ground truth on the clean signal: 32 ms frames above -50 dBFS, dilated by 3 frames (96 ms)
    n = len(clean) // 512
    en = np.array([common.rms_db(clean[i * 512:(i + 1) * 512]) for i in range(n)])
    truth = np.convolve((en > -50).astype(int), np.ones(7, int), mode="same") > 0

    conds = {}
    for name, fn in make_noisy.conditions().items():
        conds[name] = np.concatenate([np.concatenate([fn(audio[c["file"]], i, picked, audio), gap])
                                      for i, c in enumerate(picked)])
    # signals with no target speech at all: every accepted frame is a false accept
    nz = {"pink_noise_only": make_noisy.pink(len(clean), 3) * 0.05, "white_noise_only": make_noisy.white(len(clean), 4) * 0.05,
          "tv_only_two_voices": 0.5 * np.concatenate([make_noisy.babble(picked, audio, i, len(audio[c["file"]]) + len(gap))
                                                      for i, c in enumerate(picked)])}
    counter = iter(range(10**6))

    def silero(x: np.ndarray) -> np.ndarray:
        url = audio_url_name(d, f"vq{next(counter)}.f32", x.astype(np.float32))
        out = b.burst({"kind": "silero", "audio": url, "rate": 16000, "frame": 512, "seconds": 0,
                       "model": "/models/silero-vad-v5.1.2/silero_vad.onnx"})
        return _f32(out["result"]["probs_b64"])

    def rnn(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        url = audio_url_name(d, f"vq{next(counter)}.f32", to_48k(x.astype(np.float32)))
        out = b.burst({"kind": "rnnoise", "audio": url, "rate": 48000, "frame": 480, "seconds": 0})
        den48, vad = _f32(out["result"]["out_b64"]), _f32(out["result"]["vad_b64"])
        return resample_poly(den48, 1, 3).astype(np.float32), vad   # vad: one value per 10 ms

    def score(p: np.ndarray, thr: float = 0.5) -> dict:
        m = min(len(p), len(truth))
        pred, t = p[:m] > thr, truth[:m]
        tp, fp, fn = int((pred & t).sum()), int((pred & ~t).sum()), int((~pred & t).sum())
        return {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1), "false_accept_frames": fp,
                "nonspeech_frames": int((~t).sum())}

    res = {"frames": int(n), "truth_speech_frames": int(truth.sum()), "clips": len(picked), "conditions": {}}
    for name, x in conds.items():
        den, rn_vad = rnn(x)
        # fold RNNoise's own 10 ms speech probability onto the 32 ms grid (mean of the ~3 values in each frame)
        k = len(rn_vad) // n
        own = np.array([rn_vad[int(i * 3.2): int((i + 1) * 3.2)].mean() for i in range(n)]) if k else rn_vad
        res["conditions"][name] = {"silero": score(silero(x)), "rnnoise_then_silero": score(silero(den)),
                                   "rnnoise_own_vad_output": score(own)}
        out = common.WORK / "denoised"
        out.mkdir(exist_ok=True)
        common.write_wav(out / f"{name}.wav", np.clip(x, -1, 1))
        common.write_wav(out / f"{name}__rnnoise.wav", np.clip(den, -1, 1))
    for name, x in nz.items():
        den, _ = rnn(x)
        res["conditions"][name] = {"silero_frames_above_0.5_pct": float(100 * np.mean(silero(x) > 0.5)),
                                   "rnnoise_then_silero_frames_above_0.5_pct": float(100 * np.mean(silero(den) > 0.5))}
    res["clip_order_for_stt"] = [c["file"] for c in picked]
    return res


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 browser probe (needs fixtures, models, Playwright Chromium)")
        return 0

    arg = common.parse_args(argv)
    seg_s = float(arg.get("--seg-seconds", 5))
    nseg = int(arg.get("--segments", 6))
    seconds = seg_s * nseg
    only = set(str(arg.get("--only", "silero,rnnoise,kws,combo,vadquality")).split(","))
    d = Path(tempfile.mkdtemp(prefix="voice-v0-audio-", dir=common.WORK))
    try:
        return _run(arg, d, only, nseg, seg_s, seconds)
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)   # large, regenerated every run


def _run(arg: dict, d: Path, only: set, nseg: int, seg_s: float, seconds: float) -> int:
    from playwright.sync_api import sync_playwright

    x16 = bench_audio(60)
    u16 = audio_url_name(d, "bench16.f32", x16)
    u48 = audio_url_name(d, "bench48.f32", to_48k(x16))
    # keywords file for the browser KWS (same tokens as the native probe)
    import probe_kws_native as kn

    kn.write_keywords(str(d / "keywords.txt"), 1.0, 0.25)
    S = specs(u16, u48, seconds)
    srv = serve(d)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    res = {"segments": nseg, "segment_seconds": seg_s, "audio": "60 s Kokoro dictation bench (synthetic), looped", "runs": {}}
    with sync_playwright() as pw:
        b = Browser(base, pw)
        res["chromium"] = b.version
        res["machine"] = {"cpu": "Intel Core Ultra 9 275HX (24 threads), WSL2, shared with other agents",
                          "note": "headless Chromium on Linux, single-thread WASM workers"}
        try:
            base16 = b.paced_cpu([S["null16"]], segments=nseg, seg_s=seg_s)
            base48 = b.paced_cpu([S["null48"]], segments=nseg, seg_s=seg_s)
            for nm, bb in (("baseline_null_worker_16k_frames", base16), ("baseline_null_worker_48k_frames", base48)):
                res["runs"][nm] = {k: bb[k] for k in ("segments_pct", "steady_pct_median")}
            plan = {"silero": ["silero5", "silero6"], "rnnoise": ["rnnoise"]}
            for group, names in plan.items():
                if group not in only:
                    continue
                for nm in names:
                    r = b.paced_cpu([S[nm]], segments=nseg, seg_s=seg_s)
                    base = base48 if S[nm]["rate"] == 48000 else base16
                    res["runs"][nm] = summarize_job(r, base)
                    print(nm, f'{res["runs"][nm]["pct_of_core_steady_minus_null_worker"]:.1f}% of a core above baseline',
                          f'({res["runs"][nm]["pct_of_core_steady_median"]:.1f}% total)', file=sys.stderr)
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
                r = b.paced_cpu([S["rnnoise"], S["silero5"]], segments=nseg, seg_s=seg_s)
                res["runs"]["combo_rnnoise_plus_silero_concurrent"] = {
                    "pct_of_core_steady_median": r["steady_pct_median"], "segments_pct": r["segments_pct"],
                    "steady_minus_two_null_workers": r["steady_pct_median"] - (base48["steady_pct_median"] + base16["steady_pct_median"]),
                    "late_frames": [sum(c[k]["late_frames"] for c in r["cadence"]) for k in range(2)],
                    "worker_frame_ms_p95": [float(np.percentile(o["result"]["frame_ms"], 95)) for o in r["outs"]]}
            if "vadquality" in only:
                res["vad_quality"] = vad_quality(b, d, int(arg.get("--vq-clips", 36)))
        finally:
            b.close()
            srv.shutdown()
    if "kws" in only:
        res["kws_sherpa_wasm_under_node"] = kws_under_node(d, bool(arg.get("--allow-no-netns")))
    common.emit("browser_cpu" if only - {"vadquality"} else "browser_vadquality", res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
