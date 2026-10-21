"""Role bench: which model is the cheapest one that is good enough to review a
pull request, or to implement one, in this repository?

Plan: docs/plans/2026-10-20-role-bench-plan.md. Owner decisions and the case
schema: docs/plans/2026-10-20-role-bench-decisions.md. How to run it:
docs/notes/testing-benchmarks.md ("Role bench").

This is not agent-bench, long-bench or swecompare, and its scores are never
pooled with theirs. It reuses their harness runners (`longbench.harness`) and
their network audit (`swecompare.bench.audit_network`) rather than rewriting
them; everything else here is what those two do not have: a leak-proof
snapshot of one real PR, a blinded judge, a spend ledger and a quota guard.
"""
