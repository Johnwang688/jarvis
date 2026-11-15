"""Mutation evidence for WS-5c, the phone's unlock window and op tokens
(`docs/plans/2026-11-15-ws5-phone-working-set-plan.md` §7, its mutant list).

Each mutant breaks one guard in a temp copy of the package and runs the one
test meant to hold it. A mutant is killed only when **that test** fails
(FAIL or ERROR on its own name), so a mutant that breaks something else does
not count; the unmutated copy must pass each named test first (the
baseline). The harness is tests/v2/pr_merge_mutants.py's.

Free: fakes, temp HOME, no network, no paid call.

    python tests/v2/remote_unlock_mutants.py          # all mutants
    python tests/v2/remote_unlock_mutants.py 3 7      # by index
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pr_merge_mutants", HERE / "pr_merge_mutants.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

RA = "jarvis/v2/remote_auth.py"
ANS = "jarvis/v2/remote_answer.py"
RW = "jarvis/v2/remote_write.py"
API = "jarvis/v2/remote_api.py"
PM = "jarvis/v2/pr_merge.py"
UNLOCK = "tests/v2/remote_unlock_check.py"
ANSWER = "tests/v2/remote_answer_check.py"
PRC = "tests/v2/pr_merge_check.py"
AUTH = "tests/v2/remote_auth_check.py"
REVIEW = "tests/v2/review_check.py"
REV = "jarvis/v2/review.py"

MUTANTS = [
    ("a read slides the window", API,
     '        return 413, {"error": TOO_LARGE}, []\n    return 200, payload, []\n',
     '        return 413, {"error": TOO_LARGE}, []\n    auth.touch_unlock(session)\n    return 200, payload, []\n',
     ["Window.test_reads_never_slide"], UNLOCK),
    ("a session-only action reopens a closed window", RA,
     "            held = self._open_unlock(digest, passkey)\n            if held is not None:\n"
     '                held["until"] = self.mono() + UNLOCK_S\n',
     '            self._unlocks[digest] = {"passkey": passkey["key"], "until": self.mono() + UNLOCK_S}\n',
     ["Window.test_session_only_slides_an_open_window_and_never_reopens"], UNLOCK),
    ("Lock keeps the pending unlock challenge", RA,
     "        self._challenges = {k: c for k, c in self._challenges.items()\n"
     '                            if not (c["kind"] == "unlock" and c.get("session") == digest)}\n'
     "        if retire:\n",
     "        if retire:\n",
     ["Endings.test_lock_now"], UNLOCK),
    ("the generation check is dropped", RA,
     '            if entry.get("generation") != self._unlock_gen.get(digest, 0):\n',
     "            if False:\n",
     ["Race.test_a_generation_bumped_between_issue_and_verify"], UNLOCK),
    ("the unlock challenge is not spent", RA,
     "        entry = self._challenges.pop(key, None)\n",
     "        entry = self._challenges.get(key)\n",
     ["UnlockFailures.test_a_replay_is_409"], UNLOCK),
    ("open means <= until, not < until", RA,
     '        if held["passkey"] != passkey["key"] or not self.mono() < held["until"]:\n',
     '        if held["passkey"] != passkey["key"] or not self.mono() <= held["until"]:\n',
     ["Window.test_sliding_and_its_edges"], UNLOCK),
    ("the window opens before the counter is saved", RA,
     "            self._write_passkeys(rows, self._user_handle)\n            self._passkeys = rows\n"
     '            self._unlocks[digest] = {"passkey": passkey["key"], "until": self.mono() + UNLOCK_S}\n',
     '            self._unlocks[digest] = {"passkey": passkey["key"], "until": self.mono() + UNLOCK_S}\n'
     "            self._write_passkeys(rows, self._user_handle)\n            self._passkeys = rows\n",
     ["UnlockFailures.test_a_failed_counter_save_is_503_and_stays_closed"], UNLOCK),
    ("the window's binding to the session's passkey is dropped", RA,
     '        if held["passkey"] != passkey["key"] or not self.mono() < held["until"]:\n',
     '        if not self.mono() < held["until"]:\n',
     ["Window.test_the_window_is_bound_to_the_sessions_passkey"], UNLOCK),
    ("a local catch loses `unlock` (remote_answer)", ANS,
     "    except Locked:\n        return locked_reply()\n    except AuthError as exc:\n        return auth_reply(exc)\n",
     "    except AuthError as exc:\n        return auth_reply(exc)\n",
     ["Contract.test_each_family_here_is_exactly_locked_with_nothing_mutated"], UNLOCK),
    ("the act-time PR check of the tapped values is dropped", PM,
     '    if (detail.get("head"), detail.get("card")) != (head, card) \\\n            or action',
     "    if action",
     ["PhoneActs.test_the_tapped_values_after_a_push"], PRC),
    ("Always's preview check is dropped", ANS,
     '                if not hmac.compare_digest(shown["preview"], preview):\n',
     "                if False:\n",
     ["Always.test_the_preview_binds_the_rules_freshness"], ANSWER),
    ("op dedup is skipped", RA,
     "            if found is not None:\n",
     "            if False:\n",
     ["OpTokens.test_a_duplicate_never_runs_and_reuse_is_refused"], UNLOCK),
    ("admission runs before require_unlocked", RA,
     "        self.require_unlocked(session)\n        self.verify_op(session, op)\n"
     '        digest = session["token_sha256"]\n'
     "        with self._lock:\n            self._still_unlocked(session)\n"
     '            reservation = self.ops.admit(digest, op["n"], op["t"], op_digest(method, path, body), rate)\n',
     "        self.verify_op(session, op)\n"
     '        digest = session["token_sha256"]\n'
     "        with self._lock:\n"
     '            reservation = self.ops.admit(digest, op["n"], op["t"], op_digest(method, path, body), rate)\n'
     "        self.require_unlocked(session)\n",
     ["OpTokens.test_the_composed_status_order"], UNLOCK),
    ("the token TTL check is dropped", RA,
     "            if not (t <= now + OP_FUTURE_S and now < t + OP_TTL_S):\n",
     "            if not (t <= now + OP_FUTURE_S):\n",
     ["OpTokens.test_expiry"], UNLOCK),
    ("OP_KEY survives a restart", RA,
     "        self._op_key = secrets.token_bytes(OP_KEY_BYTES)\n",
     '        self._op_key = b"k" * OP_KEY_BYTES\n',
     ["PhoneActs.test_a_restart_refuses_the_old_token"], PRC),
    ("full-store admission evicts instead of refusing", RA,
     "                return mine < OPS_PER_SESSION and len(self._records) < OPS_MAX\n",
     "                while (sum(1 for k in self._records if k[0] == session_digest) >= OPS_PER_SESSION\n"
     "                       or len(self._records) >= OPS_MAX):\n"
     "                    self._records.pop(next(iter(self._records)))\n"
     "                return True\n",
     ["OpTokens.test_capacity_refuses_and_changes_nothing"], UNLOCK),
    ("a running record is pruned", RA,
     '                         if r["state"] == "running" or now < r["expires"]}\n',
     '                         if now < r["expires"]}\n',
     ["OpTokens.test_a_running_record"], UNLOCK),
    ("a refused op releases its token", RA,
     '            self.mark(op.key, op.reservation, "refused")\n',
     "            self._records.pop(op.key, None)\n",
     ["OpTokens.test_states_by_failure_point"], UNLOCK),
    ("a reservation mismatch is ignored", RA,
     '            if (record is None or record["state"] != "running"\n'
     '                    or not hmac.compare_digest(record["reservation"], reservation)):\n',
     '            if (record is None or record["state"] != "running"):\n',
     ["OpTokens.test_a_running_record"], UNLOCK),
    ("the token time check moves outside the Ops lock", RA,
     "        with self._lock:\n            now = self.mono()\n"
     "            if not (t <= now + OP_FUTURE_S and now < t + OP_TTL_S):\n"
     '                raise AuthError(410, EXPIRED_OP, {"expired": True})\n'
     "            self._prune(now)\n",
     "        now = self.mono()\n"
     "        if not (t <= now + OP_FUTURE_S and now < t + OP_TTL_S):\n"
     '            raise AuthError(410, EXPIRED_OP, {"expired": True})\n'
     "        with self._lock:\n"
     "            self._prune(now)\n",
     ["PhoneActs.test_expiry_racing_admission"], PRC),
    ("Rate records before the capacity check", RW,
     '            if not ok():\n                return "full"\n            seen.append(now)\n'
     "            self._seen[digest] = seen\n",
     "            seen.append(now)\n            self._seen[digest] = seen\n"
     '            if not ok():\n                return "full"\n',
     ["OpTokens.test_capacity_refuses_and_changes_nothing"], UNLOCK),
    ("completed is shown as Done without ok (an unknown outcome)", RA,
     '    if state == "completed" and outcome == "ok":\n',
     '    if state == "completed":\n',
     ["OpTokens.test_outcomes_and_their_words"], UNLOCK),
    ("completed is shown as Done without ok (a failed outcome)", RA,
     '    if state == "refused" or (state == "completed" and outcome == "failed"):\n',
     '    if state == "refused":\n',
     ["PhoneActs.test_a_lost_response_reads_back_its_outcome"], PRC),
    ("an accepted Update is shown as Done (Sol r6-1)", RA,
     '        return "Update requested; check at the desk" if code == "update_requested" else "Done"\n',
     '        return "Done"\n',
     ["PhoneActs.test_a_lost_response_reads_back_its_outcome"], PRC),
    ("op status looks up by n alone, across sessions", RA,
     "            record = self._records.get((session_digest, n))\n",
     "            record = next((r for k, r in self._records.items() if k[1] == n), None)\n",
     ["OpTokens.test_the_status_route"], UNLOCK),
    ("the rule read loses its session_live re-check", ANS,
     '            if not auth.session_live(session.get("token_sha256")):\n'
     '                return 401, {"error": "unauthorized"}, []\n'
     "            return 200, payload, []\n",
     '            if name == "detail" and not auth.session_live(session.get("token_sha256")):\n'
     '                return 401, {"error": "unauthorized"}, []\n'
     "            return 200, payload, []\n",
     ["Always.test_the_rule_read"], ANSWER),
    # -- #374's fix round ------------------------------------------------------------------
    ("admission's final session and window check is dropped (Sol MED)", RA,
     "            self._still_unlocked(session)\n            reservation = self.ops.admit(",
     "            reservation = self.ops.admit(",
     ["Retirement.test_an_ending_before_admission"], UNLOCK),
    ("admission runs outside the auth lock (Sol MED)", RA,
     "        with self._lock:\n            self._still_unlocked(session)\n"
     '            reservation = self.ops.admit(digest, op["n"], op["t"], op_digest(method, path, body), rate)\n',
     "        self._still_unlocked(session)\n"
     '        reservation = self.ops.admit(digest, op["n"], op["t"], op_digest(method, path, body), rate)\n',
     ["Retirement.test_an_ending_waits_for_an_admission_under_way"], UNLOCK),
    ("mutating() loses its final check (Opus LOW-1)", RA,
     "            self._guard()           # 401 or `Locked`: the op stays `admitted`\n",
     "",
     ["Retirement.test_an_ending_between_admission_and_the_mutation"], UNLOCK),
    ("mutating() loses its final check (PR family, Opus LOW-1)", RA,
     "            self._guard()           # 401 or `Locked`: the op stays `admitted`\n",
     "",
     ["PhoneActs.test_an_ending_during_the_fresh_read"], PRC),
    ("a gated catch-all swallows the final check (remote_write)", RW,
     "        return exc.status, {\"error\": exc.word, **exc.extra}, []\n    except AuthError:\n        raise\n"
     "    except Exception as exc:  # noqa: BLE001 — may have taken effect: uncertain\n",
     "        return exc.status, {\"error\": exc.word, **exc.extra}, []\n"
     "    except Exception as exc:  # noqa: BLE001 — may have taken effect: uncertain\n",
     ["Retirement.test_an_ending_between_admission_and_the_mutation"], UNLOCK),
    ("a gated catch-all swallows the final check (pr_merge)", PM,
     "        return exc.status, {\"error\": _word(exc.word)}, []\n    except AuthError:\n        raise\n",
     "        return exc.status, {\"error\": _word(exc.word)}, []\n",
     ["PhoneActs.test_an_ending_during_the_fresh_read"], PRC),
    ("the final check slides the window", RA,
     '        if self._open_unlock(session["token_sha256"], passkey) is None:\n            raise Locked()\n',
     '        held = self._open_unlock(session["token_sha256"], passkey)\n        if held is None:\n'
     '            raise Locked()\n        held["until"] = self.mono() + UNLOCK_S\n',
     ["Retirement.test_the_last_check_never_slides"], UNLOCK),
    ("the unlock challenge cap counts across sessions (Sol LOW-3)", RA,
     '                    if c["kind"] == "unlock" and c.get("session") == extra.get("session")]\n',
     '                    if c["kind"] == "unlock"]\n',
     ["StatusAndState.test_unlock_challenges_are_per_session"], AUTH),
    ("Done's refusal loses why_not (Sol LOW-2)", RW,
     '                    refused["why_not"] = _word(payload["why_not"])\n',
     "                    pass\n",
     ["DoneUndo.test_a_linked_row_or_a_running_turn_keeps_it_in"], UNLOCK),
    ("Done's why_not skips the scrub (Sol LOW-2)", RW,
     '                    refused["why_not"] = _word(payload["why_not"])\n',
     '                    refused["why_not"] = payload["why_not"]\n',
     ["DoneUndo.test_a_linked_row_or_a_running_turn_keeps_it_in"], UNLOCK),
    ("Approve selected has no catch-all (Opus LOW-4)", REV,
     "        return _catch(run)\n    return gated(handler, session, APPROVE, value, op, \"review\", act)\n",
     "        return run()\n    return gated(handler, session, APPROVE, value, op, \"review\", act)\n",
     ["Phone.test_an_exception_is_409_uncertain_never_500"], REVIEW),
    ("lock() bumps a retired session's generation (Opus NIT)", RA,
     "            if isinstance(digest, str) and digest in self._sessions:\n",
     "            if isinstance(digest, str):\n",
     ["Endings.test_lock_after_a_retirement_makes_no_generation"], UNLOCK),
    ("an Ops 429 has no Retry-After (Opus NIT)", RA,
     "                raise AuthError(429, OPS_FULL, headers=RETRY_AFTER)\n",
     "                raise AuthError(429, OPS_FULL)\n",
     ["OpTokens.test_capacity_refuses_and_changes_nothing"], UNLOCK),
]


def main(argv: list[str]) -> int:
    harness.MUTANTS[:] = MUTANTS
    return harness.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
