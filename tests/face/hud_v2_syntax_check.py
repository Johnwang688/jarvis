"""Headless checks for tree-sitter colour (ED-5; editor plan
docs/plans/2026-10-09-hud-editor-plan.md §2.6 and the ED-5 package in §3).

Free and hermetic: a `MockDaemon(0)` of its own, whose world gains a few
files, and a browser context of its own that refuses the live daemon's ports
(`hud_v2_check.guard_live`) and counts every CSP violation into a list of this
section's own. Nothing here reaches 8402/8403/8405.

Colour is read back **as the window draws it** — the computed style of a
rendered token against the HUD's tokens read off `:root` — and only once the
syntax worker has answered for that file (`window.__hudSyntax`, the client's
read-only test hooks). The checks, each written to bite:

  - a C++ file: a function call is `--syn-fn`, `void` is `--syn-type` (ED-3's
    table wins over the plan's "bold"), and a type name Monarch draws as
    plain text is `--syn-type` — the proof that tree-sitter's tokens, not
    Monarch's, are what is drawn;
  - a Markdown file's ```python fence is coloured as Python: `class` bold,
    the class name a type (Monarch calls it a function), `None` a constant,
    and the paragraph's `**bold**` comes from the inline grammar;
  - a 1.2 MB file stays on Monarch and its editor says "syntax: basic (large
    file)";
  - **every grammar loads under the HUD's CSP** (ED-4's policy plus ED-5's
    `'wasm-unsafe-eval'`): a Markdown file with a fence per language makes
    the worker compile all thirteen, each `.wasm` and the runtime's come from
    `/assets/` on the HUD's origin as `application/wasm` under the one
    policy, the worker reports every grammar compiled, and no
    `securitypolicyviolation` is raised anywhere;
  - the loader refuses a URL outside its map (another origin, another path on
    this one, `data:`, `blob:`), and passes one it ships.

Run alone (after `cd hud && npm ci && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_syntax_check.py
It also runs inside `hud_v2_check.py`, after the editor-scheme section.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from jarvis.v2 import hud_api as H  # noqa: E402
from tests.face.hud_v2_editor_theme_check import SPANS, _same, _tokens, warm  # noqa: E402
from tests.face.hud_v2_layout_check import _boot, _open_file, _set_storage, _set_ws  # noqa: E402

GRAMMARS = ("bash", "cpp", "css", "html", "javascript", "json", "markdown", "markdown_inline", "python",
            "toml", "tsx", "typescript", "yaml")

CPP = """#include "vex.h"
void spin(int speed);
Motor arm;
int main() {
  spin(100);
  return 0;
}
"""

NOTES = """# Arm notes

Lift **slowly** before the match.

```python
class Arm(Base):
    def lift(self):
        return None
```
"""

# One fence per grammar the HUD ships (Markdown and its inline grammar are the
# host): opening this makes the worker compile every one of them.
FENCES = "# Every grammar\n\nSome *inline* text.\n\n" + "".join(
    f"```{lang}\n{body}\n```\n\n" for lang, body in (
        ("cpp", 'auto s = "in-cpp";'),
        ("python", 's = "in-python"'),
        ("javascript", 'const s = "in-javascript";'),
        ("typescript", 'const s: string = "in-typescript";'),
        ("tsx", 'const e = <div title="in-tsx" />;'),
        ("json", '{"k": "in-json"}'),
        ("bash", 'echo "in-bash"'),
        ("css", 'a { content: "in-css"; }'),
        ("html", '<p title="in-html">x</p>'),
        ("yaml", 'k: "in-yaml"'),
        ("toml", 'k = "in-toml"'),
    ))

# Past `BUDGETS.maxChars` (1 MiB of UTF-16), under the editor's 2 MB cap.
BIG = "".join(f"value_{i:06d} = {i}  # a line of a large generated table\n" for i in range(26_000))

FILES = {"spin.cpp": CPP, "notes.md": NOTES, "fences.md": FENCES, "big.py": BIG}


def syntax_checks(browser, mock, base, check, until, guard_live, init_script):
    """`guard_live(ctx)` refuses the live daemon's ports; CSP violations are
    watched here — the document's events and, for the syntax worker's own
    scope, the console — into this section's own list, checked at the end."""
    from tests.face.hud_v2_check import watch_csp

    print("\neditor: tree-sitter colour (ED-5)")
    assert len(BIG) > 1_150_000, len(BIG)
    for i, (name, text) in enumerate(FILES.items()):
        mock.world["tree"][""].append({"name": name, "kind": "file", "size": len(text), "mtime": 1757900040 + i})
        mock.world["files"][name] = {"content": text, "mtime": 1757900040 + i}
    violations: list[str] = []
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, permissions=["microphone"])
    guard_live(ctx)
    watch_csp(ctx, violations.append)
    ctx.add_init_script(init_script)
    wasm: list = []
    ctx.on("response", lambda r: wasm.append(r) if r.url.split("?")[0].endswith(".wasm") else None)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    workers: list = []
    # A violation inside a worker (a blocked WebAssembly compile) fires in the
    # worker's own scope, where `watch_csp`'s document listener never hears
    # it; Chromium reports it on the console, which the page and each worker
    # relay here.
    said = re.compile(r"(?i)Content Security Policy|Refused to (compile|load|create|execute|connect|instantiate)")

    def console(m):
        if said.search(m.text or ""):
            violations.append(f"console: {m.text[:200]}")

    def worker(w):
        workers.append(w.url)
        w.on("console", console)

    page.on("console", console)
    page.on("worker", worker)
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        for section in (_every_grammar_checks, _cpp_checks, _fence_checks, _large_checks, _loader_checks):
            try:
                section(page, mock, base, check, until, wasm=wasm, workers=workers)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("tree-sitter colour raised no CSP violation", not violations, "; ".join(violations[:4]))
        check("no page errors in the tree-sitter section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def syntax_section(browser, check, until, guard_live, init_script):
    """For hud_v2_check: this section on a `MockDaemon(0)` of its own."""
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        syntax_checks(browser, mock, f"http://127.0.0.1:{mock.port}", check, until, guard_live, init_script)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# helpers

STATE = """(name) => {
  const h = window.__hudSyntax;
  if (!h) return null;
  const all = h.states().filter(s => s.uri.split('?')[0].endsWith('/' + name));
  return all.length ? all[all.length - 1] : null;
}"""


def _state(page, name: str) -> dict | None:
    return page.evaluate(STATE, name)


def _open(page, mock, until, name: str) -> bool:
    """Open `name` in pane 2 of a chat | file layout, from a fresh load (a new
    syntax worker); True once Monaco holds it."""
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "cols2", "focused": 2,
                              "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}]}))
    _boot(page, mock, None, until, reload=True)
    return _open_file(page, until, 2, name)


def _answered(page, until, name: str, timeout=15) -> dict | None:
    """The file's syntax state once the worker has answered with tokens (or
    sent it back to Monarch)."""
    return until(lambda: (lambda s: s if s and (s["served"] > 0 or s["state"] == "basic") else None)(
        _state(page, name)), timeout=timeout)


def _find(lines: list, text: str, nth: int = 0) -> dict | None:
    """The nth drawn span reading `text` (a string's quotes may be its own or
    part of it: Monarch splits them off, tree-sitter's `string` holds them)."""
    hits = [s for line in lines for s in line if s["text"].strip() in (text, f'"{text}"')]
    return hits[nth] if len(hits) > nth else None


def _drawn_as(s: dict | None, tokens: dict, token: str, bold: bool | None = None, italic: bool | None = None) -> bool:
    return bool(s) and _same(s["color"], tokens[token]) \
        and (bold is None or (s["weight"] >= 600) == bold) and (italic is None or (s["style"] == "italic") == italic)


def _settle(page, until, want: list, timeout=6.0) -> list:
    """The pane's spans once every `(text, token, bold, italic, nth)` in `want`
    is drawn so (colour arrives in two steps: Monarch, then tree-sitter, and
    an injection's grammar after that), or the last read at the timeout."""
    tokens = _tokens(page)
    last: list = []

    def ready():
        nonlocal last
        last = page.evaluate(SPANS, 2)
        return all(_drawn_as(_find(last, t, n), tokens, tok, b, i) for t, tok, b, i, n in want)

    if until(ready, timeout=timeout):
        time.sleep(0.15)
        last = page.evaluate(SPANS, 2)
    return last


def _expect(check, tokens, lines, label, text, token, bold=None, italic=None, nth=0):
    s = _find(lines, text, nth)
    style = ("bold " if bold else "") + ("italic " if italic else "") + ("not bold " if bold is False else "")
    check(f"{label}: `{text}` is {style}{token}", _drawn_as(s, tokens, token, bold, italic),
          f"drawn {s and (s['text'], s['color'], s['weight'], s['style'])}, {token} is {tokens[token]!r}")


# ---------------------------------------------------------------------------
# sections

def _every_grammar_checks(page, mock, base, check, until, wasm, workers):
    """Every grammar compiles in the syntax worker under the HUD's CSP."""
    wasm.clear()
    if not _open(page, mock, until, "fences.md"):
        check("every-grammar setup: fences.md opens in Monaco", False)
        return
    s = _answered(page, until, "fences.md")
    check("fences.md is coloured by tree-sitter (the worker answered with tokens)",
          bool(s) and s["state"] == "ready" and s["grammar"] == "markdown", str(s))
    loaded: list = []

    def all_loaded():
        nonlocal loaded
        loaded = page.evaluate("() => window.__hudSyntax ? window.__hudSyntax.loaded() : []")
        return set(loaded) >= set(GRAMMARS)

    until(all_loaded, timeout=20, step=0.25)
    check(f"the syntax worker compiled all {len(GRAMMARS)} grammars under the CSP",
          sorted(loaded) == sorted(GRAMMARS), f"missing {sorted(set(GRAMMARS) - set(loaded))}; got {loaded}")
    check("the syntax worker started from /assets under worker-src 'self'",
          any(u.startswith(base + "/assets/") and "worker" in u for u in workers), str(workers))
    names = sorted({r.url.split("?")[0].rsplit("/", 1)[-1].rsplit("-", 1)[0] for r in wasm})
    want = sorted({"web-tree-sitter", *(f"tree-sitter-{g}" for g in GRAMMARS)})
    check("the runtime's .wasm and each grammar's were fetched (one name each)", names == want,
          f"missing {sorted(set(want) - set(names))}; extra {sorted(set(names) - set(want))}")
    policy = H.content_security_policy()
    bad = []
    for r in wasm:
        headers = {h["name"].lower(): h["value"] for h in r.headers_array()}
        csp = [h["value"] for h in r.headers_array() if h["name"].lower() == "content-security-policy"]
        if not (r.url.startswith(base + "/assets/") and r.status == 200
                and headers.get("content-type", "").split(";")[0] == "application/wasm" and csp == [policy]):
            bad.append(f"{r.url} {r.status} {headers.get('content-type')} {csp}")
    check("each came from /assets on the HUD's origin, 200 application/wasm, under the one policy",
          bool(wasm) and not bad, "; ".join(bad[:3]) or "no .wasm response seen")
    check("the policy that let them compile is 'wasm-unsafe-eval' in script-src, and never 'unsafe-eval'",
          "script-src 'self' 'wasm-unsafe-eval';" in policy and " 'unsafe-eval'" not in policy, policy)
    tokens = _tokens(page)
    lines = _settle(page, until, [("in-javascript", "--syn-string", None, None, 0),
                                  ("in-toml", "--syn-string", None, None, 0)])
    hot = [(x["text"], x["color"]) for line in lines for x in line if warm(x["color"])]
    check("fences.md: no token drawn amber, orange or red", bool(lines) and not hot, str(hot[:4]))
    _expect(check, tokens, lines, "fences.md (an injected JavaScript string)", "in-javascript", "--syn-string")
    _expect(check, tokens, lines, "fences.md (an injected TOML string)", "in-toml", "--syn-string")


def _cpp_checks(page, mock, base, check, until, **_):
    if not _open(page, mock, until, "spin.cpp"):
        check("C++ setup: spin.cpp opens in Monaco", False)
        return
    s = _answered(page, until, "spin.cpp")
    check("spin.cpp is coloured by tree-sitter (C++ grammar, ready, tokens served)",
          bool(s) and s["state"] == "ready" and s["grammar"] == "cpp" and s["served"] > 0, str(s))
    tokens = _tokens(page)
    lines = _settle(page, until, [("Motor", "--syn-type", None, None, 0), ("spin", "--syn-fn", None, None, 1)])
    _expect(check, tokens, lines, "C++ function call", "spin", "--syn-fn", nth=1)
    _expect(check, tokens, lines, "C++ function declaration", "spin", "--syn-fn", nth=0)
    _expect(check, tokens, lines, "C++ builtin type (ED-3's table: no bold)", "void", "--syn-type", bold=False)
    _expect(check, tokens, lines, "C++ type name, which Monarch draws as plain text", "Motor", "--syn-type")
    _expect(check, tokens, lines, "C++ keyword", "return", "--text", bold=True)
    _expect(check, tokens, lines, "C++ include path", "vex.h", "--syn-string")
    _expect(check, tokens, lines, "C++ number", "100", "--syn-const")


def _fence_checks(page, mock, base, check, until, **_):
    if not _open(page, mock, until, "notes.md"):
        check("Markdown setup: notes.md opens in Monaco", False)
        return
    s = _answered(page, until, "notes.md")
    check("notes.md is coloured by tree-sitter (Markdown grammar)",
          bool(s) and s["state"] == "ready" and s["grammar"] == "markdown", str(s))
    tokens = _tokens(page)
    lines = _settle(page, until, [("Arm", "--syn-type", None, None, 0), ("None", "--syn-const", None, None, 0)])
    _expect(check, tokens, lines, "a Python fence in Markdown: keyword", "class", "--text", bold=True)
    _expect(check, tokens, lines, "a Python fence in Markdown: the class name (Monarch: a function)", "Arm",
            "--syn-type")
    _expect(check, tokens, lines, "a Python fence in Markdown: a method definition", "lift", "--syn-fn")
    _expect(check, tokens, lines, "a Python fence in Markdown: a constant", "None", "--syn-const")
    _expect(check, tokens, lines, "a Python fence in Markdown: self", "self", "--text", italic=True)
    _expect(check, tokens, lines, "Markdown heading", "# Arm notes", "--syn-fn", bold=True)
    _expect(check, tokens, lines, "Markdown inline", "**slowly**", "--text", bold=True)
    loaded = page.evaluate("() => window.__hudSyntax.loaded()")
    check("notes.md took three grammars: Markdown, its inline grammar and Python (the fence)",
          sorted(loaded) == ["markdown", "markdown_inline", "python"], str(loaded))


def _large_checks(page, mock, base, check, until, **_):
    if not _open(page, mock, until, "big.py"):
        check("large-file setup: big.py opens in Monaco", False)
        return
    corner = page.locator('[data-testid="pane-2"] [data-testid="syntax-status"]')
    until(lambda: corner.count() > 0 and corner.first.inner_text().strip() != "", timeout=8)
    text = corner.first.inner_text().strip() if corner.count() else ""
    check("a 1.2 MB file says \"syntax: basic (large file)\" in its editor",
          text == "syntax: basic (large file)" and corner.first.is_visible(), repr(text))
    s = _state(page, "big.py")
    check("and stays on Monarch: it never reached the worker",
          bool(s) and s["state"] == "basic" and s.get("reason") == "large file" and s["served"] == 0, str(s))
    lines = page.evaluate(SPANS, 2)
    tokens = _tokens(page)
    check("Monarch still colours it (a comment is italic --text-muted)",
          any(_drawn_as(x, tokens, "--text-muted", italic=True) for line in lines for x in line), str(lines[:1]))
    if _open(page, mock, until, "spin.cpp") and _answered(page, until, "spin.cpp"):
        until(lambda: corner.count() == 0 or not corner.first.is_visible(), timeout=3)
        check("a covered file within budget shows no status corner",
              corner.count() == 0 or not corner.first.is_visible(),
              corner.first.inner_text() if corner.count() else "")


def _loader_checks(page, mock, base, check, until, wasm, **_):
    """The worker's loader fetches only what is in its map (lib/syntax/assets.ts)."""
    s = _state(page, "spin.cpp")
    check("loader setup: a syntax worker is running", bool(s) and s["state"] == "ready", str(s))
    for what, url in (
        ("another origin", "http://127.0.0.1:9/assets/tree-sitter-cpp.wasm"),
        ("another path on this origin", f"{base}/assets/tree-sitter-evil.wasm"),
        ("a path outside /assets", f"{base}/projects/p1/file?path=x.wasm"),
        ("a data: URL", "data:application/wasm;base64,AGFzbQEAAAA="),
        ("a blob: URL", f"blob:{base}/0c0c0c0c-0000-0000-0000-000000000000"),
    ):
        r = page.evaluate("u => window.__hudSyntax.probe(u)", url)
        check(f"the loader refuses {what}", not r.get("ok") and "not a grammar the HUD ships" in (r.get("error") or ""),
              str(r))
    # A shipped one passes: the C++ grammar's URL, as the worker fetched it.
    cpp = [r.url.split("?")[0] for r in wasm if "/assets/tree-sitter-cpp-" in r.url]
    if not cpp:
        from tests.face.hud_v2_mock import DIST
        cpp = [f"{base}/assets/{p.name}" for p in sorted((DIST / "assets").glob("tree-sitter-cpp-*.wasm"))]
    r = page.evaluate("u => window.__hudSyntax.probe(u)", cpp[0]) if cpp else {}
    check("and passes a grammar it ships (the C++ grammar's own URL)", bool(r.get("ok")), f"{cpp[:1]}: {r}")


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import DIST
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        syntax_section(browser, v2.check, v2.until, lambda ctx: v2.guard_live(ctx, v2.FAILURES.append),
                       v2.FAKE_RECOGNIZER)
        browser.close()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} tree-sitter checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
