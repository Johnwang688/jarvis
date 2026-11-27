"""Exercise PC launch authorization with fake DNS, interop and model clients."""

from itertools import product
import json
from pathlib import Path
import socket
import subprocess
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from a6b1_acceptance_check import Model, reply
from a6c1_acceptance_check import Base
from r1a_policy_grid_check import BT3_KINDS, baseline_policy, bt3c_baseline_facts, fact_grid
from voice_brain_check import Factory, PREFIX, Recorder, script

from jarvis import config, tools
from jarvis.tools.files import SELF_PROTECTED
from jarvis.v3 import chat, claude_jobs, fastpath, pc, policy, t3_client
from jarvis.v3.voice.claude_brain import ClaudeVoiceBrain
from jarvis.v3.work import Registry, WorkService


START = ("cmd.exe", "/d", "/c", "start", "")
WINDOWS_PATH = r"C:\fixtures\Example Project"
URL = "https://www.example.invalid/a?first=1&second=2#section"
CASES = (
    ("app_open", {"name": "browser"}, "app.open", "L0", "Open an app", "Opened Chrome."),
    ("folder_open", {"project": "fixture"}, "folder.open", "L0", "Open a folder",
     "Opened the folder for fixture."),
    ("link_open", {"url": URL}, "link.open", "L1", "Open a link", "Opened www.example.invalid in the browser."),
)
LAUNCHES = ((*START, "chrome"), ("explorer.exe", WINDOWS_PATH),
            ("rundll32.exe", "url.dll,FileProtocolHandler", URL))
_CONVERT = pc._windows_path
_IS_LAN_HOST = config.is_lan_host
_LAUNCH = pc._launch


class PCCheck(Base):
    def setUp(self):
        super().setUp()
        self.launch = self.enterContext(patch.object(pc, "_launch"))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("Real child process")))
        self.enterContext(patch.object(socket, "getaddrinfo", side_effect=AssertionError("Real DNS lookup")))
        self.dns = self.enterContext(patch.object(config, "is_lan_host", return_value=False))
        self.convert = self.enterContext(patch.object(pc, "_windows_path", return_value=WINDOWS_PATH))
        self.enterContext(patch.object(t3_client, "resolve", side_effect=t3_client.T3Error("missing")))
        self.folder = self.tmp / "Example Project"
        self.folder.mkdir()
        self.registry = Registry(self.v3 / "work" / "projects.json")
        self.registry.add("fixture", self.folder, provider="codex", model="fixture")
        self.work = WorkService(registry=self.registry)
        policy.set_auto(self.policy_file, True)

    def service_with_calls(self, *calls):
        self.model = Model(reply(calls=calls), reply("Fixture complete."))
        return self.service(self.model, approvals=self.broker, work=self.work)

    def typed(self, name, args, *, phone=False):
        svc = self.service_with_calls((name, args))
        svc.send("Open the fixture.", phone=phone)
        self.assertEqual(self.wait_idle(svc), "idle")
        return self.results()[-1]

    def card_rows(self, name, args):
        return {
            "app_open": [{"label": "App", "value": "Chrome"}],
            "folder_open": [{"label": "Project", "value": "fixture"},
                            {"label": "Folder", "value": str(self.folder)}],
            "link_open": [{"label": "URL", "value": args.get("url", "")}],
        }[name]

    def assert_card(self, card, name, args, title, level, why):
        self.assertEqual((card["tool"], card["level"], card["why"]), (name, level, why))
        self.assertEqual(card["card"], {"title": title, "rows": self.card_rows(name, args)})

    def assert_auto(self, event, case):
        name, args, kind, level, title, _ = case
        target = " · ".join(row["value"] for row in self.card_rows(name, args))
        self.assertEqual({key: event[key] for key in ("tool", "title", "target", "level", "why")},
                         {"tool": name, "title": title, "target": target, "level": level, "why": f"default:{kind}"})

    def test_every_app_name_and_alias_uses_the_fixed_argv(self):
        expected = {
            "chrome": (("google chrome", "browser"), (*START, "chrome")),
            "vscode": (("vs code", "visual studio code", "code"), (*START, "code")),
            "spotify": ((), (*START, "spotify:")),
            "explorer": (("file explorer", "files"), ("explorer.exe",)),
            "terminal": (("windows terminal",), (*START, "wt")),
            "t3": (("t3 code",), ("explorer.exe", r"shell:AppsFolder\com.t3tools.t3code")),
        }
        self.assertEqual(set(pc.APPS), set(expected))
        for name, (aliases, argv) in expected.items():
            self.assertEqual(pc.APPS[name].aliases, aliases)
            for alias in (name, *aliases, f"  {name.upper()},. "):
                with self.subTest(alias=alias):
                    self.launch.reset_mock()
                    pc.open_app(pc.app_preview(alias), lambda: None)
                    self.launch.assert_called_once_with(argv)

    def test_unlisted_apps_metacharacters_and_paths_are_refused(self):
        for name in ("", "   ", "codecs", "chrom", "jarvis", "chrome.exe", "./chrome", r"C:\chrome",
                     "chrome;", "chrome&", "chrome|", "chrome%", "chrome^", "chrome!", "chrome*",
                     "chrome'", 'chrome"', "chrome<", "chrome>", "$(chrome)", "`chrome`", "chrome\n"):
            with self.subTest(name=name), self.assertRaisesRegex(pc.PCError, "Available apps are chrome, vscode"):
                pc.app_preview(name)
        self.launch.assert_not_called()

    def test_folders_require_a_registered_directory(self):
        self.assertEqual(self.typed("folder_open", {"project": "fixture"}), "Opened the folder for fixture.")
        self.convert.assert_called_once_with(str(self.folder))
        self.launch.assert_called_once_with(("explorer.exe", WINDOWS_PATH))
        self.launch.reset_mock()
        self.convert.reset_mock()
        for project in ("missing", str(self.folder), "..", "fixture/../fixture"):
            with self.subTest(project=project), self.assertRaises(pc.PCError):
                pc.folder_preview(self.work, project)
        self.folder.rmdir()
        with self.assertRaises(pc.PCError):
            pc.folder_preview(self.work, "fixture")
        self.convert.assert_not_called()
        self.launch.assert_not_called()

    def test_folder_conversion_passes_the_registered_path_as_one_argument(self):
        # Exercise the real conversion code while replacing its subprocess.
        with patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=WINDOWS_PATH + "\n")) as run:
            self.assertEqual(_CONVERT(str(self.folder)), WINDOWS_PATH)
            run.assert_called_once_with(["/usr/bin/wslpath", "-w", str(self.folder)], check=True,
                                        capture_output=True, text=True, timeout=3)

    def test_changed_folder_cannot_use_an_existing_card(self):
        policy.set_auto(self.policy_file, False)
        name, args, *_ = CASES[1]
        svc = self.service_with_calls((name, args))
        svc.send("Open the fixture.")
        item = self.next_card()
        self.registry.remove("fixture")
        replacement = self.tmp / "Other Project"
        replacement.mkdir()
        self.registry.add("fixture", replacement, provider="codex", model="fixture")
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results()[-1], "Error: The registered folder changed after approval.")
        self.launch.assert_not_called()

    def test_https_url_validation_and_one_argument(self):
        for url in (URL, "hTtPs://WWW.Example.invalid.:443/a%20b", "https://www.example.invalid/" + "a" * 1972):
            with self.subTest(url_length=len(url)):
                self.launch.reset_mock()
                preview = pc.link_preview(url)
                self.assertEqual(preview["host"], "www.example.invalid")
                pc.open_link(preview, lambda: None)
                self.launch.assert_called_once_with(("rundll32.exe", "url.dll,FileProtocolHandler", url))
        self.launch.reset_mock()
        self.dns.reset_mock()
        for url in ("", "http://www.example.invalid", "file:///C:/fixture", "javascript:void(0)",
                    r"\\example.invalid\share", "https:///www.example.invalid", "https://user@www.example.invalid",
                    "https://:fake-token@www.example.invalid", "https://www.example.invalid/a\tb", URL + "\x00",
                    URL + "\x7f", URL + "\u0085", URL + "\u200b", URL + "\r\n", URL + '%00', URL + '"',
                    URL + " ", " " + URL, URL + "\u00a0", URL + "\\", URL + "a" * 2000,
                    "https://192.0.2.1", "https://[2001:db8::1]", "https://127.1", "https://0x7f.1",
                    "https://localhost", "https://desk.localhost.", "https://desk.local", "https://desk.home.arpa",
                    "https://bad..example.invalid", "https://%77ww.example.invalid", "https://www.example.invalid:",
                    "https://www.example.invalid:65536", "https://www.example.invalid:0"):
            with self.subTest(url=url[:90]), self.assertRaises(pc.PCError):
                pc.link_preview(url)
        self.launch.assert_not_called()
        self.dns.assert_not_called()

    def test_link_rejects_nonpublic_dns_answers(self):
        private = ".".join(("10", "0", "0", "1"))  # a private-range address, built so it is not a literal
        for addr in ("192.0.2.1", "127.0.0.1", private, "::ffff:127.0.0.1"):
            with (self.subTest(addr=addr), patch.object(config, "is_lan_host", _IS_LAN_HOST),
                  patch.object(socket, "getaddrinfo", return_value=[(None, None, None, None, (addr, 0))]),
                  self.assertRaises(pc.PCError)):
                pc.link_preview(URL)
        self.launch.assert_not_called()

    def test_link_rechecks_dns_after_approval(self):
        policy.set_auto(self.policy_file, False)
        svc = self.service_with_calls(("link_open", {"url": URL}))
        svc.send("Open the fixture.")
        item = self.next_card()
        self.dns.return_value = True
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Error: Use a public HTTPS link"))
        self.launch.assert_not_called()

    def test_pc_policy_defaults_and_taint(self):
        p = policy.Policy(self.policy_file)
        for (_, _, kind, level, _, _), owner_request in product(CASES, (False, True)):
            with self.subTest(kind=kind, owner_request=owner_request):
                self.assertNotIn(kind, policy.OWNER_REQUEST)
                self.assertEqual(policy.DEFAULTS[kind], level)
                self.assertEqual(p.explain(dict(kind=kind, tainted=False, owner_request=owner_request)),
                                 {"level": level, "why": f"default:{kind}"})
                expected = ({"level": "L2", "why": "hard:tainted"} if kind == "link.open"
                            else {"level": level, "why": f"default:{kind}"})
                self.assertEqual(p.explain(dict(kind=kind, tainted=True, owner_request=owner_request)), expected)

    def test_existing_policy_grid_matches_origin_main_with_bt3c_exemptions(self):
        main = baseline_policy(self)
        new_kinds = {case[2] for case in CASES}
        excluded = new_kinds | BT3_KINDS
        self.assertEqual(policy.DEFAULTS.keys() - excluded, main.DEFAULTS.keys() - excluded)
        self.assertEqual(policy.OWNER_REQUEST, main.OWNER_REQUEST)
        kinds = (main.DEFAULTS.keys() | main.OTHER_PEOPLE | {"garden.water", "share.doc"}) - excluded
        baseline = main.Policy(self.policy_file)
        current = policy.Policy(self.policy_file)
        for proposal in fact_grid(sorted(kinds)):
            self.assertEqual(current.explain(proposal), baseline.explain(bt3c_baseline_facts(proposal)), proposal)

    def test_chat_auto_has_a_durable_row_and_emits_the_target(self):
        svc = self.service_with_calls(*(case[:2] for case in CASES))
        self.launch.side_effect = lambda _argv: self.assertEqual(self.states(None), ["auto"] * self.launch.call_count)
        svc.send("Open the fixtures.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results(), [case[-1] for case in CASES])
        self.assertEqual([call.args[0] for call in self.launch.call_args_list], list(LAUNCHES))
        self.assertFalse(svc.snapshot()["outside_text"])
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual(self.states(None), ["auto"] * len(CASES))
        events = [event for event in svc.events(0)["events"] if event["kind"] == "auto"]
        for event, case in zip(events, CASES, strict=True):
            self.assert_auto(event, case)
        self.assertEqual(set(self.model.seen[0]["tools"]) & chat.PC_GATED, chat.PC_GATED)

    def test_chat_cards_require_approval_with_auto_off_or_for_a_model_link_after_taint(self):
        card_cases = [(case, False) for case in CASES] + [(CASES[-1], True)]
        for (case, tainted), choice in product(card_cases, ("approve", "deny")):
            name, args, _, level, title, success = case
            with self.subTest(name=name, tainted=tainted, decision=choice):
                policy.set_auto(self.policy_file, tainted)
                args = {"url": "https://www.example.invalid/" + "a" * 1900} if name == "link_open" else args
                calls = [("vault_list", {})] if tainted else []
                svc = self.service_with_calls(*calls, (name, args))
                svc.new_session()
                self.launch.reset_mock()
                svc.send("Open the fixture.")
                item = self.next_card()
                self.assert_card(item, name, args, title, "L2" if tainted else level,
                                 "hard:tainted" if tainted else "auto-off")
                self.launch.assert_not_called()
                self.broker.answer(item["id"], item["digest"], choice)
                self.assertEqual(self.wait_idle(svc), "idle")
                if choice == "approve":
                    self.launch.assert_called_once()
                    self.assertEqual(self.results()[-1], success)
                else:
                    self.launch.assert_not_called()
                    self.assertTrue(self.results()[-1].startswith("The user declined"))
                self.assertNotIn("auto", self.kinds(svc))
                svc.close()

    def test_phone_refuses_all_three_tools_before_a_card(self):
        for name, args, *_ in CASES:
            with self.subTest(name=name):
                self.assertTrue(self.typed(name, args, phone=True).startswith(f"Refused: {pc.PHONE_REFUSAL}"))
        self.assertEqual(self.states(None), [])
        self.assertEqual(self.broker.pending(), [])
        self.dns.assert_not_called()
        self.convert.assert_not_called()
        self.launch.assert_not_called()

    def test_phone_steering_during_preview_blocks_launch(self):
        entered, release = Event(), Event()

        def lookup(_host):
            entered.set()
            self.assertTrue(release.wait(5))
            return False

        self.dns.side_effect = lookup
        svc = self.service_with_calls(("link_open", {"url": URL}))
        svc.send("Open the fixture.")
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(svc.send("Also using the phone.", phone=True)["steered"])
        finally:
            release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith(f"Refused: {pc.PHONE_REFUSAL}"))
        self.assertEqual(self.states(None), [])
        self.launch.assert_not_called()

    def test_phone_steering_during_conversion_blocks_launch(self):
        entered, release = Event(), Event()

        def convert(_path):
            entered.set()
            self.assertTrue(release.wait(5))
            return WINDOWS_PATH

        self.convert.side_effect = convert
        svc = self.service_with_calls(("folder_open", {"project": "fixture"}))
        svc.send("Open the fixture.")
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(svc.send("Also using the phone.", phone=True)["steered"])
        finally:
            release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith(f"Refused: {pc.PHONE_REFUSAL}"))
        self.assertNotIn("auto", self.kinds(svc))
        self.launch.assert_not_called()

    def test_voice_sdk_offers_all_tools_and_uses_chat_receipts(self):
        factory, events = Factory(), Recorder()
        self.enterContext(patch.object(claude_jobs, "resolve_native_cli", return_value="/example/bin/claude"))
        self.enterContext(patch.object(claude_jobs, "cli_version_status", return_value="ok"))
        brain = ClaudeVoiceBrain(self.v3 / "voice", approvals=self.broker, work=self.work, client_factory=factory)
        self.addCleanup(brain.close)
        brain.subscribe(events)

        def send(calls):
            factory.scripts.append(script(("", calls)))
            return brain.send("Open the fixture.", steer=False, input_id="fixture", fresh_after=0)

        turn = send([case[:2] for case in CASES])
        events.wait("reply", turn=turn)
        client = factory.clients[-1]
        self.assertTrue({PREFIX + name for name in chat.PC_GATED} <= set(client.options.allowed_tools))
        self.assertEqual([result for _, result, _ in client.results], [case[-1] for case in CASES])
        self.assertEqual([call.args[0] for call in self.launch.call_args_list], list(LAUNCHES))
        for case, event in zip(CASES, (e for e in events.events if e["kind"] == "auto"), strict=True):
            self.assert_auto(event, case)
        self.assertEqual(brain.pending_approvals(turn), [])
        self.assertEqual(self.states(None), ["auto"] * len(CASES))

        for tainted in (False, True):
            policy.set_auto(self.policy_file, tainted)
            calls = [("vault_list", {})] if tainted else []
            self.launch.reset_mock()
            turn = send(calls + [case[:2] for case in CASES])
            card_cases = CASES[-1:] if tainted else CASES
            for count, case in enumerate(card_cases, 1):
                name, args, _, level, title, _ = case
                events.wait("approval_pending", count=count, turn=turn)
                item, = brain.pending_approvals(turn)
                self.assert_card(item, name, args, title, "L2" if tainted else level,
                                 "hard:tainted" if tainted else "auto-off")
                self.assertEqual(self.launch.call_count, 2 if tainted else count - 1)
                self.broker.answer(item["id"], item["digest"], "deny" if tainted else "approve")
            events.wait("reply", turn=turn)
            self.assertEqual(self.launch.call_count, 2 if tainted else len(CASES))
            auto_events = (event for event in events.events if event.get("turn") == turn and event["kind"] == "auto")
            for case, event in zip(CASES[:2] if tainted else (), auto_events, strict=True):
                self.assert_auto(event, case)

    def test_launcher_errors_are_safe_and_do_not_retry(self):
        for error, expected in (
            (FileNotFoundError("fixture output"), "Windows launch is unavailable through WSL."),
            (subprocess.TimeoutExpired("fixture output", 3), "The Windows launcher timed out."),
            (subprocess.CalledProcessError(1, "fixture output"), "The Windows launcher reported a failure."),
        ):
            with self.subTest(error=type(error)):
                self.launch.reset_mock()
                self.launch.side_effect = error
                self.assertEqual(self.typed("app_open", {"name": "chrome"}), f"Error: {expected}")
                self.launch.assert_called_once_with(LAUNCHES[0])

    def test_launcher_uses_argv_without_a_shell(self):
        with (patch.dict("os.environ", {}, clear=True),
              patch.object(pc, "WSL_INTEROP_SOCKET", SimpleNamespace(is_socket=lambda: False)),
              patch.object(subprocess, "run") as run):
            for argv, binary in zip(LAUNCHES, ("/mnt/c/Windows/System32/cmd.exe", "/mnt/c/Windows/explorer.exe",
                                              "/mnt/c/Windows/System32/rundll32.exe"), strict=True):
                with self.subTest(executable=argv[0]):
                    run.reset_mock()
                    _LAUNCH(argv)
                    run.assert_called_once_with([binary, *argv[1:]], check=True, timeout=3, shell=False, cwd="/",
                                                env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, start_new_session=True)

    def test_pc_tools_are_gated_explicit_and_self_protected(self):
        fastpath.register_tools()
        self.assertIn("jarvis/v3/pc.py", SELF_PROTECTED)
        self.assertFalse(chat.PC_GATED & set(chat.available_tools(gated=False)))
        self.assertTrue(chat.PC_GATED <= chat.CLEAN_RESULT_TOOLS)
        for name, args, *_ in CASES:
            entry = tools.REGISTRY[name]
            self.assertTrue(entry.dangerous and name in tools.EXPLICIT_ONLY)
            self.assertFalse(tools.parallelizable(name))
            self.assertEqual(set(entry.schema["required"]), args.keys())


if __name__ == "__main__":
    unittest.main()
