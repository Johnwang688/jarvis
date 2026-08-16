"""SWE-bench Verified, run against two harnesses and graded by the projects' tests.

This exists because long-bench answered a different question. long-bench measures
what a harness does to a *transcript* under load; it contains no test suite, and
nothing in it requires writing code that works. The question actually being asked
was how Jarvis+Luna compares to Claude Code+Sonnet at agentic programming, and
the only honest instrument for that is one where **the repository's own tests
decide**, not a fixture whose answer key I wrote.

So nothing here grades anything. The official `swebench` harness does that, in
its own Docker image, using each project's FAIL_TO_PASS / PASS_TO_PASS lists. My
job is to put a working repo in front of an agent, let it work, and hand the
resulting diff over.

Three design decisions worth stating up front, because each one is a way the
number could have been wrong:

**The agent can actually run the tests.** The instance image has the repo at
`/testbed` with its environment installed. The workspace is that directory copied
out to the host — build artifacts and all — and then bind-mounted back in, so the
agent edits on the host and a `run_tests` helper executes them inside the
container against the real environment. Editing blind would measure something
much narrower than "agentic programming".

**`run_tests` cannot contaminate the patch.** It is written into the workspace and
then added to `.git/info/exclude`, which is local to the clone and never part of
a diff. Without that, `git add -A` would sweep the scaffolding into every
prediction.

**Editing the tests cannot help.** The eval script checks the test files back out
to the base commit and applies the official test patch before running anything,
so an agent that "fixes" a test changes nothing about its score.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DATASET = "princeton-nlp/SWE-bench_Verified"

# Django is 231 of 500 instances. An unstratified sample is half Django, which
# measures one project's conventions rather than SWE ability, so no single repo
# may exceed this share of a subset.
MAX_REPO_SHARE = 0.25

PROMPT = """\
You are working in a checkout of the `{repo}` repository at commit {commit}.

Below is a real issue reported against this project. Fix it by editing the
source. Your work is judged by running the project's own test suite, so the fix
has to actually work -- not merely look plausible.

<issue>
{problem}
</issue>

How to work here:
- Run the tests with `./run_tests <pytest args>` from the repository root. For
  example: `./run_tests tests/test_foo.py -x`. It runs inside the project's real
  environment, so it is the ground truth for whether your change works.
- Edit source files normally. Do not edit the tests: the graders restore every
  test file before scoring, so changes there are discarded and cannot help you.
- Do not use the network, and do not look for the upstream fix. Solve it from
  the code in front of you.
- Leave the repository in a state where the fix is applied. Do not commit; the
  working tree is what gets collected.
"""


@dataclass
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    image: str
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)

    def prompt(self) -> str:
        return PROMPT.format(repo=self.repo, commit=self.base_commit[:12],
                             problem=self.problem_statement.strip())


def load_instances(instance_ids: list[str] | None = None) -> list[Instance]:
    from datasets import load_dataset
    from swebench.harness.test_spec.test_spec import make_test_spec

    rows = load_dataset(DATASET, split="test")
    wanted = set(instance_ids or [])
    out = []
    for row in rows:
        if wanted and row["instance_id"] not in wanted:
            continue
        out.append(Instance(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row["problem_statement"],
            image=make_test_spec(row).instance_image_key,
            fail_to_pass=json.loads(row["FAIL_TO_PASS"]),
            pass_to_pass=json.loads(row["PASS_TO_PASS"]),
        ))
    return out


def subset(instances: list[Instance], size: int, seed: int = 1) -> list[Instance]:
    """A seeded, repo-stratified sample — reproducible, and not 46% Django."""
    rng = random.Random(seed)
    by_repo: dict[str, list[Instance]] = {}
    for inst in instances:
        by_repo.setdefault(inst.repo, []).append(inst)
    for pool in by_repo.values():
        pool.sort(key=lambda i: i.instance_id)
        rng.shuffle(pool)

    cap = max(1, int(size * MAX_REPO_SHARE))
    chosen: list[Instance] = []
    # Round-robin across repos so the cap binds naturally and small projects are
    # represented before the big ones fill the quota.
    order = sorted(by_repo, key=lambda r: (-len(by_repo[r]), r))
    while len(chosen) < size:
        added = False
        for repo in order:
            pool = by_repo[repo]
            taken = sum(1 for c in chosen if c.repo == repo)
            if pool and taken < cap and len(chosen) < size:
                chosen.append(pool.pop())
                added = True
        if not added:
            break
    chosen.sort(key=lambda i: i.instance_id)
    return chosen


# ---------------------------------------------------------------------------
# the workspace: a host checkout wired to a live container
# ---------------------------------------------------------------------------

RUN_TESTS = """\
#!/usr/bin/env bash
# Runs the project's test suite inside its real environment.
exec docker exec {container} bash -lc \
  'source /opt/miniconda3/bin/activate && conda activate testbed && cd /testbed && python -m pytest '"$(printf '%q ' "$@")"
"""


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def container_name(instance: Instance, tag: str) -> str:
    return f"swecmp-{tag}-{instance.instance_id}".replace("__", "-")[:60]


NAMESPACE = "swebench"


def published_image(instance: Instance) -> str:
    """The name this instance's image has on Docker Hub.

    `__` becomes `_1776_` once a namespace is involved — the convention lives in
    swebench's own TestSpec. Pulling the wrong name fails as "repository does not
    exist", which reads exactly like "no prebuilt image published" and is how the
    first estimate of this work concluded everything had to be built locally.
    """
    return f"{NAMESPACE}/{instance.image}".replace("__", "_1776_")


def ensure_images(instance_ids: list[str], max_workers: int = 2) -> None:
    """Get every instance image locally — pulled if published, built if not.

    `run_evaluation` deletes instance images when it finishes, so a workspace
    built afterwards finds nothing. Both harness cells reuse these, so this is a
    one-time cost paid up front rather than a surprise inside one run.

    Pulling is preferred purely on cost: the published image is the same
    artifact, and building it locally spends an hour of CPU reproducing a
    download. Anything unpublished falls back to a local build.
    """
    instances = load_instances(instance_ids)
    unpulled = []
    for inst in instances:
        if image_exists(inst):
            continue
        pulled = _run(["docker", "pull", published_image(inst)])
        if pulled.returncode == 0:
            # Retag to the un-namespaced name, so `prepare()` and the grader
            # both find it under the name make_test_spec produced.
            _run(["docker", "tag", published_image(inst), inst.image])
        else:
            unpulled.append(inst.instance_id)

    if not unpulled:
        return
    import docker
    from datasets import load_dataset
    from swebench.harness.docker_build import build_instance_images

    rows = load_dataset(DATASET, split="test")
    wanted = set(unpulled)
    subset_rows = [r for r in rows if r["instance_id"] in wanted]
    # The tags default to "latest" on make_test_spec but NOT on this entry
    # point, which asserts rather than defaulting. Passing them explicitly keeps
    # the names identical to what make_test_spec produced for `Instance.image`.
    build_instance_images(client=docker.from_env(), dataset=subset_rows,
                          max_workers=max_workers, tag="latest",
                          env_image_tag="latest")


def image_exists(instance: Instance) -> bool:
    return _run(["docker", "image", "inspect", instance.image]).returncode == 0


def prepare(instance: Instance, workspace: Path, tag: str) -> str:
    """Materialize the workspace and start the container. Returns its name."""
    if not image_exists(instance):
        raise RuntimeError(
            f"image {instance.image} is not built — run "
            f"`python -m swecompare images` first")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    name = container_name(instance, tag)
    _run(["docker", "rm", "-f", name])

    # Copy /testbed out with its build artifacts intact. Cloning from git would
    # lose the installed-in-place state the environment depends on.
    staging = f"{name}-stage"
    _run(["docker", "rm", "-f", staging])
    created = _run(["docker", "create", "--name", staging, instance.image, "sleep", "1"])
    if created.returncode:
        raise RuntimeError(f"docker create failed: {created.stderr.strip()[:300]}")
    if workspace.exists():
        _run(["rm", "-rf", str(workspace)])
    copied = _run(["docker", "cp", f"{staging}:/testbed", str(workspace)])
    _run(["docker", "rm", "-f", staging])
    if copied.returncode:
        raise RuntimeError(f"docker cp failed: {copied.stderr.strip()[:300]}")

    # --network none: tests do not need the internet, and it removes one route
    # to the upstream fix. The agent's own shell runs on the host, so this is a
    # reduction of the cheating surface, not a seal — see audit_network().
    # As the host user, not root: the container writes __pycache__ and
    # .pytest_cache straight into the bind-mounted workspace, and root-owned
    # files there cannot be deleted afterwards — the second run on an instance
    # fails to rebuild its workspace. HOME is redirected because the host uid
    # has no home inside the image.
    started = _run([
        "docker", "run", "-d", "--name", name, "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "-v", f"{workspace}:/testbed", instance.image, "sleep", "infinity",
    ])
    if started.returncode:
        raise RuntimeError(f"docker run failed: {started.stderr.strip()[:300]}")

    helper = workspace / "run_tests"
    helper.write_text(RUN_TESTS.format(container=name), encoding="utf-8")
    helper.chmod(0o755)
    # Local to this clone and never part of a diff, so the scaffolding cannot
    # end up inside a prediction.
    exclude = workspace / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        with exclude.open("a", encoding="utf-8") as handle:
            # Byproducts of *running* the tests, not of solving the issue. Most
            # of these repos gitignore them already, but `git add -A` only needs
            # one that does not to sweep compiled bytecode into a prediction.
            handle.write("\nrun_tests\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                         ".tox/\n*.egg-info/\n")
    return name


def teardown(name: str) -> None:
    _run(["docker", "rm", "-f", name])


def extract_patch(instance: Instance, workspace: Path) -> str:
    """The agent's work as a diff against the base commit.

    `add -A` first, so a new file counts; `run_tests` is excluded locally so it
    cannot ride along.
    """
    _run(["git", "-C", str(workspace), "add", "-A"])
    done = _run(["git", "-C", str(workspace), "diff", "--cached", instance.base_commit])
    return done.stdout if done.returncode == 0 else ""


def audit_network(record) -> list[str]:
    """Shell commands that look like they reached the internet.

    Web tools are disallowed for both harnesses, but each still has a shell, and
    every instance is a merged pull request that a determined agent could look
    up. Prevention would mean running the agent itself inside a sealed network
    namespace; this is detection instead, and it is reported alongside the score
    rather than silently assumed away.
    """
    markers = ("curl ", "wget ", "pip install", "git fetch", "git pull",
               "github.com", "githubusercontent", "http://", "https://")
    hits = []
    for name, args in record.tool_calls:
        blob = args.lower()
        if any(marker in blob for marker in markers):
            hits.append(f"{name}: {args[:160]}")
    return hits
