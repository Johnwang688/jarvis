"""PX-5 mutation evidence (WS-5c's phone half): each mutant of hud/src/mobile/unlock.ts or app.ts must
fail the phone's vitest (unlock.test.ts, unlockApp.test.ts, pr.test.ts, answer.test.ts). Each file is
restored after every mutant, whatever happens.

Run from hud/: python3 scripts/px5_mutants.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HUD = Path(__file__).resolve().parents[1]
MOBILE = HUD / "src" / "mobile"
UNLOCK = "unlock.ts"
APP = "app.ts"
TESTS = ["src/mobile/unlock.test.ts", "src/mobile/unlockApp.test.ts", "src/mobile/pr.test.ts", "src/mobile/answer.test.ts"]

MUTANTS = [
    (UNLOCK, "resend twice: a second 423 unlocks and sends again",
     "      if (st.unlockTried || st.sends > 1) return { kind: \"locked-again\" };",
     "      if (st.sends > 2) return { kind: \"locked-again\" };"),
    (UNLOCK, "mint a new token on the retry",
     "      if (u !== true) return this.forOp(u, st);\n      return this.send(op, st, stage);\n    }\n    if (status === 410) {",
     "      if (u !== true) return this.forOp(u, st);\n      const fresh = this.freeze(op.key, op.url, { ...op.body });\n"
     "      return this.send(fresh ?? op, st, stage);\n    }\n    if (status === 410) {"),
    (UNLOCK, "show Done for update_requested",
     "    return s.code === \"update_requested\" ? \"Update requested; check at the desk\" : \"Done\";",
     "    return \"Done\";"),
    (UNLOCK, "treat a POST 401 as locked",
     "    if (status === 423) {",
     "    if (status === 423 || status === 401) {"),
    (UNLOCK, "POST before unlocking when locked",
     "    held.promise = this.mirror.open() ? this.send(op, held.state, stage) : this.locked(op, held.state, stage);",
     "    held.promise = this.send(op, held.state, stage);"),
    (UNLOCK, "a 410 resent automatically",
     "      void this.pool.refill();\n      // A 410 is definite",
     "      void this.pool.refill();\n      if (st.sends < 2) return this.send(op, st, stage);\n      // A 410 is definite"),
    (UNLOCK, "a retry after an unlock 409",
     "    return { kind: \"unlock-refused\", status: got.status, body: got.body };\n  }",
     "    if (got.status === 409) return true;\n    return { kind: \"unlock-refused\", status: got.status, body: got.body };\n  }"),
    (UNLOCK, "the body not detached from its source (an SSE re-read could change it)",
     "    const body = JSON.parse(JSON.stringify(fields)) as Json;",
     "    const body = fields;"),
    (UNLOCK, "a duplicate shown as success",
     "    if (status === 409 && body.duplicate === true) return this.recover(op, \"duplicate\", st.after423);",
     "    if (status === 409 && body.duplicate === true) return { kind: \"done\", status: 200, body };"),
    # Fix round (Sol MED 1 and LOW 2; Opus L1 to L5 and the nit).
    (APP, "Done reads the working set again at the tap (Sol MED 1)",
     "`/m/api/working/${encodeURIComponent(id)}/done`, { seq },",
     "`/m/api/working/${encodeURIComponent(id)}/done`, { seq: this.doneEntry(id)?.seq ?? seq },"),
    (APP, "Undo sends a token replaced since its button was drawn",
     " || !token || st.token !== token || ",
     " || !token || "),
    (APP, "Confirm ALWAYS takes a rule read again since it was drawn",
     " || !rule || card.rule !== rule || ",
     " || !rule || "),
    (APP, "a reopened sheet draws AUTHORIZE on while its row's answer is out (Opus L5)",
     "const drawn: CardState = !card.busy && this.answering.has(card.rowId)",
     "const drawn: CardState = false"),
    (APP, "AUTHORIZE ignores a row still answering (Opus L5)",
     "  async allowCard(card: CardState | null = this.card): Promise<void> {\n"
     "    if (!card || this.card !== card || card.busy || this.answering.has(card.rowId)) return;",
     "  async allowCard(card: CardState | null = this.card): Promise<void> {\n"
     "    if (!card || this.card !== card || card.busy) return;"),
    (APP, "a PR row's Unlock and send kept after its head and card changed (Opus nit)",
     "alive = !!view && view.head === body.head && view.card === body.card;",
     "alive = true;"),
    (UNLOCK, "a later op's 2xx clears the control's uncertainty (Sol LOW 2)",
     "      return { kind: \"done\", status, body };",
     "      this.unsure.delete(op.key);\n      return { kind: \"done\", status, body };"),
    (UNLOCK, "a 410 never reads the earlier unknown op's status again",
     "      await this.settle(op.key);\n",
     ""),
    (UNLOCK, "a lost unlock answer trusts the local Locked (Opus L1)",
     "    if (\"offline\" in got || PROXY_DOWN.has(got.status)) { this.reached(false); this.reread(); return { kind: \"unlock-lost\" }; }",
     "    if (\"offline\" in got || PROXY_DOWN.has(got.status)) { this.reached(false); return { kind: \"unlock-lost\" }; }"),
    (UNLOCK, "a lost action answer trusts local state (Opus L1)",
     "      this.reached(false);\n      this.reread();\n      return this.recover(op, \"lost\");",
     "      this.reached(false);\n      return this.recover(op, \"lost\");"),
    (UNLOCK, "a 423 older than an unlock trusts the local Locked (Opus L1)",
     "      if (this.unlocks !== unlocks) this.reread();\n",
     ""),
    (UNLOCK, "a 2xx slides the pill open after a Lock now or a 423 (Opus L2)",
     "      if (this.lockEpoch === epoch) {",
     "      if (true) {"),
    (UNLOCK, "Lock now does not bump the lock epoch (Opus L2)",
     "      this.lockEpoch += 1;\n      this.mirror.lock();\n      this.changed();\n      return \"locked\";",
     "      this.mirror.lock();\n      this.changed();\n      return \"locked\";"),
    (UNLOCK, "a 423 does not bump the lock epoch (Opus L2)",
     "      this.lockEpoch += 1;\n      this.mirror.lock();\n      this.changed();\n      // An unlock finished",
     "      this.mirror.lock();\n      this.changed();\n      // An unlock finished"),
    (UNLOCK, "the slide counts from the answer, not the send (Opus L2)",
     "        this.mirror.slideGated(sentAt);",
     "        this.mirror.slideGated();"),
    (UNLOCK, "our resend's duplicate after a late 423 says already sent (Opus L3)",
     "outcome.why === \"duplicate\" && outcome.relocked && outcome.status?.state === \"refused\"",
     "false"),
    (UNLOCK, "the 423 path says nothing was sent (Opus L4)",
     "  return sent ? \"nothing was done\" : \"nothing was sent\";",
     "  return \"nothing was sent\";"),
]


def main() -> int:
    originals = {f: (MOBILE / f).read_text() for f in (UNLOCK, APP)}
    killed = 0
    try:
        for target, name, old, new in MUTANTS:
            original = originals[target]
            if original.count(old) != 1:
                print(f"  BAD   {name}: the original text is not found exactly once")
                continue
            (MOBILE / target).write_text(original.replace(old, new))
            run = subprocess.run(["npx", "vitest", "run", *TESTS], cwd=HUD, capture_output=True, text=True)
            out = run.stdout + run.stderr
            failed = [line.strip() for line in out.splitlines() if line.strip().startswith(("×", "✗"))]
            if run.returncode != 0 and failed:
                killed += 1
                print(f"  KILLED {name}: {len(failed)} failing, first: {failed[0][:140]}")
            else:
                print(f"  SURVIVED {name}")
            (MOBILE / target).write_text(original)
    finally:
        for f, text in originals.items():
            (MOBILE / f).write_text(text)
    print(f"{killed}/{len(MUTANTS)} mutants killed; unlock.ts and app.ts restored")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
