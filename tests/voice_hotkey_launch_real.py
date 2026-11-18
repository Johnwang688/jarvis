"""Opt-in Windows SAPI -> both hotkeys -> packetized capture -> local STT -> actual intent/Flow.

From WSL run the voice-probe Python with this script and --run. It uses the
configured Windows desktop bridge venv by default. For a different installation,
export JARVIS_WINDOWS_PYTHON to the WSL path of its Windows python.exe first.
The test has no physical input device, window focus, chat or typing/launch side effects.
"""
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
CASES = (
    ("Right Ctrl", "Open Google Chrome for me.", "David", "open"),
    ("Ctrl+D", "Open Google Chrome for me.", "Zira", "open"),
    ("Right Ctrl", "When I open Google Chrome it restores my tabs.", "David", "ordinary"),
    ("Ctrl+D", "Type the words open Google Chrome for me.", "Zira", "literal"),
)


def capture(keys, pcm):
    from windows import voice_agent as agent, voicewire as wire
    import numpy as np

    obj = object.__new__(agent.Agent)
    obj.capture_lock, obj.generation_lock = threading.Lock(), threading.Lock()
    obj.online, obj.recording = threading.Event(), threading.Event()
    obj.online.set()
    obj.capture = obj.upload = obj.client = None
    obj.tapped, obj.packet_ready = False, True
    obj.generation, obj.frames, obj.audio_bad, obj.chunks = 0, 0, False, []
    obj.player, obj.overlay, obj.clips = Mock(), Mock(), queue.Queue()
    step = 0
    held = {agent.VK_CONTROL, agent.VK_RCONTROL} if keys == "Right Ctrl" else {agent.VK_CONTROL, agent.VK_LCONTROL, agent.VK_D}
    obj.user32 = SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if step < 2 and vk in held else 0)
    obj.desktop_key = SimpleNamespace(hud=False, poll=lambda: keys == "Ctrl+D" and step == 0, close=lambda: None)
    def tick(_):
        nonlocal step
        step += 1
    obj.done = SimpleNamespace(is_set=lambda: step >= 3, wait=tick)
    def fabricated_tap(callback):
        if callback is not None:
            for i in range(0, len(pcm), wire.MIC_PACKET_BYTES):
                samples = np.frombuffer(pcm[i:i + wire.MIC_PACKET_BYTES], dtype="<i2")
                callback(samples, len(samples), None, None)
        return True
    obj.wake = SimpleNamespace(tap=fabricated_tap)
    with patch.dict(sys.modules, {"sounddevice": SimpleNamespace()}), patch.object(
            agent.time, "monotonic_ns", lambda: step * 400_000_000):
        obj.poll()
    _, item, source = obj.clips.get_nowait()
    assert obj.clips.empty() and source == "hotkey"
    packets = list(item.packets())
    assert not item.cancelled and b"".join(packets) == pcm and len(packets) > 1
    return {"source": source, "packets": len(packets), "wav": base64.b64encode(wire.wav_bytes(pcm)).decode("ascii")}


def windows_worker():
    from voice_anywhere_synthetic_real import SYNTH
    from windows.voiceactions import Actions, exact_start_app
    phrases = [{"text": text, "voice": voice, "rate": 0, "volume": 95} for _, text, voice, _ in CASES]
    code = base64.b64encode(SYNTH.encode("utf-16le")).decode("ascii")
    samples = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", code],
                             capture_output=True, text=True, check=True, timeout=45,
                             env=dict(os.environ, JARVIS_TEST_CASES=json.dumps(phrases)),
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout.splitlines()
    index = Actions(desktop=object()).apps
    chrome = exact_start_app("Google Chrome", index, reject_alias_conflicts=True)
    assert chrome and chrome["name"] == "Google Chrome"
    results = [capture(case[0], base64.b64decode(sample, validate=True))
               for case, sample in zip(CASES, samples, strict=True)]
    print(json.dumps({"samples": results, "chrome": chrome}))


def check():
    import stt_compare_probe as probe
    from jarvis.v2 import voice_intent
    from windows.voiceactions import Actions
    from windows.voiceflow import Flow

    native = os.environ.get("JARVIS_WINDOWS_PYTHON")
    if not native:
        from jarvis import config
        bridge = subprocess.check_output(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR], text=True).strip()
        native = str(Path(bridge) / "venv" / "Scripts" / "python.exe")
    script = subprocess.check_output(["wslpath", "-w", str(HERE)], text=True).strip()
    job = subprocess.run([native, "-B", script, "--windows-worker"], capture_output=True, text=True, timeout=60)
    if job.returncode:
        raise RuntimeError(job.stderr[-1000:])
    got = json.loads(job.stdout)
    app = got["chrome"]
    index = SimpleNamespace(returncode=0, stdout=json.dumps([{"Name": app["name"], "AppID": app["id"]}]))
    engine = probe.load_stt_local(probe.find_model_dir(None)).Engine(threads=2, schedule=False)
    failures = 0
    try:
        for (alias, _, _, expected), sample in zip(CASES, got["samples"], strict=True):
            wav = base64.b64decode(sample["wav"])
            words = engine.transcribe(wav, "audio/wav", timeout=45)
            source = sample["source"]
            launches, steps = [], []
            actions = Actions(desktop=object(), runner=lambda *a, **k: index,
                              launcher=lambda argv, **kw: launches.append(argv))
            client = SimpleNamespace(route=voice_intent.route)
            typer = SimpleNamespace(run=steps.extend, category=lambda: "other")
            Flow(client, typer, actions, Mock(), None).on_clip(wav, source, transcript=words)
            command = ["explorer.exe", "shell:AppsFolder\\" + app["id"]]
            typed = " ".join(row[1] for row in steps if row[0] == "text")
            if expected == "open":
                ok = launches == [command] and not steps
            else:
                content = " ".join(typed.casefold().split()).strip(" .,!?:")
                ok = not launches and bool(steps) and all(row[0] == "text" for row in steps)
                if expected == "literal":
                    ok = ok and content == "open google chrome for me"
                else:
                    ok = ok and content == words.casefold().strip(" .,!?:") and "my tabs" in content
            failures += int(not ok)
            print(json.dumps({"ok": ok, "hotkey": alias, "source": source, "packets": sample["packets"],
                              "decoded": words, "launches": len(launches), "typed": typed}, ensure_ascii=True), flush=True)
    finally:
        engine.unload()
    return int(bool(failures))


if __name__ == "__main__":
    if sys.argv[1:] == ["--windows-worker"]:
        windows_worker()
    elif sys.argv[1:] == ["--run"]:
        raise SystemExit(check())
    else:
        print("skipped: use --run on the paired WSL/Windows test machine")
