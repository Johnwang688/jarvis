"""Mutation evidence for the between-turn reader (SV-6, 2026-10-16).

Each mutant breaks one guard in a temp copy of the package and runs the named
checks of `idle_reader_check.py` against the copy (`chat_git_mutants.run`).
Every mutant must make a check fail; one that survives is a guard no test
holds. Free: fakes, recordings, temp HOME. The speech skip's mutant lives in
the HUD's vitest (`hud/src/lib/speech/controller.test.ts`).

    python tests/v2/idle_reader_mutants.py          # all mutants
    python tests/v2/idle_reader_mutants.py 3 7      # by index
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_git_mutants import run  # noqa: E402  (same runner: temp copy, one mutation)

C = "jarvis/v2/providers/claude.py"
X = "jarvis/v2/providers/codex.py"
DMN = "jarvis/v2/daemon.py"
MIR = "jarvis/v2/discord/mirror.py"
RUNNER = "jarvis/v2/runner.py"

MUTANTS = [
    ("hand-off race: a turn does not park the idle reader", C,
     "        await self._park(session)\n        session.reading = True",
     "        session.reading = True",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_one_reader_at_a_time_whenever_the_owner_sends"]),
    # Not a mutant: `_park` without awaiting the cancelled task is
    # equivalent on one event loop (the idle reader routes each message with
    # no await, and `Queue.get` leaves an item in place when cancelled), so
    # the await is defence in depth and no check can tell them apart.
    ("double read: a child's message published and also held", C,
     "            for event in self._translate(session, item):\n                watch.put(event)\n            return True",
     "            for event in self._translate(session, item):\n                watch.put(event)\n            self._hold(session, item)\n            return True",
     ["tests/v2/idle_reader_check.py", "ClaudeRecordings.test_a_background_childs_end_and_the_clis_own_turn_arrive_with_no_send",
      "ClaudeScripted.test_one_reader_at_a_time_whenever_the_owner_sends"]),
    ("auto-approve between turns (Claude)", C,
     "        req_id = uuid.uuid4().hex[:12]\n        pending = _Pending()",
     "        if session.events is None:\n            return Decision.ALLOW\n        req_id = uuid.uuid4().hex[:12]\n        pending = _Pending()",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_an_approval_between_turns_is_the_threads_permit_and_deny_all_stays_deny_all"]),
    ("auto-approve between turns (Codex)", X,
     "            yield from self._server_request(h, msg, pending_request, child=child)\n        except BaseException:\n            if not pending_request.replied:\n                self._refuse(s, msg[\"id\"])\n            raise\n\n    def _server_request",
     "            s.rpc.reply(msg[\"id\"], _decision(True))\n            return\n        except BaseException:\n            if not pending_request.replied:\n                self._refuse(s, msg[\"id\"])\n            raise\n\n    def _server_request",
     ["tests/v2/idle_reader_check.py", "CodexIdle.test_a_childs_approval_between_turns_asks_the_permit_and_nothing_else_answers"]),
    ("Codex: a send reads beside the idle drain", X,
     "                    while s.reader is not None and not s.closed.is_set() and not s.cancelled.is_set():",
     "                    while False:",
     ["tests/v2/idle_reader_check.py", "CodexIdle.test_a_send_waits_for_a_card_the_idle_drain_holds"]),
    ("Discord posts a CLI turn", MIR,
     "            if not turn or kind not in TURN_KINDS or row.get(\"self_started\"):",
     "            if not turn or kind not in TURN_KINDS:",
     ["tests/v2/idle_reader_check.py", "MirrorSkips"]),
    ("the daemon does not mark a CLI turn's records", DMN,
     "            if session.self_started and not idle:\n                record[\"self_started\"] = True",
     "            pass",
     ["tests/v2/idle_reader_check.py", "DaemonClaude.test_the_clis_own_turn_is_a_turn_of_the_thread", "MirrorSkips"]),
    ("leaked reader: close does not park the idle reader", C,
     "        with contextlib.suppress(Exception):\n            _await(session.loop, self._park(session), PARK_TIMEOUT_S)\n        session.held.clear()\n        session.offer = None\n        self._stop_pump(session)\n        try:\n            _await(session.loop, session.client.disconnect(), CONTROL_TIMEOUT_S)",
     "        session.held.clear()\n        session.offer = None\n        self._stop_pump(session)\n        try:\n            _await(session.loop, session.client.disconnect(), CONTROL_TIMEOUT_S)",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_close_while_idle_leaves_no_reader_and_ends_the_watch",
      "DaemonClaude.test_close_resume_and_shutdown_end_the_watcher"]),
    ("leaked watcher: close does not end the feed", C,
     "            if feed is not None:\n                feed.put(_CLOSED)",
     "            pass",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_close_while_idle_leaves_no_reader_and_ends_the_watch",
      "DaemonClaude.test_close_resume_and_shutdown_end_the_watcher"]),
    ("a model switch leaves the idle reader on the old client", C,
     "            with contextlib.suppress(Exception):\n                _await(session.loop, self._park(session), PARK_TIMEOUT_S)\n            session.held.clear()",
     "            session.held.clear()",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_a_model_switch_while_idle_parks_the_reader_and_drops_what_the_old_cli_held"]),
    ("no idle reading at all (the stall)", C,
     "        session.idle = asyncio.ensure_future(self._idle(session))",
     "        return",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_the_buffer_is_drained_between_turns_so_a_late_hook_is_answered",
      "ClaudeRecordings.test_stop_of_one_child_is_confirmed_while_idle"]),
    ("a CLI turn is never offered (attributed to the next send)", C,
     "            session.offer = origin\n",
     "",
     ["tests/v2/idle_reader_check.py", "DaemonClaude.test_the_clis_own_turn_is_a_turn_of_the_thread"]),
    ("the idle reader reports a dead CLI itself, not the next message", C,
     "            session.held.append(item)\n            return False",
     "            if session.watch is not None:\n                session.watch.put(Event(EventKind.ERROR, session.thread_id, {\"message\": \"dead\", \"fatal\": True}))\n            return False",
     ["tests/v2/idle_reader_check.py", "ClaudeScripted.test_a_cli_that_dies_between_turns_is_reported_by_the_next_message_as_before"]),
    ("an owner message overtakes the CLI's own turn", DMN,
     "                if self._maybe_cli(session, eager=True):\n                    # The CLI started a turn by itself and it runs first, in",
     "                if False:\n                    # The CLI started a turn by itself and it runs first, in",
     ["tests/v2/idle_reader_check.py", "DaemonWatch.test_an_owner_message_meeting_an_unadopted_offer_waits_for_it"]),
    ("the queue drains before an offered CLI turn", DMN,
     "                    if self._maybe_cli(session):\n                        drain = False",
     "                    pass",
     ["tests/v2/idle_reader_check.py", "DaemonWatch.test_an_offer_while_a_turn_runs_goes_first_when_it_ends",
      "DaemonWatch.test_an_offer_heard_while_a_turn_runs_is_adopted_when_it_ends"]),
    ("a between-turn gate record keeps the last turn's id", DMN,
     "                      \"turn_id\": None if idle else session.turn_id,",
     "                      \"turn_id\": session.turn_id,",
     ["tests/v2/idle_reader_check.py", "DaemonWatch.test_a_gate_question_between_turns_is_logged_with_no_turn"]),
    ("a fatal error between turns keeps the dead session", DMN,
     "            if fatal and session.worker is None:",
     "            if False:",
     ["tests/v2/idle_reader_check.py", "DaemonWatch.test_errors_between_turns_are_lines_and_a_fatal_one_drops_the_session"]),
    ("the runner reads a CLI turn as its own", RUNNER,
     "             and not record.get(\"self_started\"))",
     "             )",
     ["tests/v2/idle_reader_check.py", "RunnerSkips"]),
    ("the runner gives up on the CLI's own turn after SEND_TIMEOUT", RUNNER,
     "                if CLI_TURN_RUNNING in str(exc):",
     "                if False:",
     ["tests/v2/idle_reader_check.py", "RunnerSkips"]),
    ("Codex: no idle drain (a child is read only by the next send)", X,
     "                    out, more = self._idle_once(h)",
     "                    out, more = [], True\n                    time.sleep(0.1)",
     ["tests/v2/idle_reader_check.py", "CodexIdle.test_a_child_that_outlives_the_turn_is_read_with_no_send",
      "CodexIdle.test_stop_of_one_child_between_turns"]),
    ("Codex: an unknown thread's request between turns is let through", X,
     "            if params.get(\"threadId\") != s.thread_id:\n                s.rpc.reply(msg[\"id\"], error={\"code\": -32602, \"message\": \"Request identity mismatch\"})\n                raise RpcError(\"Codex server request identity mismatch\")\n            s.rpc.reply(msg[\"id\"], error={\"code\": -32603, \"message\": \"No turn is running\"})",
     "            s.rpc.reply(msg[\"id\"], error={\"code\": -32603, \"message\": \"No turn is running\"})",
     ["tests/v2/idle_reader_check.py", "CodexIdle.test_an_unknown_threads_request_between_turns_is_fatal_as_in_a_turn"]),
]


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(index, MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}", flush=True)
        if not killed:
            survived.append(MUTANTS[index][0])
    print()
    if survived:
        print(f"{len(survived)} of {len(picks)} mutants survived")
        return 1
    print(f"all {len(picks)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
