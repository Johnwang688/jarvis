"""Mutation evidence for push to talk from the phone (P-7).

Each mutant weakens one boundary, in a temp copy of the package (Python) or
of the phone's sources (TypeScript), and runs the check that guards it:
tests/v2/remote_stt_check.py for the route, `hud/src/mobile/talk.test.ts`
for the app. Every mutant must make its check fail; one that survives is a
boundary no test guards. Free: fakes only, temp config, ephemeral ports.

    python tests/v2/remote_stt_mutants.py [index …]
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PY_CHECK = ["tests/v2/remote_stt_check.py"]
TS_CHECK = ["npx", "vitest", "run", "src/mobile/talk.test.ts"]

STT = "jarvis/v2/remote_stt.py"
LISTENER = "jarvis/v2/remote_listener.py"
AUTH = "jarvis/v2/remote_auth.py"
HUD = "jarvis/v2/hud_api.py"
TALK = "hud/src/mobile/talk.ts"
APP = "hud/src/mobile/app.ts"

MUTANTS = [
    # -- the route ------------------------------------------------------------------------
    ("the clip route reached before the session check", "py", AUTH,
     '        status, session = owner_status(handler)\n        if status != 200:\n',
     '        from . import remote_stt as _early\n'
     '        if path == _early.PATH:\n'
     '            return _early.route(handler, path, {"token_sha256": ""}, body)\n'
     '        status, session = owner_status(handler)\n        if status != 200:\n'),
    ("the clip route taken out of api_route", "py", AUTH,
     '        heard = remote_stt.route(handler, path, session, body)\n',
     '        heard = None\n'),
    ("any Content-Type taken", "py", STT,
     '    if _header(handler, "Content-Type").split(";", 1)[0].strip().lower() != MIME:\n',
     '    if False:\n'),
    ("any WAV shape taken (stereo, 44.1 kHz, 8-bit)", "py", STT,
     '    if shape != (1, 2, RATE_HZ, "NONE"):\n', '    if False:\n'),
    ("no length limit on the clip", "py", STT,
     '    if frames > RATE_HZ * MAX_S:\n', '    if False:\n'),
    ("no rate per session", "py", STT,
     '            if len(seen) >= RATE:\n', '            if False:\n'),
    ("two clips at once from one session", "py", STT,
     '            if digest in self._busy:\n', '            if False:\n'),
    ("the large body for everyone (no owner test)", "py", LISTENER,
     '    if handler.command == "POST" and path == remote_stt.PATH and remote_auth.owner_status(handler)[0] == 200:\n',
     '    if handler.command == "POST" and path == remote_stt.PATH:\n'),
    ("the large body on every path", "py", LISTENER,
     '    if handler.command == "POST" and path == remote_stt.PATH and remote_auth.owner_status(handler)[0] == 200:\n',
     '    if remote_auth.owner_status(handler)[0] == 200:\n'),
    ("the desk's local preference dropped (cloud chain only)", "py", HUD,
     '                                      prefer_local=True)\n', '                                      prefer_local=False)\n'),
    ("speaker verification skipped on the phone", "py", STT,
     '        _kind, got = dictate(daemon, data, MIME, budget=BUDGET_S)\n',
     '        from jarvis import voice as _voice\n'
     '        _kind, got = "matched", _voice.stt_result(data, mime=MIME, prefer_local=True)\n'),
    ("the reply says more than the text", "py", STT,
     '    return 200, {"text": text}, []\n', '    return 200, {"text": text, "backend": "local"}, []\n'),
    ("the words delivered to the chat as well", "py", STT,
     '    LOG.info("phone: a dictation clip transcribed")\n',
     '    LOG.info("phone: a dictation clip transcribed")\n'
     '    for _t in daemon.stores.threads.list():\n'
     '        try:\n'
     '            daemon.deliver(_t.id, __import__("jarvis.v2.remote_write", fromlist=["x"]).phone_message(\n'
     '                daemon.stores.projects.get(_t.project_id), text or "x"))\n'
     '        except Exception:\n'
     '            pass\n'),
    ("the words logged", "py", STT,
     '    LOG.info("phone: a dictation clip transcribed")\n',
     '    LOG.info("phone: a dictation clip transcribed: %s", text)\n'),
    ("the clip kept in a temp file", "py", STT,
     '        check_wav(body)\n',
     '        check_wav(body)\n'
     '        import tempfile as _tf\n'
     '        _tf.NamedTemporaryFile(delete=False, suffix=".wav", dir=__import__("os").environ["HOME"]).write(body)\n'),
    # -- the app --------------------------------------------------------------------------
    ("tracks left running after the recording", "ts", TALK,
     '    if (rec.stream) stopTracks(rec.stream);\n', ''),
    ("a late permission's stream left running", "ts", TALK,
     '      // The finger left before the permission came back: nothing records.\n      stopTracks(stream);\n',
     '      // The finger left before the permission came back: nothing records.\n'),
    ("the microphone asked for on drawing the box", "ts", TALK,
     '  view(threadId: string): HTMLElement {\n',
     '  view(threadId: string): HTMLElement {\n'
     '    void this.env.media()?.getUserMedia({ audio: true }).catch(() => {});\n'),
    ("the words sent instead of only inserted", "ts", APP,
     '    this.renderComposer(true);\n  }\n\n  /** A 401 from the clip upload',
     '    this.renderComposer(true);\n    void this.sendMessage(threadId, joined);\n  }\n\n  /** A 401 from the clip upload'),
    ("sliding off no longer cancels", "ts", TALK,
     '    const move = (e: Event) => { if (!inside(e as MouseEvent)) off(); };\n',
     '    const move = (_e: Event) => {};\n'),
    ("a mic on the Inbox", "ts", APP,
     '      view = inboxView(this.inbox, (row) => {\n',
     '      view = el("section");\n      view.append(this.talk.view("x"));\n      const _unused = inboxView(this.inbox, (row) => {\n'),
    ("a clip under the minimum sent", "ts", TALK,
     '    if (ms < MIN_MS) {\n', '    if (ms < 0) {\n'),
]


def _copy(tmp: Path) -> Path:
    copy = tmp / "repo"
    shutil.copytree(ROOT / "jarvis", copy / "jarvis", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "hud").mkdir()
    for name in ("src", "package.json", "tsconfig.json", "vite.config.ts", "index.html", "mobile.html"):
        source = ROOT / "hud" / name
        if source.is_dir():
            shutil.copytree(source, copy / "hud" / name)
        elif source.exists():
            shutil.copy2(source, copy / "hud" / name)
    os.symlink((ROOT / "hud" / "node_modules").resolve(), copy / "hud" / "node_modules")
    return copy


def run(mutant, python: str) -> tuple[bool, str]:
    name, kind, rel, old, new = mutant
    with tempfile.TemporaryDirectory(prefix="stt-mutant-") as tmp:
        copy = _copy(Path(tmp))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        if kind == "py":
            proc = subprocess.run([python, *PY_CHECK], cwd=copy, env=dict(os.environ, PYTHONPATH=str(copy)),
                                  capture_output=True, text=True, timeout=900)
        else:
            proc = subprocess.run(TS_CHECK, cwd=copy / "hud", capture_output=True, text=True, timeout=900)
        out = proc.stdout + proc.stderr
        if proc.returncode == 0:
            return False, "survived: the check passed"
        lines = [line.strip() for line in out.splitlines()
                 if line.strip().startswith(("AssertionError", "FAIL", "×", "→"))]
        return True, (lines[0] if lines else out.strip().splitlines()[-1])[:150]


def main(argv: list[str]) -> int:
    python = sys.executable
    picks = [int(a) for a in argv] or range(len(MUTANTS))
    survived = 0
    for i in picks:
        killed, why = run(MUTANTS[i], python)
        survived += not killed
        print(f"{'killed' if killed else 'SURVIVED':8} {i:2} {MUTANTS[i][0]}: {why}", flush=True)
    print(f"\n{'all %d mutants killed' % len(picks) if not survived else '%d survived' % survived}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
