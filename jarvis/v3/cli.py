"""Command-line entry points for v3."""

import argparse
import base64
from datetime import datetime
import fcntl
import getpass
from http.client import HTTPConnection, HTTPException
import json
from math import isfinite
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import warnings

from jarvis import config
from jarvis.v2 import hud_install
from windows.v3voice_problems import PROBLEMS, UNKNOWN
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


def policy_show(_args) -> int:
    from .policy import MONEY, OTHER_PEOPLE, TAINT_EXEMPT, Policy

    print("Effective policy for untainted proposals with no spend, attendees, or owner event.")
    print("Rows with a condition use those values in place of the example facts.")
    for kind, when, decision in Policy().table():
        condition = f"  when {json.dumps(when, sort_keys=True)}" if when else ""
        hard = ""
        if kind in OTHER_PEOPLE or kind.startswith("share."):
            hard = "  [hard:other-people floor L2]"
        elif kind in MONEY:
            hard = "  [hard:money floor L2]"
        print(f"{kind}  {decision['level']}  [{decision['why']}]{hard}{condition}")
    print("Hard L2 floors")
    print("  Outside text or missing or invalid taint facts, with these exemptions")
    print(f"    {', '.join(sorted(TAINT_EXEMPT))}")
    print("    browser.open and link.open when the owner supplied the exact address this turn")
    print(f"  {', '.join(sorted(OTHER_PEOPLE))}, share.*, or calendar actions without a verified zero attendee count")
    print(f"  {', '.join(sorted(MONEY))}, spending, or an invalid money fact")
    print("  Moving or deleting calendar events without verified Jarvis ownership")
    print("  Unrecognized kinds")
    return 0


def policy_set(args) -> int:
    from .policy import Policy, set_rule

    try:
        previous, current = set_rule(config.V3_POLICY_FILE, args.kind, args.level)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Error: The policy file could not be updated safely.", file=sys.stderr)
        return 1
    print(f"Set {args.kind} to {args.level}. Effective level {previous} -> {current}.")
    for rule in Policy(config.V3_POLICY_FILE).rules_for(args.kind):
        when = rule.get("when", {})
        condition = f"  when {json.dumps(when, sort_keys=True)}" if when else ""
        print(f"{rule['kind']}  {rule['level']}{condition}")
    return 0


def policy_auto(args) -> int:
    from .policy import Policy, set_auto

    try:
        if args.setting != "status":
            set_auto(config.V3_POLICY_FILE, args.setting == "on")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Error: The policy file could not be updated safely.", file=sys.stderr)
        return 1
    print("Automatic running is on." if Policy().auto else "Automatic running is off.")
    return 0


def work_registry(args) -> int:
    from .t3_client import T3, T3Error
    from .work import Registry, WorkError, WorkService

    registry = Registry(config.V3_DIR / "work" / "projects.json")
    try:
        if args.action == "list":
            service = WorkService(registry=registry)
            projects = service.projects()
            count = len(service.threads())
            for project in projects:
                print(f"{project.name}  {project.path}  {project.provider}  {project.model}  {project.base or '-'}")
            print(f"Threads: {count}")
        elif args.action == "add":
            project = registry.add(args.name, args.path, provider=args.provider, model=args.model, base=args.base)
            if not args.no_t3:
                try:
                    T3().project_add(project.path, title=project.name)
                except T3Error as exc:
                    print(f"Warning: {exc} The project remains registered with Jarvis.", file=sys.stderr)
        else:
            registry.remove(args.name)
    except WorkError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _login_store():
    from .logins import Store

    return Store.system()


def _login_session(browser, *, window):
    from .browser.session import BrowserSession

    return BrowserSession(browser, window=window)


def _test_login(args):
    from .browser.link import LinkError
    from .browser.session import BrowserError
    from .browser.signin import find_login, sign_in
    from .logins import LoginError, canonical_origin

    try:
        store = _login_store()
        login = find_login(store, args.name)
        window = not args.hidden
        session = _login_session(login.browser, window=window)
        try:
            if args.page is None or canonical_origin(args.page) == login.origin:
                session.start()
            outcome = sign_in(session, store, login.name, page=args.page)
            line = f"{login.name}: {outcome.detail}"
            wait_for_enter = window and sys.stdin.isatty()
            if wait_for_enter:
                print(line, flush=True)
                try:
                    input("Press Enter to close the browser. ")
                except (EOFError, KeyboardInterrupt):
                    pass
        finally:
            session.close()
        if not wait_for_enter:
            print(line)
    except LoginError:
        print("Error: The saved login could not be loaded.", file=sys.stderr)
        return 1
    except (BrowserError, LinkError):
        print("Error: The browser session could not be used.", file=sys.stderr)
        return 1
    return {"signed_in": 0, "needs_code": 2}.get(outcome.result, 1)


def _add_login(store, args):
    from .logins import LoginError

    login, _ = store._check_add(args.name, args.site, args.user, browser=args.browser, replace=args.replace)
    try:
        with warnings.catch_warnings():
            # Prevent getpass from falling back to input with terminal echo on.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(f"Password for {login.username} at {login.origin}: ")
            confirmation = getpass.getpass("Again: ")
    except (EOFError, KeyboardInterrupt, OSError, UnicodeError, getpass.GetPassWarning):
        raise LoginError("Could not read the password without terminal echo.") from None
    if not password or not confirmation:
        raise LoginError("The password cannot be empty.")
    if password != confirmation:
        raise LoginError("The passwords did not match.")
    login = store.add(args.name, args.site, args.user, password, browser=args.browser, replace=args.replace)
    print(f"Saved login {login.name} for {login.origin} (user {login.username}, browser {login.browser}).")


def _list_logins(store):
    logins = store.list()
    if not logins:
        print("No saved logins.")
        return
    name_width = max(len(login.name) for login in logins)
    origin_width = max(len(login.origin) for login in logins)
    user_width = max(len(login.username) for login in logins)
    for login in logins:
        print(f"{login.name:<{name_width}}  {login.origin:<{origin_width}}  "
              f"{login.username:<{user_width}}  {login.browser}")


def login_command(args) -> int:
    from .logins import LoginError

    if args.action == "test":
        return _test_login(args)

    try:
        if args.action == "add" and not sys.stdin.isatty():
            raise LoginError("Run this in your own terminal. "
                             "The password is typed there and is never passed as an argument.")
        store = _login_store()
        if args.action == "add":
            _add_login(store, args)
        elif args.action == "list":
            _list_logins(store)
        elif store.remove(args.name):
            print(f"Removed login {args.name}.")
        else:
            raise LoginError(f"No saved login named {args.name}.")
    except LoginError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


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


VOICE_ACTIONS = ("setup", "start", "stop", "status")


def voice_helper(args) -> int:
    if args.action == "setup":
        from .voice.installation import setup

        return setup()
    port = config.V3_API_PORT
    address = f"127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        if args.action == "status":
            connection.request("GET", "/v3/status", headers={"Host": address})
        else:
            connection.request("POST", "/v3/owner/voice/helper", json.dumps({"action": args.action}),
                               {"Host": address, "Origin": f"http://{address}",
                                "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        if isinstance(data, dict):
            if (args.action == "status" and response.status == 200
                    and data.get("service") == SERVICE_NAME and data.get("version") == "v3"
                    and isinstance(data.get("voice"), dict)):
                _print_voice_status(data["voice"])
                return 0
            if args.action != "status" and isinstance(data.get("message"), str):
                print(data["message"])
                return int(response.status != 200)
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    print("Could not read the voice status." if args.action == "status"
          else "The v3 daemon could not control the Windows voice helper.")
    return 1


_COMPUTER_CODE_HELP = "Pass the code with --code or set JARVIS_COMPUTER_CODE."


def _write_screenshot(path, image, *, directory=None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600, dir_fd=directory), "wb") as output:
        try:
            output.write(image)
            output.flush()
        except OSError:
            # Remove a partial output only while the name still identifies its file.
            try:
                entry = os.stat(path, dir_fd=directory, follow_symlinks=False)
                if os.path.samestat(entry, os.fstat(output.fileno())):
                    os.unlink(path, dir_fd=directory)
            except OSError:
                pass
            raise


def _save_screenshot(image, destination):
    if destination is not None:
        path = Path(destination).absolute()
        _write_screenshot(path, image)
        return path

    uid = os.getuid()
    folder = Path(tempfile.gettempdir()).absolute() / f"jarvis-computer-{uid}"
    folder.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("The screenshot folder must be private and owned by you.")
        # Keep the save and retention pass together across concurrent CLI calls.
        fcntl.flock(directory, fcntl.LOCK_EX)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"shot-{stamp}-{secrets.token_hex(4)}.png"
        _write_screenshot(name, image, directory=directory)
        with os.scandir(directory) as entries:
            shots = sorted((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
                           for entry in entries if entry.name != name and entry.name.startswith("shot-")
                           and entry.name.endswith(".png") and entry.is_file(follow_symlinks=False))
        for _, old in shots[:-19]:
            os.unlink(old, dir_fd=directory)
        return folder / name
    finally:
        os.close(directory)


def _print_screenshot(data, destination) -> int:
    image, size = data.get("image_b64"), data.get("size")
    app, title = data.get("app"), data.get("title")
    if (not isinstance(image, str) or not image or not isinstance(size, list)
            or len(size) != 2 or any(type(side) is not int or side < 1 for side in size)
            or not isinstance(app, str) or not isinstance(title, str)):
        print("The v3 daemon returned an invalid screenshot.")
        return 1
    try:
        picture = base64.b64decode(image, validate=True)
    except ValueError:
        print("The v3 daemon returned an invalid screenshot.")
        return 1
    try:
        path = _save_screenshot(picture, destination)
    except FileExistsError:
        print("The screenshot output already exists.")
        return 1
    except OSError:
        print("Could not save the screenshot to the requested output or private screenshot folder.")
        return 1
    print(path)
    print("window: " + " ".join(app.splitlines()) + " - " + " ".join(title.splitlines()))
    print(f"size: {size[0]}x{size[1]}")
    return 0


def computer_command(args) -> int:
    action = args.action
    path = "/v3/owner/computer"
    body = {"action": action}
    if action == "screenshot":
        code = args.code if args.code is not None else os.environ.get("JARVIS_COMPUTER_CODE")
        if not code:
            print(_COMPUTER_CODE_HELP)
            return 1
        path += "/do"
        body = {"op": action, "code": code}

    port = config.V3_API_PORT
    address = f"127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=75 if action == "screenshot" else 15)
    try:
        connection.request("POST", path, json.dumps(body),
                           {"Host": address, "Origin": f"http://{address}",
                            "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
    except (OSError, HTTPException, ValueError):
        print("Could not reach the v3 daemon for desktop driving.")
        return 1
    finally:
        connection.close()

    if isinstance(data, dict):
        message = data.get("message")
        if response.status != 200 and isinstance(message, str):
            print(message)
            return 1
        if response.status == 200:
            if action == "screenshot":
                return _print_screenshot(data, args.out)
            if action == "start" and isinstance(data.get("code"), str) and data["code"]:
                print(data["code"])
                print(_COMPUTER_CODE_HELP)
                return 0
            if action == "stop" and isinstance(message, str):
                print(message)
                return 0
            if action == "status" and all(type(data.get(key)) is bool for key in ("enabled", "driving", "bridge")):
                enabled = "enabled" if data["enabled"] else "disabled"
                driving = "active" if data["driving"] else "stopped"
                bridge = "connected" if data["bridge"] else "disconnected"
                print(f"Desktop driving {enabled}, grant {driving}, bridge {bridge}.")
                return 0
    print("The v3 daemon returned an unexpected desktop driving reply.")
    return 1


def _print_voice_status(voice):
    brain, brain_state, turns = voice.get("brain"), voice.get("brain_state"), voice.get("turns")
    if brain == "claude" and brain_state in ("idle", "warming", "running", "stopping", "closed"):
        print(f"The Claude voice brain is {brain_state}.")
    state = voice.get("state")
    if state in ("resting", "listening", "working", "speaking", "asking", "unavailable"):
        print(f"The voice session is {state}.")
    detector = voice.get("detector")
    if isinstance(detector, str):
        print(f"The speech detector reports {detector}.")
    for engine, label in (("transcriber", "transcriber"), ("voice", "speech engine")):
        info = voice.get(engine)
        if isinstance(info, dict):
            name = info.get("name")
            print(f"The {label} is {name}." if isinstance(name, str) else f"The {label} is unavailable.")
    if type(voice.get("gpu_warm")) is bool:
        print("The GPU voice is warm." if voice["gpu_warm"] else "The GPU voice is not warm.")
    if type(turns) is int:
        print(f"Voice turns started: {turns}.")
    problem = voice.get("problem")
    if isinstance(problem, dict):
        code, at = problem.get("code"), problem.get("at")
        if isinstance(code, str) and type(at) in (int, float) and isfinite(at):
            _, line, _ = PROBLEMS.get(code, UNKNOWN)
            print(f"Voice problem {_voice_problem_age(time.time() - at)}: {line}")
    link = voice.get("link")
    if not isinstance(link, dict):
        return
    print("The voice link is listening." if link.get("listening") is True else "The voice link is offline.")
    print("The Windows voice helper is connected." if link.get("connected") is True
          else "The Windows voice helper is disconnected.")
    reason = {
        "not_started": "The voice link has not started.",
        "session_unavailable": "The voice session is unavailable.",
        "key_missing": "The voice pairing file is missing. Run jarvis voice-link setup.",
        "key_invalid": "The voice pairing file is invalid. Run jarvis voice-link setup.",
        "bind_failed": "The voice link could not bind its port.",
        "start_failed": "The voice link could not start.",
        "stopped": "The voice link has stopped.",
    }
    if isinstance(link.get("reason"), str) and link["reason"] in reason:
        print(reason[link["reason"]])
    if isinstance(link.get("helper_version"), str):
        print(f"The Windows voice helper reports version {link['helper_version']}.")
    if type(link.get("level")) is int:
        print(f"Microphone level: {link['level']} out of 255.")


def _voice_problem_age(seconds):
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"


def add_commands(sub, *, voice=True):
    if voice:
        voice_cmd = sub.add_parser("voice", help="manage the Windows voice helper")
        voice_sub = voice_cmd.add_subparsers(dest="action", required=True)
        for action in VOICE_ACTIONS:
            voice_sub.add_parser(action).set_defaults(func=voice_helper)

    computer = sub.add_parser("computer", help="capture the front Windows window")
    computer.set_defaults(func=computer_command)
    computer_sub = computer.add_subparsers(dest="action", required=True)
    for action in ("start", "stop", "status"):
        computer_sub.add_parser(action)
    screenshot = computer_sub.add_parser("screenshot")
    screenshot.add_argument("--code", help="desktop driving code")
    screenshot.add_argument("--out", type=Path, help="path for the new PNG file")

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

    policy = sub.add_parser("policy", help="owner controls for proposal policy")
    policy_sub = policy.add_subparsers(dest="action", required=True)
    policy_sub.add_parser("show", help="show the effective decision-rights table").set_defaults(func=policy_show)
    setter = policy_sub.add_parser("set", help="set a policy rule")
    setter.add_argument("kind")
    setter.add_argument("level")
    setter.set_defaults(func=policy_set)
    automatic = policy_sub.add_parser("auto", help="control automatic actions in owner chat")
    auto_sub = automatic.add_subparsers(dest="setting", required=True)
    for setting in ("on", "off", "status"):
        auto_sub.add_parser(setting).set_defaults(func=policy_auto)

    work = sub.add_parser("work", help="owner controls for coding projects")
    work.set_defaults(func=work_registry)
    work_sub = work.add_subparsers(dest="action", required=True)
    work_sub.add_parser("list", help="show registered projects and thread count")
    add = work_sub.add_parser("add", help="register a project")
    add.add_argument("name")
    add.add_argument("path")
    add.add_argument("--provider", required=True)
    add.add_argument("--model", required=True)
    add.add_argument("--base")
    add.add_argument("--no-t3", action="store_true", help="skip registering the folder with T3")
    work_sub.add_parser("remove", help="remove a registered project").add_argument("name")

    login = sub.add_parser("login", help="manage saved web logins")
    login.set_defaults(func=login_command)
    login_sub = login.add_subparsers(dest="action", required=True)
    save = login_sub.add_parser("add", help="save a login with a password typed in the terminal")
    save.add_argument("name")
    save.add_argument("--site", required=True, help="HTTPS site origin")
    save.add_argument("--user", required=True, help="username")
    save.add_argument("--browser", default="chrome", metavar="{chrome,edge}")
    save.add_argument("--replace", action="store_true")
    login_sub.add_parser("list", help="list saved login metadata")
    login_sub.add_parser("remove", help="remove a saved login").add_argument("name")
    test = login_sub.add_parser("test", help="try a saved login in the browser")
    test.add_argument("name")
    test.add_argument("--page", metavar="URL", help="sign-in page on the saved site")
    test.add_argument("--window", action="store_true", help=argparse.SUPPRESS)
    test.add_argument("--hidden", action="store_true", help="run the browser headless")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal agent.")
    add_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(exc)
        return 1
