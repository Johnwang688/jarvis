"""Free synthetic checks for the avatar system — no API calls, no browser.

What it pins down, in the order it matters:

  1. **The SVG sanitizer.** An avatar SVG is drawn inside `jarvis.html`, the
     window that gates approvals, and `write_file` can reach `avatars/`. So
     the file is untrusted input: script, event handlers, external
     references, entities and unknown elements all have to come out, and the
     geometry has to survive.
  2. **Wake phrases stay anchored.** A wake hit cancels the turn in flight and
     starts recording — an unanchored pattern takes the owner's words
     mid-sentence. Every generated regex is `\\b`-anchored at both ends, and
     a raw one that is not gets anchored on the way through.
  3. **The default is exactly today's behaviour.** The built-in avatar's
     regexes are duplicated in `jarvis.html` as its no-config fallback; this
     asserts the two copies have not drifted. It also asserts where "big
     yahoo" lives now: on the `bibi` avatar, and *not* on the default.
  4. **Nothing can leave the HUD without a wake word or a name.** A missing
     avatar, a broken avatar, an unparseable SVG and an avatar with no stated
     wake phrase all degrade to something summonable.
  5. **The identity rename** reaches the system prompt without renaming the
     shell commands inside it.

Run:  .venv/bin/python tests/avatars_check.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config  # noqa: E402

# Every avatar path is redirected before the module loads anything, so this
# never reads or writes the owner's real avatars or their saved choice.
_TMP = tempfile.TemporaryDirectory()
config.AVATARS_DIR = Path(_TMP.name) / "avatars"
config.AVATAR_STATE_PATH = Path(_TMP.name) / "state" / "avatar.json"
config.AVATAR_ENV = ""
config.AVATARS_DIR.mkdir(parents=True)

from jarvis import avatars  # noqa: E402
from jarvis.face.server import STATIC_DIR  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok  " if ok else "FAIL") + "  " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make(slug: str, meta: dict, svg: str | None = None) -> Path:
    path = config.AVATARS_DIR / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "avatar.json").write_text(json.dumps(meta), encoding="utf-8")
    if svg is not None:
        (path / "avatar.svg").write_text(svg, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 1. the sanitizer
# --------------------------------------------------------------------------


def check_sanitizer() -> None:
    print("\n--- svg sanitizer ---")

    hostile = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"
                      onload="fetch('/approve',{method:'POST'})">
      <script>fetch('/approve', {method: 'POST'})</script>
      <foreignObject><body xmlns="http://www.w3.org/1999/xhtml">
        <button onclick="alert(1)">AUTHORIZE</button></body></foreignObject>
      <a href="javascript:alert(1)"><circle cx="5" cy="5" r="4"/></a>
      <image href="http://evil.example/beacon.png"/>
      <use href="http://evil.example/x.svg#y"/>
      <path d="M0 0 L10 10" stroke="#7dd3fc" onmouseover="alert(1)"/>
    </svg>"""
    out = avatars.sanitize_svg(hostile)
    low = out.lower()
    check("script element stripped", "<script" not in low and "fetch(" not in low)
    check("onload/onclick/onmouseover stripped", "onload" not in low and "onmouse" not in low)
    check("foreignObject stripped", "foreignobject" not in low and "authorize" not in low)
    check("javascript: href stripped", "javascript:" not in low)
    check("external <image> stripped", "<image" not in low and "evil.example" not in low)
    check("the geometry survives", 'd="M0 0 L10 10"' in out and 'stroke="#7dd3fc"' in out)
    # <a> is unwrapped rather than dropped — editors wrap shapes in links and
    # losing the drawing would be a silently blank avatar.
    check("art inside a stripped <a> survives the unwrap", "<circle" in out)

    entity = """<!DOCTYPE svg [<!ENTITY x "boom">]>
                <svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>"""
    try:
        avatars.sanitize_svg(entity)
        check("doctype/entity refused", False, "accepted")
    except avatars.SvgRejected:
        check("doctype/entity refused", True)

    for label, bad in (
        ("non-svg root refused", "<html><body>hi</body></html>"),
        ("unparseable input refused", "<svg><circle"),
        ("oversize input refused", '<svg viewBox="0 0 1 1">' + " " * (600 * 1024) + "</svg>"),
    ):
        try:
            avatars.sanitize_svg(bad)
            check(label, False, "accepted")
        except avatars.SvgRejected:
            check(label, True)

    # url(...) in a style attribute is the CSS route back out to the network.
    styled = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
              '<rect style="fill:url(http://evil.example/x)" width="4" height="4"/>'
              '<rect style="fill:#7dd3fc" width="2" height="2"/></svg>')
    out = avatars.sanitize_svg(styled)
    check("style with url() dropped", "evil.example" not in out)
    check("plain style kept", "fill:#7dd3fc" in out)

    # The viewBox is what lets any avatar scale to the orb; width/height on the
    # root would fight the HUD's CSS.
    sized = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle r="4"/></svg>'
    out = avatars.sanitize_svg(sized)
    check("width/height dropped for the viewBox", 'width="64"' not in out and "viewBox=" in out)
    check("viewBox derived from width/height", 'viewBox="0 0 64 64"' in out, out)

    # Real-world shape: a namespaced file from an editor must round-trip.
    for name, (_desc, _accent, svg) in _templates().items():
        try:
            art = avatars.sanitize_svg(svg)
            ok = art.count("<") > 4 and "viewBox" in art
        except avatars.SvgRejected as exc:
            ok, art = False, str(exc)
        check(f"starter template {name!r} survives sanitizing", ok, art[:60])


def _templates():
    from jarvis import avatar_templates

    return avatar_templates.TEMPLATES


# --------------------------------------------------------------------------
# 2 & 3. wake phrases
# --------------------------------------------------------------------------


def check_wake() -> None:
    print("\n--- wake phrases ---")

    av = avatars.Avatar(slug="vex", name="Vex", wake=["vex", "hey vex"])
    pats = avatars.wake_patterns(av)
    check("every generated pattern is anchored both ends",
          all(p.startswith(r"\b") and p.endswith(r"\b") for p in pats), str(pats))
    check("the phrase fires", avatars.matches_wake("hey vex what time is it", av))
    check("a substring does not fire", not avatars.matches_wake("vexing problem", av))
    check("an unrelated word does not fire", not avatars.matches_wake("convex hull", av))
    check("multi-word tolerates recognizer spacing",
          avatars.matches_wake("heyvex, mute yourself", av))

    raw = avatars.Avatar(slug="r", name="R", wake=[], wake_regex=["ro?ver"])
    pats = avatars.wake_patterns(raw)
    check("an unanchored raw regex is anchored on the way through",
          pats == [r"\bro?ver\b"], str(pats))
    check("the anchoring is real", not avatars.matches_wake("groverdale", raw))

    broken = avatars.Avatar(slug="b", name="Bee", wake=["bee"], wake_regex=["(unclosed"])
    check("a regex that will not compile is dropped, not raised",
          avatars.wake_patterns(broken) == [r"\bbee\b"])

    # 3. the HUD's hard-coded fallback must be the same patterns.
    page = (STATIC_DIR / "jarvis.html").read_text(encoding="utf-8")
    block = re.search(r"const WAKE_PATTERNS = \[(.*?)\];", page, re.S)
    in_js = re.findall(r"/(.*?)/i,", block.group(1)) if block else []
    check("jarvis.html's fallback matches avatars.DEFAULT",
          in_js == avatars.DEFAULT.wake_regex, f"{in_js} vs {avatars.DEFAULT.wake_regex}")

    # And the default answers to its own name and nothing else.
    for phrase in ("jarvis", "hey jarvis what time is it"):
        check(f"default still wakes on {phrase!r}", avatars.matches_wake(phrase, avatars.DEFAULT))
    for phrase in ("yahoo finance is down", "jarvisson called", "we rented a big yacht"):
        check(f"default still silent on {phrase!r}",
              not avatars.matches_wake(phrase, avatars.DEFAULT))

    # "big yahoo" moved to the bibi avatar (2026-08-06). The default must not
    # keep answering to a phrase that now belongs to someone else — two avatars
    # sharing a wake phrase means the owner cannot tell which one they summoned.
    for phrase in ("big yahoo", "big ya hoo", "big yahu"):
        check(f"default no longer wakes on {phrase!r}",
              not avatars.matches_wake(phrase, avatars.DEFAULT))

    # The real avatar's phrases, kept in step with avatars/bibi/avatar.json.
    bibi = avatars.Avatar(
        slug="bibi", name="Bibi", wake=["big yahu", "netanyahu", "bibi"],
        wake_regex=[
            r"\bbig\s*y(?:a|ah|oo)+[\s-]*(?:hoo+|whoo+|who|hu)\b",
            r"\b(?:benjamin[\s-]*)?n[ae]th?[ae]n[\s-]*y(?:a|ah|oo)+"
            r"[\s-]*(?:hoo+|whoo+|who|hu|hou|ho)\b",
        ],
    )
    # The name fires whole or on its own, in the spellings the recognizer
    # hands back for it — same tolerance rule as "big yahu".
    for phrase in ("big yahu", "big yahoo", "big ya hoo", "big yoohoo", "bibi",
                   "netanyahu", "benjamin netanyahu", "netanyahoo", "netan yahoo",
                   "nathan yahoo", "netanyaho", "hey netanyahu what time is it"):
        check(f"bibi wakes on {phrase!r}", avatars.matches_wake(phrase, bibi))
    # "benjamin" alone is deliberately NOT a phrase — it is a common name, and
    # a wake hit takes the owner's words mid-sentence.
    for phrase in ("yahoo finance is down", "we rented a big yacht", "jarvis",
                   "benjamin", "benjamin franklin", "nathan is on the call",
                   "nathan you should call him", "netanya beach"):
        check(f"bibi silent on {phrase!r}", not avatars.matches_wake(phrase, bibi))

    # The avatar shipped as a template has to be the one the checks above
    # describe — a template whose json drifts is a wake phrase nobody owns.
    check("the bibi template is checked in", "bibi" in _templates())


# --------------------------------------------------------------------------
# 4. the store degrades safely
# --------------------------------------------------------------------------


def check_store() -> None:
    print("\n--- the store ---")

    make("vex", {"name": "Vex", "wake": ["vex"], "accent": "#f59e0b"},
         '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4"/></svg>')
    make("mute-face", {"name": "Quiet"})  # no wake phrase, no svg
    make("broken", {"name": "Broken"}, "<svg><this is not xml")

    slugs = [a.slug for a in avatars.available()]
    check("the built-in default is always listed first", slugs[0] == "jarvis", str(slugs))
    check("on-disk avatars are listed", {"vex", "mute-face", "broken"} <= set(slugs), str(slugs))

    check("active() defaults to jarvis", avatars.active().slug == "jarvis")
    avatars.set_active("vex")
    check("set_active persists", json.loads(config.AVATAR_STATE_PATH.read_text())["slug"] == "vex")
    check("active() reads it back", avatars.active().name == "Vex")

    check("an unknown slug raises rather than half-switching",
          _raises(lambda: avatars.set_active("nope"), LookupError))
    check("active() survives a slug that vanished",
          _with_state("deleted", lambda: avatars.active().slug) == "jarvis")
    check("a path-traversal slug resolves to nothing", avatars.load("../../etc") is None)

    quiet = avatars.load("mute-face")
    check("an avatar with no stated wake phrase is still summonable",
          avatars.matches_wake("quiet", quiet), str(avatars.wake_patterns(quiet)))

    check("an unparseable svg reads as no face, not an exception",
          avatars.svg(avatars.load("broken")) is None)
    check("a good svg comes back sanitized", "<circle" in (avatars.svg(avatars.load("vex")) or ""))
    check("describe() reports the face and the accent",
          avatars.load("vex").describe()["has_svg"] is True
          and avatars.load("vex").describe()["accent"] == "245,158,11")

    changed: list[str] = []
    avatars.on_change(lambda av: changed.append(av.slug))
    avatars.set_active("mute-face")
    check("on_change fires so open windows redraw", changed == ["mute-face"], str(changed))

    config.AVATAR_ENV = "vex"
    check("JARVIS_AVATAR (`jarvis face -a`) pins one process without "
          "disturbing the saved choice",
          avatars.active().slug == "vex"
          and json.loads(config.AVATAR_STATE_PATH.read_text())["slug"] == "mute-face")
    # Otherwise the picker would save the new avatar, redraw the window, and
    # go on serving the pinned one — window and server disagreeing about who
    # he is, which is exactly the confabulation-fuel CLAUDE.md warns about.
    avatars.set_active("vex")  # sets the pointer; must also drop the pin
    avatars.set_active("mute-face")
    check("an explicit switch beats the pin rather than being shadowed by it",
          avatars.active().slug == "mute-face" and config.AVATAR_ENV == "")

    # How a window *boots*. The saved pointer is writable by Jarvis himself
    # (`set_avatar`), so one "become the fox" used to rename the desk window
    # for every launch afterwards. A bare `jarvis face` now starts as the
    # built-in and the saved slug is left exactly where it was.
    avatars.set_active("vex")
    config.AVATAR_ENV = ""
    saved = lambda: json.loads(config.AVATAR_STATE_PATH.read_text())["slug"]  # noqa: E731
    check("a bare `jarvis face` boots as the default, whatever was saved last",
          avatars.pin_for_window().slug == "jarvis"
          and avatars.active().slug == "jarvis"
          and saved() == "vex", saved())
    check("-a still names the avatar for that window only",
          avatars.pin_for_window("vex").slug == "vex"
          and avatars.active().slug == "vex" and saved() == "vex")

    config.AVATAR_ENV = "mute-face"
    check("JARVIS_AVATAR is an explicit pin and is not overridden by the boot "
          "default", avatars.pin_for_window().slug == "mute-face"
          and config.AVATAR_ENV == "mute-face")

    config.AVATAR_ENV = ""
    check("an unknown -a raises rather than booting as somebody else",
          _raises(lambda: avatars.pin_for_window("nope"), LookupError)
          and config.AVATAR_ENV == "")

    avatars.pin_for_window()  # boot as the default...
    avatars.set_active("vex")  # ...then switch in the window
    check("switching in the window still beats the boot pin",
          avatars.active().slug == "vex" and config.AVATAR_ENV == "")
    avatars.set_active("jarvis")


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _with_state(slug: str, fn):
    config.AVATAR_STATE_PATH.write_text(json.dumps({"slug": slug}), encoding="utf-8")
    try:
        return fn()
    finally:
        config.AVATAR_STATE_PATH.write_text(json.dumps({"slug": "vex"}), encoding="utf-8")


# --------------------------------------------------------------------------
# 5. identity in the prompt
# --------------------------------------------------------------------------


def check_identity() -> None:
    print("\n--- identity rename ---")

    base = config.SYSTEM_PROMPT
    check("the default avatar changes nothing", avatars.rename(base, avatars.DEFAULT) == base)

    vex = avatars.load("vex")
    out = avatars.rename(base, vex)
    check("'You are Jarvis' becomes the avatar's name", out.startswith("You are Vex,"), out[:40])
    check("no capital-J Jarvis survives in the body",
          "Jarvis," not in out.split("You are currently going by")[0])
    # The prompt names shell commands. Renaming `jarvis face` into `vex face`
    # would send the owner to a command that does not exist, so the rename is
    # case-sensitive and only touches the capitalised name.
    check("the shell command in the prompt is NOT renamed", "jarvis chat -c" in out)
    check("lowercase invocations are left alone",
          avatars.rename("Jarvis says: run `jarvis face -a fox`.", vex)
          .startswith("Vex says: run `jarvis face -a fox`."))
    check("the block names the avatar", "going by **Vex**" in out)

    # An avatar that kept "jarvis" as a wake phrase should not be told the
    # owner might "slip" and use it.
    both = avatars.Avatar(slug="b", name="Vex", wake=["vex", "jarvis"])
    check("no stale-name note when the old name is still a wake phrase",
          "may still slip" not in avatars.rename(base, both))
    check("the note is there when it is not", "may still slip" in out)

    # The rename has to reach every surface, which it does by living in
    # Agent._refresh_system rather than in any one server.
    agent_src = (Path(__file__).resolve().parents[1] / "jarvis" / "agent.py").read_text()
    check("Agent._refresh_system applies it (so it survives compaction)",
          "avatars.rename(self._base_system)" in agent_src)


def main() -> int:
    check_sanitizer()
    check_wake()
    check_store()
    check_identity()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all avatar checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _TMP.cleanup()
