"""long-bench — does a harness still work when the run stops fitting in a window?

The other benches in this repo measure a model's tool calls (`bench`), one
browser skill (`vocabbench`), whole-Jarvis behaviour on short multi-turn tasks
(`agentbench`), and CAD (`cadbench`). None of them runs long enough to put the
*context manager* under load: vocab-bench tops out around 33 steps, and
agent-bench's `long-haul` only fires compaction at all because its threshold was
hand-tuned down to 1800 tokens.

So every harness change since 2026-08-09 — spill-on-truncate, the token meter,
the tail-mounted working context, parallel dispatch, the step-budget handoff —
is currently unmeasured. This is the ruler.

Two things make it different from the others, and both are deliberate:

**It does not import Jarvis.** A task is a pure `plan(seed)`, a `materialize`
that writes a directory, a prompt, and a grader that reads the directory back.
Anything that can be handed a prompt and a working directory can be measured —
the Jarvis loop, `claude -p`, or a person with an editor — which is what makes a
cross-harness comparison possible at all.

**Ground truth is never written to disk.** `plan(seed)` is pure, so the grader
recomputes the answer at grading time. There is no file in the workspace that
contains the score, and two machines on the same seed grade identically.
"""

__all__ = ["harness", "report", "spec", "tasks", "world"]
