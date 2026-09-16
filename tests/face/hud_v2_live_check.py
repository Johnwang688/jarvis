"""The smoke subset of `hud_v2_check`, against the **real** v2 daemon.

Manual. `hud_v2_check.py` proves the window against a mock of
`docs/hud-api.md`; this proves the mock was not wrong — that the daemon's
answers have the shapes the window reads, which is the only question a mock
can never settle. It asserts nothing about a task or a provider: it starts no
work, spends no money, and sends no turn.

It SKIPS rather than fails when nothing is listening on FACE_PORT, so it can
sit in a sweep before WP12a has merged.

Run (with `jarvis daemon2` already up):
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python \\
        tests/face/hud_v2_live_check.py
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PORT = 8402
BASE = f"http://127.0.0.1:{PORT}"

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = ""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return r.status, json.loads(r.read() or b"null")


def main() -> int:
    if not listening():
        print(f"SKIPPED — nothing is serving {BASE}. Start `jarvis daemon2` first.")
        return 0

    print("\nthe daemon serves the built window")
    try:
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            html = r.read().decode("utf-8", "replace")
        check("GET / serves the HUD", r.status == 200 and "<div id=\"root\">" in html)
        # The title never moves, whatever the avatar: windows/uiatree.py's
        # FORBIDDEN_TITLES matches on it.
        check("the title is still J.A.R.V.I.S.", "J.A.R.V.I.S." in html)
    except urllib.error.URLError as e:
        check("GET / serves the HUD", False, str(e))
        return 1

    print("\nthe shapes the window reads")
    for path, test, why in [
        ("/projects", lambda b: isinstance(b, list), "a list of projects"),
        ("/threads", lambda b: isinstance(b, list), "a list of threads"),
        ("/tasks", lambda b: isinstance(b, list), "a list of tasks"),
        ("/approvals", lambda b: isinstance(b, list), "a list of pending approvals"),
        ("/usage", lambda b: isinstance(b, dict) and "providers" in b, "providers keyed by name"),
        ("/schedules", lambda b: isinstance(b, list), "a list of schedules"),
        ("/route", lambda b: isinstance(b, dict) and "decisions" in b, "the route view"),
    ]:
        try:
            status, body = get(path)
            check(f"GET {path} returns {why}", status == 200 and test(body),
                  f"{status} {str(body)[:120]}")
        except Exception as e:  # noqa: BLE001 - a live probe reports, never raises
            check(f"GET {path} returns {why}", False, str(e))

    # `quota` is filled only from a provider's own report and is never computed;
    # a `{}` where the window expects `null` would render as an empty quota
    # rather than as "not reported".
    try:
        _, usage = get("/usage")
        providers = (usage or {}).get("providers", {})
        ok = all(p.get("quota") is None or "windows" in (p.get("quota") or {})
                 for p in providers.values())
        check("every quota is null or carries windows", ok, str(providers)[:200])
        ok = all(isinstance(p.get("today", {}).get("work_tokens"), (int, float))
                 for p in providers.values())
        check("every provider reports today's work tokens", ok)
    except Exception as e:  # noqa: BLE001
        check("usage shapes", False, str(e))

    # A project the HUD can badge, and the file scope the File tab walks.
    try:
        _, projects = get("/projects")
        if projects:
            pid = projects[0]["id"]
            status, plat = get(f"/projects/{pid}/platform")
            check("platform names wsl or windows",
                  status == 200 and plat.get("platform") in ("wsl", "windows"), str(plat))
            status, tree = get(f"/projects/{pid}/tree?path=&depth=1")
            check("the tree returns entries",
                  status == 200 and isinstance(tree.get("entries"), list), str(tree)[:120])
            names = {e["name"] for e in tree.get("entries", [])}
            check("the skip list is applied", not ({".git", "node_modules", ".venv"} & names),
                  str(sorted(names))[:160])
        else:
            print("  note there are no projects yet; file and platform shapes not probed")
    except Exception as e:  # noqa: BLE001
        check("project shapes", False, str(e))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} live checks FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {CHECKS} live HUD checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
