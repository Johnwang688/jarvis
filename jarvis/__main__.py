"""The `jarvis` command."""

from __future__ import annotations

import argparse
import json
import sys
import threading

from rich.console import Console
from rich.table import Table

from . import agent as agent_mod
from . import agentbench as agentbench_mod
from . import cadbench as cadbench_mod
from . import bench as bench_mod
from . import config, permissions, sessions, tools
from . import vocabbench as vocab_mod

console = Console()


def _make_reader():
    """Interactive line reader.

    prompt_toolkit handles bracketed paste — a pasted multi-line message lands
    in one buffer and submits as one turn, instead of each line becoming its
    own turn (which mangles the conversation). Falls back to plain input when
    stdin is not a terminal.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import FileHistory

        if not sys.stdin.isatty():
            raise RuntimeError("not a tty")

        session = PromptSession(
            history=FileHistory(str(config.REPO_ROOT / ".jarvis_history"))
        )
        return lambda: session.prompt(ANSI("\n\x1b[1;32myou\x1b[0m "))
    except Exception:
        return lambda: console.input("\n[bold green]you[/bold green] ")


def _approve(tool: tools.Tool, args: dict) -> bool:
    # A background task's approval lands here on the task's own thread — but
    # stdin belongs to the REPL, and two threads reading one terminal
    # interleave into nonsense. Deny with a note instead of prompting; the
    # face and daemon are the surfaces that can answer background questions.
    # (Foreground work is untouched: everything else calls this on the main
    # thread — run_subagent is synchronous and parallel dispatch never runs a
    # dangerous tool.)
    if threading.current_thread() is not threading.main_thread():
        from . import runtime

        who = f" from {runtime.origin()}" if runtime.origin() else ""
        console.print(
            f"[dim]background request{who} for {tool.name} denied — the "
            f"terminal prompt cannot be shared; use the face or daemon for "
            f"approvable background tasks[/dim]"
        )
        return False
    console.print()
    console.print(f"[yellow]{tool.name}[/yellow] wants to run:")
    for key, value in args.items():
        console.print(f"  [dim]{key}:[/dim] {value}")
    try:
        answer = console.input(
            "[bold]Allow? [y/N/a][/bold] [dim](a = always: allowlist this so it stops asking)[/dim] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in ("a", "always"):
        try:
            entry = permissions.add_allow(tool.name, args)
        except permissions.NotAllowlistable as exc:
            # Approved this once, but not turned into a standing rule — say so
            # rather than letting the owner believe they will stop being asked.
            console.print(f"[dim]  approved once; not allowlisted: {exc}[/dim]")
        else:
            console.print(f"[dim]  allowlisted: {entry} ({config.ALLOWLIST_PATH})[/dim]")
        return True
    return answer in ("y", "yes")


def _pick_session(args, surface: str) -> sessions.Session | None:
    """Which conversation this invocation continues, if any.

    Default is a fresh session, not the last one: resuming silently is how you
    end up talking into a transcript you have forgotten the contents of. The
    recent-session titles are in Jarvis's context either way, so a new session
    still knows the old ones exist.
    """
    chosen = getattr(args, "resume", None)
    if chosen:
        session = sessions.load(chosen)
        if session is None:
            raise RuntimeError(f"no session {chosen!r} — try: jarvis sessions")
        return session
    if getattr(args, "continue_", False):
        session = sessions.latest()
        if session is None:
            console.print("[dim]no earlier session to continue — starting a new one[/dim]")
        return session or sessions.new(surface)
    return sessions.new(surface)


def _make_agent(model: str | None, session: sessions.Session | None = None) -> agent_mod.Agent:
    def on_event(kind: str, data) -> None:
        if kind == "tool_start":
            name, raw = data
            console.print(f"[dim]  → {name}({raw[:120]})[/dim]")
        elif kind == "context" and data.saved > 500:
            bits = []
            if data.images_evicted:
                bits.append(f"{data.images_evicted} image(s)")
            if data.results_truncated:
                bits.append(f"{data.results_truncated} result(s)")
            if data.messages_compacted:
                bits.append(f"compacted {data.messages_compacted} msgs")
            console.print(f"[dim]  ⤷ context: freed ~{data.saved:,} tokens ({', '.join(bits)})[/dim]")
        elif kind == "text" and data:
            console.print(f"\n[bold cyan]jarvis[/bold cyan] {data}")
        elif kind == "interim_text" and data:
            # said on the way to a tool call, not the finished reply
            console.print(f"\n[cyan]jarvis[/cyan] [dim]{data}[/dim]")

    return agent_mod.Agent(
        model=model, approve=permissions.gate(_approve), on_event=on_event, session=session
    )


def cmd_chat(args) -> int:
    session = _pick_session(args, "chat")
    jarvis = _make_agent(args.model, session)
    console.print(f"[dim]model: {jarvis.model} · {len(jarvis.tool_specs)} tools · Ctrl-D to exit[/dim]")
    if session is not None:
        resumed = f" · resuming {session.turns} turn(s)" if session.turns else ""
        console.print(f"[dim]session: {session.id} {session.title!r}{resumed}[/dim]")

    read_input = _make_reader()
    session_cost = 0.0
    try:
        while True:
            try:
                user_input = read_input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print(f"\n[dim]session cost: ${session_cost:.4f}[/dim]")
                return 0
            if not user_input:
                continue
            if user_input in ("exit", "quit"):
                console.print(f"[dim]session cost: ${session_cost:.4f}[/dim]")
                return 0

            try:
                turn = jarvis.run_turn(user_input)
            except Exception as exc:
                console.print(f"[red]error:[/red] {exc}")
                continue

            session_cost += turn.cost_usd
            console.print(
                f"[dim]  {turn.steps} step(s) · {turn.latency_s:.1f}s · "
                f"${turn.cost_usd:.4f} · session ${session_cost:.4f}[/dim]"
            )
    finally:
        _stop_browser("chat")
        sessions.drain_titles()


def _stop_browser(trace_name: str) -> None:
    # A browser left running dies with an EPIPE tantrum from the Node driver
    # when the process exits underneath it. stop() is a no-op if no browser
    # tool ever ran this session.
    from .browser import SESSION

    SESSION.stop(trace_name=trace_name)


def cmd_ask(args) -> int:
    jarvis = _make_agent(args.model)
    try:
        turn = jarvis.run_turn(" ".join(args.prompt))
    finally:
        _stop_browser("ask")
    console.print(f"[dim]{turn.steps} step(s) · {turn.latency_s:.1f}s · ${turn.cost_usd:.4f}[/dim]")
    return 0


FAMILIES = {"tools": bench_mod, "vocab": vocab_mod, "agent": agentbench_mod,
            "cad": cadbench_mod}


def cmd_bench(args) -> int:
    mod = FAMILIES[getattr(args, "family", "tools")]
    roster = args.models or mod.DEFAULT_ROSTER
    tasks = mod.TASKS
    if args.task:
        tasks = [t for t in tasks if t.name in args.task]
        if not tasks:
            console.print(f"[red]no task named {args.task}[/red]")
            return 1

    console.print(f"[dim]{len(roster)} model(s) × {len(tasks)} task(s)[/dim]\n")

    summary = []
    graded: dict[str, list] = {}
    for model in roster:
        table = Table(title=model, title_style="bold cyan", header_style="dim")
        table.add_column("task")
        table.add_column("tests", style="dim")
        table.add_column("ok", justify="center")
        table.add_column("calls", style="dim")
        table.add_column("s", justify="right")
        table.add_column("$", justify="right")

        passed = cost = latency = 0.0
        results = []
        for task in tasks:
            with console.status(f"{model} · {task.name}"):
                result = mod.run_task(model, task)
            results.append(result)
            passed += result.passed
            cost += result.cost_usd
            latency += result.latency_s
            detail = result.detail or ",".join(n for n, _ in result.calls) or "—"
            if result.error:
                detail = f"{detail} [red]{result.error[:40]}[/red]"
            table.add_row(
                task.name,
                task.tests,
                "[green]✓[/green]" if result.passed else "[red]✗[/red]",
                detail[:60],
                f"{result.latency_s:.1f}",
                f"{result.cost_usd:.5f}",
            )

        console.print(table)
        # A graded family's value is in *which* checks failed, and the table
        # column is too narrow for that.
        for result in results:
            for check in result.checks:
                if not check.ok:
                    console.print(
                        f"  [dim]{result.task}[/dim] [red]✗[/red] "
                        f"[dim]{check.category}[/dim] {check.name}"
                    )
        console.print()
        graded[model] = results
        summary.append((model, int(passed), len(tasks), latency, cost))

    board = Table(title="summary", title_style="bold", header_style="dim")
    board.add_column("model")
    board.add_column("passed", justify="right")
    board.add_column("total s", justify="right")
    board.add_column("total $", justify="right")
    for model, ok, total, latency, cost in sorted(summary, key=lambda r: (-r[1], r[4])):
        color = "green" if ok == total else "yellow" if ok >= total * 0.6 else "red"
        board.add_row(model, f"[{color}]{ok}/{total}[/{color}]", f"{latency:.1f}", f"{cost:.5f}")
    console.print(board)

    # "passed" is all-or-nothing per task; a graded family also reports how
    # much of each category it earned.
    if hasattr(mod, "score_report"):
        console.print()
        mod.score_report(console, graded)
    return 0


def cmd_face(args) -> int:
    # Imported lazily so `jarvis tools` etc. don't pay for the voice stack.
    from . import avatars
    from .face import server as face_server

    # Process-scoped, like --dangerously-skip-permissions: this window runs as
    # one avatar without touching the owner's saved choice, so opening a second
    # face is not a way to silently change the first. With no -a and no
    # JARVIS_AVATAR, that avatar is the built-in default — `jarvis face` is
    # `jarvis face -a jarvis`. Switching in the HUD still works and still
    # persists; it just does not decide how the *next* window boots.
    try:
        avatars.pin_for_window(getattr(args, "avatar", "") or "")
    except LookupError:
        console.print(
            f"[red]no avatar named {args.avatar!r}[/red] — "
            + ", ".join(a.slug for a in avatars.available())
        )
        return 1

    if getattr(args, "dangerously_skip_permissions", False):
        # The ONLY way into approve-everything mode, by design. It is a
        # process variable, so a restart is always back to ask + allowlist.
        permissions.set_mode("all")
        console.print(
            "[bold red]⚠ PERMISSIONS OFF — every dangerous tool runs without "
            "asking until this process exits.[/bold red]"
        )
    session = _pick_session(args, "face")
    console.print(f"session: {session.id} {session.title!r} ({session.turns} turn(s))")
    return face_server.main(args.page, session=session)


def cmd_auth(args) -> int:
    # Human-only by design: the consent flow is a CLI subcommand, never a
    # tool, so the agent cannot initiate or widen its own access.
    if args.service == "onshape":
        from . import onshape_auth

        return onshape_auth.connect(redo=args.redo)
    if args.service == "discord":
        from .tools import discord

        return discord.connect()
    from . import google_auth

    return google_auth.connect(args.client_json)


def cmd_desktop(args) -> int:
    # Human-only, like `jarvis auth`: installing and starting the bridge is
    # how the owner hands over the desktop, so it is never a tool.
    if args.action == "setup":
        from . import desktop_setup

        return desktop_setup.setup()

    from .desktop import SESSION

    SESSION.start()
    if args.action == "status":
        if args.wait:
            console.print(f"Waiting up to {args.wait}s for the bridge to connect…")
            SESSION.wait_for_bridge(args.wait)
        console.print(SESSION.status())
        return 0 if SESSION.connected else 1
    return 0


def cmd_daemon(args) -> int:
    from . import daemon

    if args.action == "install":
        return daemon.install()
    return daemon.run()


def cmd_goal(args) -> int:
    from . import daemon, goals

    goal = goals.create(
        " ".join(args.statement),
        dollars=args.dollars,
        hours=args.hours,
        slices=args.slices,
    )
    console.print(f"goal [bold]{goal.id}[/bold] queued: {goal.statement}")
    console.print(
        f"caps: ${goal.budgets['dollars']:.2f} / {goal.budgets['hours']:g}h / "
        f"{goal.budgets['slices']} slices"
    )
    if not daemon.is_running():
        console.print(
            "[yellow]no daemon is running — the goal waits until "
            "`jarvis daemon` starts.[/yellow]"
        )
    return 0


def cmd_goals(args) -> int:
    from . import goals

    found = goals.all_goals()
    if not found:
        console.print(f"[dim]no goals yet ({config.GOALS_DIR})[/dim]")
        return 0
    for goal in found:
        reason = f" ({goal.reason})" if goal.reason else ""
        console.print(
            f"{goal.id}  [bold]{goal.status}[/bold]{reason}  "
            f"{goal.slices} slices  ${goal.spent_usd:.2f}  {goal.statement[:80]}"
        )
    return 0


def cmd_sessions(args) -> int:
    found = sessions.recent(args.limit)
    if not found:
        console.print(f"[dim]no saved sessions yet ({config.SESSIONS_DIR})[/dim]")
        return 0
    table = Table(header_style="dim")
    table.add_column("id")
    table.add_column("title")
    table.add_column("where", style="dim")
    table.add_column("last", style="dim")
    table.add_column("turns", justify="right")
    table.add_column("$", justify="right")
    for item in found:
        table.add_row(
            item.id,
            item.title[:44],
            item.meta.get("surface", ""),
            sessions.when(item.meta.get("updated", 0)),
            str(item.turns),
            f"{float(item.meta.get('cost_usd', 0.0)):.4f}",
        )
    console.print(table)
    console.print(f"\n[dim]resume one: jarvis chat -r <id>   ·   {config.SESSIONS_DIR}[/dim]")
    return 0


def cmd_avatar(args) -> int:
    """List, switch, or scaffold an avatar (name + wake phrases + face)."""
    from . import avatar_templates, avatars, voice

    here = avatars.active()

    if args.action == "new":
        if not args.slug:
            console.print("[red]usage: jarvis avatar new <slug> [--template fox][/red]")
            return 1
        slug = args.slug.lower()
        if not avatars.SLUG_RE.match(slug):
            console.print(f"[red]{slug!r} is not a usable name (a-z 0-9 - _)[/red]")
            return 1
        template = args.template or "bust"
        if template not in avatar_templates.TEMPLATES:
            console.print(
                f"[red]no template {template!r}[/red] — "
                + ", ".join(sorted(avatar_templates.TEMPLATES))
            )
            return 1
        desc, accent, svg = avatar_templates.TEMPLATES[template]
        path = config.AVATARS_DIR / slug
        if path.exists():
            console.print(f"[red]{path} already exists[/red]")
            return 1
        path.mkdir(parents=True)
        name = args.name or slug.replace("-", " ").replace("_", " ").title()
        (path / "avatar.svg").write_text(svg, encoding="utf-8")
        (path / "avatar.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "wake": [name.lower()],
                    "accent": accent,
                    "description": desc,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        console.print(f"created [bold]{path}[/bold] from the {template} template")
        console.print(
            f"[dim]edit avatar.svg / avatar.json, then: jarvis avatar {slug}[/dim]"
        )
        return 0

    if args.action:  # `jarvis avatar fox` — switch, persistently
        try:
            av = avatars.set_active(args.action)
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(
            f"avatar is now [bold]{av.name}[/bold] "
            f"(wake: {', '.join(av.wake_labels())})"
        )
        console.print("[dim]a running face picks it up on its next window load[/dim]")
        return 0

    table = Table(header_style="dim")
    table.add_column("")
    table.add_column("slug")
    table.add_column("name")
    table.add_column("wake phrases")
    table.add_column("voice", style="dim")
    table.add_column("face", style="dim")
    for av in avatars.available():
        table.add_row(
            "→" if av.slug == here.slug else "",
            av.slug,
            av.name,
            ", ".join(av.wake_labels()).lower(),
            # What he will actually speak in, not what the file asks for — a
            # voice that is not installed falls back, and the list is where
            # the owner should be able to see that.
            voice.voice_for(av),
            "svg" if av.svg_path else "built-in",
        )
    console.print(table)
    console.print(f"\n[dim]{config.AVATARS_DIR}[/dim]")
    console.print(
        "[dim]switch: jarvis avatar <slug>   ·   "
        "new: jarvis avatar new <slug> --template "
        + "|".join(sorted(avatar_templates.TEMPLATES))
        + "[/dim]"
    )
    return 0


def cmd_voice(args) -> int:
    """List, clone, mix, audition, or remove local voices (human-only).

    Creation lives here and not in a tool on purpose — the `jarvis auth`
    reasoning: cloning a voice needs the speaker's lawful consent (the
    pocket-tts license says so explicitly), so making one is the owner's
    act, never the agent's. Using a voice that already exists is the cheap
    part and stays where it was: the HUD picker and avatar.json.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from . import pocket, voice

    action = args.action or "list"
    rest = list(args.rest or [])

    if action == "list":
        created = {
            v["name"]: v["created"]
            for v in (pocket.custom_voices() if pocket.available() else [])
        }
        table = Table(header_style="dim")
        table.add_column("voice")
        table.add_column("backend", style="dim")
        table.add_column("kind", style="dim")
        table.add_column("created", style="dim")
        for entry in voice.catalog():
            bare = entry["name"].removeprefix("pocket:")
            table.add_row(
                entry["name"],
                entry["backend"],
                entry["kind"],
                created.get(bare, "") if entry["backend"] == "pocket" else "",
            )
        console.print(table)
        if pocket.available():
            console.print(f"\n[dim]custom voices: {config.VOICES_DIR}[/dim]")
        else:
            console.print(
                "\n[dim]pocket-tts is not installed, so only Kokoro voices are "
                'listed — install with: uv pip install -e ".[pocketvoice]"[/dim]'
            )
        console.print(
            "[dim]clone: jarvis voice clone <name> <audio...>   ·   "
            "mix: jarvis voice mix <name> <voice>=<w> <voice>=<w>   ·   "
            "hear one: jarvis voice say <name> [text][/dim]"
        )
        return 0

    if action == "clone":
        if len(rest) < 2:
            console.print(
                "[red]usage: jarvis voice clone <name> <audio.wav> [more.wav ...][/red]"
            )
            return 1
        name = rest[0].lower().removeprefix("pocket:")
        sources = [Path(p).expanduser() for p in rest[1:]]
        if not args.yes:
            console.print(
                "Cloning a voice requires the speaker's lawful consent "
                "(pocket-tts license). Only clone a voice you have the right to use."
            )
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                console.print("aborted")
                return 1
        try:
            path = pocket.clone(name, sources)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(f"cloned [bold]pocket:{name}[/bold] -> {path}")
        console.print(f"[dim]hear it: jarvis voice say {name}[/dim]")
        return 0

    if action == "mix":
        if len(rest) < 3:
            console.print(
                "[red]usage: jarvis voice mix <name> <voice>=<weight> "
                "<voice>=<weight> [...][/red]"
            )
            return 1
        name = rest[0].lower().removeprefix("pocket:")
        weights: dict[str, float] = {}
        for pair in rest[1:]:
            component, _, weight = pair.partition("=")
            component = component.strip().removeprefix("pocket:")
            try:
                weights[component] = float(weight)
            except ValueError:
                console.print(f"[red]{pair!r} is not <voice>=<weight>[/red]")
                return 1
        try:
            path = pocket.mix(name, weights)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(f"mixed [bold]pocket:{name}[/bold] -> {path}")
        console.print(
            "[dim]hybrid blending is experimental — it interpolates the voice "
            "states. If it sounds wrong, clone from several files instead: "
            "jarvis voice clone <name> <a.wav> <b.wav>[/dim]"
        )
        console.print(f"[dim]hear it: jarvis voice say {name}[/dim]")
        return 0

    if action == "say":
        if not rest:
            console.print("[red]usage: jarvis voice say <name> [text...][/red]")
            return 1
        name = rest[0]
        text = " ".join(rest[1:]) or "Systems online. All diagnostics green."
        # A bare Kokoro name auditions as itself; everything else is pocket's.
        if name not in voice.available_voices():
            name = "pocket:" + name.removeprefix("pocket:")
        try:
            audio = voice.tts(text, voice=name)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        suffix = ".wav" if audio.startswith(b"RIFF") else ".mp3"
        fd, out = tempfile.mkstemp(prefix="jarvis-voice-", suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        played = False
        if suffix == ".wav":  # paplay/aplay speak WAV; MP3 just gets a path
            for player in ("paplay", "aplay"):
                exe = shutil.which(player)
                if not exe:
                    continue
                try:
                    subprocess.run([exe, out], check=True, capture_output=True)
                    played = True
                    break
                except Exception:
                    continue
        console.print(f"[dim]{'played' if played else 'saved'}:[/dim] {out}")
        return 0

    if action == "rename":
        if len(rest) != 2:
            console.print("[red]usage: jarvis voice rename <old> <new>[/red]")
            return 1
        old = rest[0].lower().removeprefix("pocket:")
        new = rest[1].lower().removeprefix("pocket:")
        try:
            pocket.rename(old, new)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(f"renamed pocket:{old} -> [bold]pocket:{new}[/bold]")
        console.print(
            "[dim]anything still pointing at the old name (an avatar.json, the "
            "HUD picker) falls back audibly — reselect the new name there[/dim]"
        )
        return 0

    if action == "rm":
        if len(rest) != 1:
            console.print("[red]usage: jarvis voice rm <name>[/red]")
            return 1
        try:
            pocket.remove(rest[0].lower().removeprefix("pocket:"))
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(f"removed pocket:{rest[0]}")
        console.print(
            "[dim]a running face keeps a removed voice cached until restart[/dim]"
        )
        return 0

    console.print(
        f"[red]unknown action {action!r}[/red] — list, clone, mix, say, rename, rm"
    )
    return 1


def cmd_tools(args) -> int:
    table = Table(header_style="dim")
    table.add_column("tool")
    table.add_column("args", style="dim")
    table.add_column("description")
    for name in sorted(tools.REGISTRY):
        entry = tools.REGISTRY[name]
        params = ", ".join(entry.schema["properties"])
        label = f"[yellow]{name}[/yellow]" if entry.dangerous else name
        table.add_row(label, params, entry.description.splitlines()[0])
    console.print(table)
    console.print("\n[dim]yellow = requires your approval before running[/dim]")
    return 0


def cmd_config(args) -> int:
    for tier, model in config.TIERS.items():
        console.print(f"{tier:14} {model}")
    console.print(f"\n[dim]memory: {config.MEMORY_DIR}[/dim]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal agent.")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="interactive session (default)")
    chat.add_argument("-m", "--model", help="override the orchestrator model")
    chat.add_argument(
        "-c",
        "--continue",
        dest="continue_",
        action="store_true",
        help="continue the most recent session instead of starting a new one",
    )
    chat.add_argument("-r", "--resume", metavar="ID", help="resume a specific session id")
    chat.set_defaults(func=cmd_chat)

    ask = sub.add_parser("ask", help="run one prompt and exit")
    ask.add_argument("prompt", nargs="+")
    ask.add_argument("-m", "--model", help="override the orchestrator model")
    ask.set_defaults(func=cmd_ask)

    bench = sub.add_parser("bench", help="stress-test models on tool calling")
    bench.add_argument("models", nargs="*", help="OpenRouter model ids (default: cheap roster)")
    bench.add_argument("-t", "--task", action="append", help="run only these tasks")
    bench.add_argument(
        "--family",
        choices=["tools", "vocab", "agent", "cad"],
        default="tools",
        help=(
            "task family: canned tool-calling tasks, the real-browser vocab "
            "drill, agent-bench (whole-Jarvis, sandboxed, category-rated), or "
            "cad-bench (live Onshape — costs real API calls AND Onshape "
            "quota; builds and deletes cadbench-* assemblies in the sandbox)"
        ),
    )
    bench.set_defaults(func=cmd_bench)

    face = sub.add_parser("face", help="open the Jarvis HUD window (voice mode)")
    face.add_argument("page", nargs="?", default="jarvis.html", help="HUD page to open")
    face.add_argument(
        "-c",
        "--continue",
        dest="continue_",
        action="store_true",
        help="continue the most recent session instead of starting a new one",
    )
    face.add_argument("-r", "--resume", metavar="ID", help="resume a specific session id")
    face.add_argument(
        "-a",
        "--avatar",
        metavar="SLUG",
        help="run this window as a specific avatar (see `jarvis avatar`)",
    )
    face.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="approve every dangerous tool without asking, until this process exits",
    )
    face.set_defaults(func=cmd_face)

    auth = sub.add_parser("auth", help="connect an external account (one-time, human-only)")
    auth.add_argument(
        "service", choices=["google", "onshape", "discord"], help="which service to connect"
    )
    auth.add_argument(
        "client_json",
        nargs="?",
        help="google only: path to the OAuth client JSON (omit to show status)",
    )
    auth.add_argument(
        "--redo",
        action="store_true",
        help="onshape only: redo the setup (keys, sandbox, libraries) even if connected",
    )
    auth.set_defaults(func=cmd_auth)

    desktop = sub.add_parser(
        "desktop", help="set up or check the Windows desktop bridge (human-only)")
    desktop.add_argument("action", choices=["setup", "status"], nargs="?", default="status")
    desktop.add_argument("--wait", type=float, default=0.0,
                         help="seconds to wait for the bridge to connect")
    desktop.set_defaults(func=cmd_desktop)

    daemon = sub.add_parser(
        "daemon",
        help="headless always-on service: Discord gateway + DM approvals, no window",
    )
    daemon.add_argument("action", choices=["run", "install"], nargs="?", default="run")
    daemon.set_defaults(func=cmd_daemon)

    goal = sub.add_parser("goal", help="queue a background goal for the daemon")
    goal.add_argument("statement", nargs="+", help="what to accomplish")
    goal.add_argument("--dollars", type=float, default=None, help="spend ceiling")
    goal.add_argument("--hours", type=float, default=None, help="wall-clock ceiling")
    goal.add_argument("--slices", type=int, default=None, help="turn-slice ceiling")
    goal.set_defaults(func=cmd_goal)

    sub.add_parser("goals", help="list background goals").set_defaults(func=cmd_goals)

    saved = sub.add_parser("sessions", help="list saved conversations")
    saved.add_argument("-n", "--limit", type=int, default=20, help="how many to show")
    saved.set_defaults(func=cmd_sessions)

    avatar = sub.add_parser(
        "avatar", help="list, switch, or create an avatar (name + wake word + face)"
    )
    avatar.add_argument(
        "action", nargs="?", default="", help="a slug to switch to, or 'new'"
    )
    avatar.add_argument("slug", nargs="?", default="", help="slug for 'new'")
    avatar.add_argument("--template", help="starter art for 'new'")
    avatar.add_argument("--name", help="display name for 'new' (default: the slug)")
    avatar.set_defaults(func=cmd_avatar)

    voicecmd = sub.add_parser(
        "voice", help="local voices: list, clone, mix, audition (human-only)"
    )
    voicecmd.add_argument(
        "action", nargs="?", default="", help="list, clone, mix, say, rename, or rm"
    )
    voicecmd.add_argument("rest", nargs="*", help="arguments for the action")
    voicecmd.add_argument(
        "--yes", action="store_true", help="skip the consent confirmation (scripts)"
    )
    voicecmd.set_defaults(func=cmd_voice)

    sub.add_parser("tools", help="list registered tools").set_defaults(func=cmd_tools)
    sub.add_parser("config", help="show configured model tiers").set_defaults(func=cmd_config)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        args = parser.parse_args(["chat"])

    try:
        return args.func(args)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
