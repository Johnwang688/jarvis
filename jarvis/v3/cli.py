"""Command-line entry points for v3."""

import argparse
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import signal
import sqlite3
import sys
import time

from jarvis import config
from jarvis.v2 import hud_install
from . import hud, switch
from .daemon import Daemon, SERVICE_NAME


def run_daemon(_args) -> int:
    daemon = Daemon()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        daemon.start()
        while not stopping:
            time.sleep(0.1)
    finally:
        daemon.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def status(_args) -> int:
    port = config.V3_API_PORT
    address = f"127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    response = None
    try:
        connection.request("GET", "/v3/status", headers={"Host": address})
        response = connection.getresponse()
        if response.status == 200:
            payload = json.loads(response.read())
            if (isinstance(payload, dict) and payload.get("service") == SERVICE_NAME
                    and payload.get("version") == "v3"):
                print(f"v3 daemon running on {address}")
                return 0
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    if response is None:
        print(f"v3 daemon not running on {address}")
    else:
        print(f"Something else answers on {address}")
    return 1


def start(args) -> int:
    env = switch.default_env()
    result = switch.to_v3(env, force=args.force)
    if result == 0:
        hud.open_window(env.out)
    return result


def stop(_args) -> int:
    return switch.stop_v3(switch.default_env())


def restart(_args) -> int:
    return switch.restart_v3(switch.default_env())


def install(args) -> int:
    env = switch.default_env()
    try:
        built = hud.install_hud(allow_dirty=args.allow_dirty)
    except (hud_install.InstallRefused, OSError) as exc:
        env.out(f"jarvis agent install: {exc}")
        return 1
    description = hud_install.describe(built)
    if built.dirty:
        description = (description.removesuffix(" in hud/.")
                       + " in the v3 frontend inputs.")
    env.out(description)
    repo = Path(config.REPO_ROOT)
    return switch.install_unit(
        env,
        unit_path=Path.home() / ".config/systemd/user/jarvis-agent.service",
        python=str(repo / ".venv/bin/python"),
        repo=repo,
    )


def learned(args) -> int:
    from .skill_store import SkillStore, StoreError

    store = SkillStore()
    try:
        if args.action in ("ledger", "curate"):
            if args.action == "curate":
                from .skill_curator import Curator

                rows = Curator(store).run(dry_run=args.dry_run)
            else:
                rows = store.records(with_status=True)
            for row in rows:
                print(json.dumps(row))
            return 0
        if args.action == "rollback":
            result = store.rollback(args.id)
        else:
            flag = store.pin if args.action in ("pin", "unpin") else store.lock
            result = flag(args.name, args.action in ("pin", "lock"))
    except StoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error: The learned skill store could not complete the operation safely.", file=sys.stderr)
        return 1
    print(result)
    return 0


def vault_import(_args) -> int:
    from .vault import Vault

    try:
        added, skipped = Vault().import_memory(Path(config.REPO_ROOT) / "memory")
    except (OSError, ValueError):
        print("Error: Vault import could not complete.")
        return 1
    print(f"Imported {len(added)} memory entries. Skipped {len(skipped)}.")
    return 0


def brain_server(_args) -> int:
    from . import brain_mcp

    return brain_mcp.main()


def brain_registration(args) -> int:
    from . import brain_install

    return {"install": brain_install.install, "status": brain_install.status,
            "uninstall": brain_install.uninstall}[args.action]()


def consolidate_vault(args) -> int:
    from . import consolidate
    from .budget import Budget
    from .claude_usage import read_windows
    from .index import Index

    try:
        with Index() as index:
            if args.dry_run:
                count, size = consolidate.preview(index)
                print(f"Selected {count} documents. Prompt size {size} characters.")
                return 0
            result = consolidate.run(index=index, budget=Budget(usage=read_windows))
    except (OSError, ValueError, sqlite3.Error, OverflowError, RuntimeError):
        print("Error: Consolidation could not complete safely.", file=sys.stderr)
        return 1
    print(f"{result.outcome} ({result.docs} documents)." + (f" {result.note}" if result.note else ""))
    return int(result.outcome == "error")


def add_commands(sub):
    agentd = sub.add_parser("agentd", help="run the v3 daemon in the foreground")
    agentd.set_defaults(func=run_daemon)

    agent = sub.add_parser("agent", help="v3 daemon commands")
    agent_sub = agent.add_subparsers(dest="action", required=True)
    agent_status = agent_sub.add_parser("status", help="show v3 daemon status")
    agent_status.set_defaults(func=status)
    # The owner-op gate recognizes the action word. Options must take no values.
    agent_start = agent_sub.add_parser("start", help="switch to the v3 daemon")
    agent_start.add_argument("--force", action="store_true", help="switch even if v2 is busy")
    agent_start.set_defaults(func=start)
    agent_sub.add_parser("stop", help="stop the v3 daemon").set_defaults(func=stop)
    agent_sub.add_parser("restart", help="restart the v3 daemon").set_defaults(func=restart)
    agent_install = agent_sub.add_parser("install", help="install the v3 HUD and systemd user unit")
    agent_install.add_argument("--allow-dirty", action="store_true", help="install with uncommitted frontend changes")
    agent_install.set_defaults(func=install)

    skills = sub.add_parser("learned", help="owner controls for learned skills")
    skills.set_defaults(func=learned)
    commands = skills.add_subparsers(dest="action", required=True)
    commands.add_parser("ledger", help="show the learned skill ledger as JSON lines")
    commands.add_parser("curate", help="curate learned skills and print the digest as JSON lines").add_argument(
        "--dry-run", action="store_true", help="preview changes without writing")
    commands.add_parser("rollback", help="undo an applied ledger change").add_argument("id")
    for action in ("pin", "unpin", "lock", "unlock"):
        commands.add_parser(action, help=f"{action} a learned skill").add_argument("name")

    vault = sub.add_parser("vault", help="owner memory vault commands")
    vault_sub = vault.add_subparsers(dest="action", required=True)
    vault_sub.add_parser("import", help="copy legacy repo memory into the vault").set_defaults(func=vault_import)

    consolidation = sub.add_parser("consolidate", help="consolidate new documents into the owner vault")
    consolidation.add_argument("--dry-run", action="store_true", help="show selection and prompt size")
    consolidation.set_defaults(func=consolidate_vault)

    brain = sub.add_parser("brain", help="serve or register the v3 brain MCP")
    brain_sub = brain.add_subparsers(dest="action", required=True)
    brain_sub.add_parser("mcp", help="serve the brain over stdio").set_defaults(func=brain_server)
    for action in ("install", "status", "uninstall"):
        brain_sub.add_parser(action, help=f"{action} the user-scope brain MCP registration").set_defaults(
            func=brain_registration)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal agent.")
    add_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(exc)
        return 1
