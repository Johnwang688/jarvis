"""The `jarvis` command."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from . import agent as agent_mod
from . import bench as bench_mod
from . import config, tools

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
        answer = console.input("[bold]Allow? [y/N][/bold] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
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

    return agent_mod.Agent(model=model, approve=_approve, on_event=on_event)


def cmd_chat(args) -> int:
    jarvis = _make_agent(args.model)
    console.print(f"[dim]model: {jarvis.model} · {len(jarvis.tool_specs)} tools · Ctrl-D to exit[/dim]")

    read_input = _make_reader()
    session_cost = 0.0
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


def cmd_ask(args) -> int:
    jarvis = _make_agent(args.model)
    turn = jarvis.run_turn(" ".join(args.prompt))
    console.print(f"[dim]{turn.steps} step(s) · {turn.latency_s:.1f}s · ${turn.cost_usd:.4f}[/dim]")
    return 0


def cmd_bench(args) -> int:
    roster = args.models or bench_mod.DEFAULT_ROSTER
    tasks = bench_mod.TASKS
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
                result = bench_mod.run_task(model, task)
            passed += result.passed
            cost += result.cost_usd
            latency += result.latency_s
            detail = ",".join(n for n, _ in result.calls) or "—"
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
    bench.set_defaults(func=cmd_bench)

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
