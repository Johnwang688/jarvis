"""A4 failure paths, CLI wiring, and state transitions on an injected clock."""

import argparse
from http.client import RemoteDisconnected
from pathlib import Path
import stat
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from a4_acceptance_check import World
from jarvis import config
from jarvis.v3 import cli, switch


class DefaultEnv(unittest.TestCase):
    def test_v2_status_uses_configured_loopback_endpoint(self):
        with (patch.object(config, "DAEMON_PORT", 18405),
              patch.object(switch, "HTTPConnection") as connect):
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"version": 2}'
            self.assertEqual(switch.default_env().v2_status(), ({"version": 2}, None))
            connect.assert_called_once_with("127.0.0.1", 18405, timeout=2.0)
            connection.request.assert_called_once_with("GET", "/status")
            response.read.assert_called_once_with(1 << 20)
            connection.close.assert_called_once_with()

    def test_v2_status_distinguishes_connection_refusal_from_an_unreadable_status(self):
        cases = (
            (ConnectionRefusedError(), 200, b"{}", "refused"),
            (TimeoutError(), 200, b"{}", "/status did not answer within 2 s"),
            (RemoteDisconnected(), 200, b"{}", "/status could not be read (RemoteDisconnected)"),
            (None, 503, b"{}", "/status answered HTTP 503"),
            (None, 200, b"?", "/status did not answer with JSON"),
            (None, 200, b"[]", "/status did not answer with a JSON object"),
        )
        for error, code, body, reason in cases:
            with self.subTest(reason=reason), patch.object(switch, "HTTPConnection") as connect:
                connection = connect.return_value
                connection.request.side_effect = error
                response = connection.getresponse.return_value
                response.status = code
                response.read.return_value = body
                self.assertEqual(switch.default_env().v2_status(), (None, reason))
                connection.close.assert_called_once_with()

    def test_systemctl_uses_resolved_native_argv(self):
        with (patch.object(switch.shutil, "which", return_value="/usr/bin/systemctl") as which,
              patch.object(switch, "run") as run):
            self.assertIs(switch.default_env().systemctl("stop", switch.V3_UNIT), run.return_value)
            which.assert_called_once_with("systemctl")
            run.assert_called_once_with(
                ["/usr/bin/systemctl", "--user", "stop", switch.V3_UNIT],
                capture_output=True, text=True, timeout=90.0, stdin=switch.DEVNULL,
            )

    def test_systemctl_refuses_missing_or_windows_executable(self):
        for executable in (None, "/mnt/c/bin/systemctl"):
            with (self.subTest(executable=executable),
                  patch.object(switch.shutil, "which", return_value=executable),
                  patch.object(switch, "run") as run):
                env = World(v2_active=False, v3_active=True).env()
                env.systemctl = switch.default_env().systemctl
                self.assertEqual(switch.stop_v3(env), 1)
                run.assert_not_called()


class Switch(unittest.TestCase):
    def test_command_failure_at_each_step(self):
        for source, target, operation in (
            (switch.V2_UNIT, switch.V3_UNIT, switch.to_v3),
            (switch.V3_UNIT, switch.V2_UNIT, switch.to_v2),
        ):
            steps = [("stop", source), ("disable", source), ("enable", "--now", target)]
            for index, step in enumerate(steps):
                with self.subTest(step=step):
                    world = World(v2_active=source == switch.V2_UNIT,
                                  v3_active=source == switch.V3_UNIT)
                    systemctl = world.systemctl

                    def fail(*args):
                        if args == step:
                            if index == 2:
                                systemctl(*args)  # enable --now can fail after enabling the unit
                            else:
                                world.calls.append(list(args))
                            return CompletedProcess(args, 1, "", "injected failure")
                        return systemctl(*args)

                    world.systemctl = fail
                    self.assertEqual(operation(world.env()), 1)
                    expected = [list(args) for args in steps[:index + 1]]
                    if index:
                        expected += [["stop", target], ["disable", target], ["enable", "--now", source]]
                    self.assertEqual(world.verbs(), expected)
                    output = "\n".join(world.lines)
                    world.sleep(2)
                    self.assertTrue(world._active(source))
                    self.assertTrue(world.enabled[source])
                    self.assertFalse(world._active(target))
                    self.assertFalse(world.enabled[target])
                    if index:
                        self.assertIn(f"Restored {source}", output)
                    self.assertIn("injected failure", output)
                    self.assertIn(f"journalctl --user -u {step[-1]} -n 50 --no-pager", output)
                    self.assertIn("jarvis hud", output)

    def test_busy_approval_refuses_before_mutating_systemd(self):
        world = World()
        world.v2_status = lambda: ({"version": 2, "approvals_pending": 1}, None)
        self.assertEqual(switch.to_v3(world.env()), 3)
        self.assertEqual(world.verbs(), [])
        self.assertIn("--force", "\n".join(world.lines))

    def test_nonpositive_and_noninteger_busy_counts_are_ignored(self):
        for count in (0, -1, True, 1.5, "2", None):
            with self.subTest(count=count):
                world = World()
                world.v2_status = lambda: (
                    {"version": 2, "turns_running": count, "approvals_pending": count}, None,
                )
                self.assertEqual(switch.to_v3(world.env()), 0)

    def test_state_query_failure_does_not_count_as_down(self):
        for source, target, operation in (
            (switch.V2_UNIT, switch.V3_UNIT, switch.to_v3),
            (switch.V3_UNIT, switch.V2_UNIT, switch.to_v2),
        ):
            with self.subTest(source=source):
                world = World(v2_active=source == switch.V2_UNIT, v3_active=source == switch.V3_UNIT)
                env = world.env()
                original = env.systemctl

                def command(*args):
                    if args == ("is-active", source) and ["stop", source] in world.verbs():
                        return CompletedProcess(args, 1, "", "Failed to connect to bus.")
                    return original(*args)

                env.systemctl = command
                self.assertEqual(operation(env), 1)
                self.assertEqual(world.verbs(), [
                    ["stop", source], ["disable", source],
                    ["stop", target], ["disable", target], ["enable", "--now", source],
                ])
                self.assertIn("Failed to connect to bus", "\n".join(world.lines))

    def test_waits_for_port_after_unit_is_down(self):
        world = World()
        env = world.env()
        original = env.systemctl

        def command(*args):
            if args == ("enable", "--now", switch.V3_UNIT):
                self.assertFalse(world._active(switch.V2_UNIT))
                self.assertGreaterEqual(world.now, 1003)
            return original(*args)

        env.systemctl = command
        env.port_free = lambda port: port == config.DAEMON_PORT and world.now >= 1003
        self.assertEqual(switch.to_v3(env), 0)
        self.assertEqual(world.verbs()[:3], [
            ["stop", switch.V2_UNIT], ["disable", switch.V2_UNIT],
            ["enable", "--now", switch.V3_UNIT],
        ])

    def test_v2_reappears_while_waiting_for_port(self):
        world = World()
        env = world.env()

        def sleep(delay):
            world.sleep(delay)
            if world.now >= 1002.5:
                world._active(switch.V2_UNIT)
                world.active[switch.V2_UNIT] = True

        env.sleep = sleep
        # The first process has stopped by 1002, with the port still held.
        # The unit comes back before the port would otherwise be free.
        env.port_free = lambda port: world.now >= 1003
        self.assertEqual(switch.to_v3(env, wait=3.1), 1)
        self.assertAlmostEqual(world.now, 1003.1)
        self.assertTrue(world.active[switch.V2_UNIT])
        self.assertTrue(world.enabled[switch.V2_UNIT])
        self.assertNotIn(["enable", "--now", switch.V3_UNIT], world.verbs())

    def test_start_deadline_is_honoured_and_identity_is_required(self):
        for source, target, operation in (
            (switch.V2_UNIT, switch.V3_UNIT, switch.to_v3),
            (switch.V3_UNIT, switch.V2_UNIT, switch.to_v2),
        ):
            with self.subTest(target=target):
                world = World(v2_active=source == switch.V2_UNIT, v3_active=source == switch.V3_UNIT)
                env = world.env()
                polls = []

                def wrong_status():
                    polls.append(world.now)
                    world._active(target)
                    if target == switch.V3_UNIT:
                        return {"service": "inventory", "version": "v3"}
                    return {"version": 1}, None

                def command(*args):
                    if args == ("enable", "--now", source):
                        self.assertFalse(world._active(target))
                        self.assertFalse(world.enabled[target])
                    return world.systemctl(*args)

                if target == switch.V3_UNIT:
                    env.v3_status = wrong_status
                else:
                    env.v2_status = wrong_status
                env.systemctl = command
                self.assertEqual(operation(env, wait=2.1), 1)
                self.assertAlmostEqual(polls[-1], 1004.1)
                self.assertTrue(world._active(source))
                self.assertTrue(world.enabled[source])
                self.assertIn(f"Restored {source}", "\n".join(world.lines))

    def test_rollback_reports_target_cleanup_failure_without_starting_source(self):
        world = World()
        env = world.env()

        def command(*args):
            if args == ("enable", "--now", switch.V3_UNIT):
                world.systemctl(*args)
                return CompletedProcess(args, 1, "", "partial start")
            if args == ("stop", switch.V3_UNIT):
                return CompletedProcess(args, 1, "", "cleanup failed")
            return world.systemctl(*args)

        env.systemctl = command
        self.assertEqual(switch.to_v3(env), 1)
        self.assertNotIn(["enable", "--now", switch.V2_UNIT], world.verbs())
        output = "\n".join(world.lines)
        self.assertIn("partial start", output)
        self.assertIn("cleanup failed", output)
        self.assertIn(f"Rollback to {switch.V2_UNIT} failed", output)

    def test_oversleep_cannot_start_a_poll(self):
        world = World(v2_active=False, v3_active=True)
        env = world.env()
        polls = []

        def command(*args):
            if args == ("is-active", switch.V3_UNIT):
                polls.append(world.now)
            return world.systemctl(*args)

        env.systemctl = command
        env.sleep = lambda delay: world.sleep(delay + 0.01)
        self.assertEqual(switch.stop_v3(env, wait=0.1), 1)
        self.assertEqual(polls, [1000.0])

    def test_missing_v3_restores_enabled_and_running_v2(self):
        world = World(v3_installed=False)
        self.assertEqual(switch.to_v3(world.env()), 1)
        self.assertTrue(world.enabled[switch.V2_UNIT])
        self.assertTrue(world.active[switch.V2_UNIT])
        self.assertIn(["enable", "--now", switch.V2_UNIT], world.verbs())
        self.assertIn("jarvis agent install", "\n".join(world.lines))

    def test_restart_refuses_when_v2_is_up(self):
        world = World()
        self.assertEqual(switch.restart_v3(world.env()), 1)
        self.assertEqual(world.verbs(), [])

    def test_restart_waits_for_fresh_status_and_stop_waits_for_shutdown(self):
        world = World(v2_active=False, v3_active=True)
        world.v3_status = lambda: {
            "service": "jarvis-agentd", "pid": 1, "started_at": int(world.now >= 1003),
        }
        self.assertEqual(switch.restart_v3(world.env()), 0)
        self.assertEqual(world.now, 1003)
        self.assertIn(["restart", switch.V3_UNIT], world.verbs())
        self.assertEqual(switch.stop_v3(world.env()), 0)
        self.assertEqual(world.now, 1005)
        self.assertFalse(world.active[switch.V3_UNIT])


class Install(unittest.TestCase):
    def test_backup_name_mode_and_contents(self):
        world = World()
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            unit = repo / "jarvis-agent.service"
            unit.write_bytes(b"old unit\n")
            unit.chmod(0o600)
            kwargs = {"unit_path": unit, "repo": repo, "python": str(repo / ".venv/bin/python")}
            self.assertEqual(switch.install_unit(world.env(), **kwargs), 0)
            backup, = repo.glob("*.bak-*")
            self.assertRegex(backup.name, r"^jarvis-agent\.service\.bak-\d{8}T\d{12}Z$")
            self.assertEqual(backup.read_bytes(), b"old unit\n")
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o644)
            self.assertEqual(world.verbs(), [["daemon-reload"]])
            self.assertEqual(switch.install_unit(world.env(), **kwargs), 0)
            self.assertEqual(world.verbs(), [["daemon-reload"]])

    def test_reload_failure_can_be_retried(self):
        world = World()
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            unit = repo / "jarvis-agent.service"
            unit.write_bytes(b"old unit\n")
            unit.chmod(0o600)
            kwargs = {"unit_path": unit, "repo": repo, "python": str(repo / ".venv/bin/python")}
            original = unit.stat()
            env = world.env()
            env.systemctl = lambda *args: CompletedProcess(args, 1, "", "reload failed")
            self.assertEqual(switch.install_unit(env, **kwargs), 1)
            self.assertEqual(unit.read_bytes(), b"old unit\n")
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
            self.assertEqual(unit.stat().st_ino, original.st_ino)
            self.assertEqual(unit.stat().st_mtime_ns, original.st_mtime_ns)
            self.assertEqual(list(repo.iterdir()), [unit])
            self.assertEqual(switch.install_unit(world.env(), **kwargs), 0)
            self.assertEqual(world.verbs(), [["daemon-reload"]])

    def test_symlink_is_refused_without_writes_or_reload(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), TemporaryDirectory() as directory:
                world = World()
                repo = Path(directory)
                source = repo / "managed.service"
                if not dangling:
                    source.write_bytes(b"old unit\n")
                unit = repo / "jarvis-agent.service"
                unit.symlink_to(source.name)
                before = set(repo.iterdir())
                self.assertEqual(switch.install_unit(
                    world.env(), unit_path=unit, repo=repo, python=str(repo / ".venv/bin/python"),
                ), 1)
                self.assertEqual(unit.readlink(), Path(source.name))
                self.assertEqual(set(repo.iterdir()), before)
                if not dangling:
                    self.assertEqual(source.read_bytes(), b"old unit\n")
                self.assertEqual(world.calls, [])
                self.assertIn("symlink", "\n".join(world.lines))

    def test_failed_first_install_removes_new_unit_and_directories(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            unit = repo / "systemd/user/jarvis-agent.service"
            env = World().env()
            env.systemctl = lambda *args: CompletedProcess(args, 1, "", "reload failed")
            self.assertEqual(switch.install_unit(
                env, unit_path=unit, repo=repo, python=str(repo / ".venv/bin/python"),
            ), 1)
            self.assertEqual(list(repo.iterdir()), [])


class Cli(unittest.TestCase):
    def test_owner_action_options_take_no_values(self):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers()
        cli.add_commands(commands)
        agent = commands.choices["agent"]
        actions = next(action for action in agent._actions if isinstance(action, argparse._SubParsersAction))
        for subparser in [agent, *actions.choices.values()]:
            for option in subparser._actions:
                if option.option_strings:
                    self.assertEqual(option.nargs, 0, option.option_strings)

    def test_agent_start_uses_force_and_reports_hud_hook(self):
        world = World(v2_busy=True)
        with patch.object(switch, "default_env", return_value=world.env()):
            self.assertEqual(cli.main(["agent", "start"]), 3)
            self.assertNotIn("no v3 HUD yet", world.lines)
            self.assertEqual(cli.main(["agent", "start", "--force"]), 0)
        self.assertIn("no v3 HUD yet", world.lines)

    def test_agent_install_uses_the_user_unit_and_repo_python(self):
        env = World().env()
        home = Path("/test-home")
        repo = Path("/test-repo")
        with (patch.object(Path, "home", return_value=home),
              patch.object(config, "REPO_ROOT", repo),
              patch.object(switch, "default_env", return_value=env),
              patch.object(switch, "install_unit", return_value=0) as install):
            self.assertEqual(cli.main(["agent", "install"]), 0)
        install.assert_called_once_with(
            env, unit_path=home / ".config/systemd/user/jarvis-agent.service",
            python=str(repo / ".venv/bin/python"), repo=repo,
        )

    def test_hud_switches_only_when_v3_is_active_and_stops_on_failure(self):
        from jarvis import __main__ as main
        from jarvis.v2 import daemon

        args = argparse.Namespace(action="open", no_window=True)
        for active, result in ((False, 0), (True, 0), (True, 1)):
            with self.subTest(active=active, result=result):
                env = World(v3_active=active).env()
                with (patch.object(switch, "default_env", return_value=env),
                      patch.object(switch, "to_v2", return_value=result) as to_v2,
                      patch.object(daemon, "is_running", return_value=True) as v2_running,
                      patch.object(config, "HUD_DEV", True)):
                    self.assertEqual(main.cmd_hud(args), result)
                if active:
                    to_v2.assert_called_once_with(env)
                else:
                    to_v2.assert_not_called()
                if result:
                    v2_running.assert_not_called()
                else:
                    v2_running.assert_called_once_with(config.DAEMON_PORT)

    def test_hud_foreground_refusal_requires_v3_identity(self):
        from jarvis import __main__ as main
        from jarvis.v2 import daemon

        args = argparse.Namespace(action="open", no_window=True)
        for service, expected in (("jarvis-agentd", 1), ("inventory", 0)):
            with self.subTest(service=service):
                world = World()
                env = world.env()
                env.v3_status = lambda: {"service": service}
                with (patch.object(switch, "default_env", return_value=env),
                      patch.object(switch, "to_v2") as to_v2,
                      patch.object(daemon, "is_running", return_value=True) as v2_running,
                      patch.object(config, "HUD_DEV", True)):
                    self.assertEqual(main.cmd_hud(args), expected)
                to_v2.assert_not_called()
                if expected:
                    v2_running.assert_not_called()
                    self.assertIn("Ctrl+C in its terminal", "\n".join(world.lines))
                else:
                    v2_running.assert_called_once_with(config.DAEMON_PORT)


if __name__ == "__main__":
    unittest.main()
