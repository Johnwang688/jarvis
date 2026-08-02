"""The `jarvis` command."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from . import agent as agent_mod
from . import bench as bench_mod
from . import config, permissions, tools
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
        entry = permissions.add_allow(tool.name, args)
        console.print(f"[dim]  allowlisted: {entry} ({config.ALLOWLIST_PATH})[/dim]")
        return True
    return answer in ("y", "yes")


def _make_agent(model: str | None) -> agent_mod.Agent:
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
        model=model, approve=permissions.gate(_approve), on_event=on_event
    )


def cmd_chat(args) -> int:
    jarvis = _make_agent(args.model)
    console.print(f"[dim]model: {jarvis.model} · {len(jarvis.tool_specs)} tools · Ctrl-D to exit[/dim]")

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


def cmd_bench(args) -> int:
    mod = vocab_mod if getattr(args, "family", "tools") == "vocab" else bench_mod
    roster = args.models or mod.DEFAULT_ROSTER
    tasks = mod.TASKS
    if args.task:
        tasks = [t for t in tasks if t.name in args.task]
        if not tasks:
            console.print(f"[red]no task named {args.task}[/red]")
            return 1

    console.print(f"[dim]{len(roster)} model(s) × {len(tasks)} task(s)[/dim]\n")

    summary = []
    for model in roster:
        table = Table(title=model, title_style="bold cyan", header_style="dim")
        table.add_column("task")
        table.add_column("tests", style="dim")
        table.add_column("ok", justify="center")
        table.add_column("calls", style="dim")
        table.add_column("s", justify="right")
        table.add_column("$", justify="right")

        passed = cost = latency = 0.0
        for task in tasks:
            with console.status(f"{model} · {task.name}"):
                result = mod.run_task(model, task)
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
        console.print()
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
    return 0


def cmd_face(args) -> int:
    # Imported lazily so `jarvis tools` etc. don't pay for the voice stack.
    from .face import server as face_server

    if getattr(args, "dangerously_skip_permissions", False):
        # The ONLY way into approve-everything mode, by design. It is a
        # process variable, so a restart is always back to ask + allowlist.
        permissions.set_mode("all")
        console.print(
            "[bold red]⚠ PERMISSIONS OFF — every dangerous tool runs without "
            "asking until this process exits.[/bold red]"
        )
    return face_server.main(args.page)


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
        choices=["tools", "vocab"],
        default="tools",
        help="task family: canned tool-calling tasks, or the real-browser vocab drill",
    )
    bench.set_defaults(func=cmd_bench)

    face = sub.add_parser("face", help="open the Jarvis HUD window (voice mode)")
    face.add_argument("page", nargs="?", default="jarvis.html", help="HUD page to open")
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
