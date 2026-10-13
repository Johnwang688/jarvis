#!/usr/bin/env python3
"""One-time lossless CLAUDE.md migration; no application/config imports.

An integrated source requires --source-commit, --source-sha and --section-map
(JSON source_lines, areas and core) together. Inspect the source and map first.
Check/self-test automatically use a present manifest\'s inspected provenance.

The pinned source is read through git -C. Dry-run writes nothing. Apply refuses
a changed source and any existing destination. Check reconstructs the source,
including whitespace/order, and checks retained guide copies. Self-test corrupts
only a temporary copy to demonstrate that the verifier detects loss or edits.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "7c22612a9abfbe4b68249f9643da0bddd82e29ca"
SHA = "e0ce92391edcf72997e5014b1a2b174844479e064694d02a8a6b5a41cff59acf"
MANIFEST = "docs/notes/claude-migration.json"
SOURCE_LINES = 9648
# Complete prose boundaries, never a bold phrase inside a paragraph.
AREAS = (
    (1, "orientation"), (25, "providers-runtime"), (96, "discord"),
    (231, "control-plane"), (334, "browser"), (642, "providers-runtime"),
    (775, "terminals"), (862, "control-plane"), (935, "providers-runtime"),
    (994, "hud-workspace"), (1527, "terminals"), (1703, "browser"),
    (2173, "vex"), (2434, "hud-workspace"), (2525, "safety-permissions"),
    (2786, "hud-workspace"), (2897, "editor"), (3244, "safety-permissions"),
    (3506, "browser"), (3600, "editor"), (4221, "hud-workspace"),
    (4602, "testing-benchmarks"), (4625, "orientation"),
    (4722, "invariants"), (4961, "safety-permissions"),
    (6315, "testing-benchmarks"), (7119, "models"), (7141, "decisions"),
    (7277, "testing-benchmarks"), (7437, "safety-permissions"),
    (7473, "voice-face"), (7845, "integrations"), (7878, "legacy-runtime"),
    (8008, "voice-face"), (8328, "integrations"), (8421, "discord"),
    (8476, "legacy-runtime"), (8555, "integrations"), (8574, "desktop"),
    (8631, "discord"), (8674, "legacy-runtime"), (8886, "safety-permissions"),
    (9137, "testing-benchmarks"), (9229, "legacy-runtime"),
    (9255, "integrations"), (9299, "models"), (9388, "voice-face"),
    (9434, "models"), (9539, "integrations"), (9566, "live-validation"),
)
CORE = ((9, 32, "chat-git-and-v2"), (4625, 4658, "purpose-and-environment"),
        (4722, 4960, "invariants"), (7141, 7276, "decisions"))
INTRO = """# CLAUDE.md

Working instructions for agents changing Jarvis. Read this guide, then the area
notes and design/decision documents for the code you will touch. Update current
rules here; put implementation history, review findings and measured results in
the appropriate area note. Keep this guide near 38–40 KB.

The previous file is preserved verbatim in [area notes](#area-index), with a
[migration manifest](docs/notes/claude-migration.json). Those notes mix live
rules, dated findings and superseded behavior. Moving a rule does **not** revoke
its safety obligations. Summaries here are not permission to omit a guard:
read the complete relevant notes before changing a boundary. Resolve older
conflicting accounts using the latest explicit owner decision and verify
current code. Historical “live” claims are not new deployment receipts.

Invariants and decisions are retained byte for byte; their named paragraph
references are in the notes. Preserved paths are repository-relative.

"""
V2 = """## Working on v2

Read [the design](docs/jarvis-v2-design.md) for `jarvis/v2/` changes. Jarvis owns
the control plane; Claude/Codex supply the brains; v1 remains the fast path.
Keep the design's settled decisions.

Claude's `PreToolUse` hook is its gate; its SDK sandbox does not confine on
this WSL2. Preserve native CLI resolution/version floors and startup/resume
policy checks. Sandbox widening needs a human, never Always; deny without one.
Layer 1 refuses protected/credential grants. Unknown/malformed approvals fail
closed. Both providers' `answer()` only denies.

Use `TaskControl` for task verbs. Tasks pin their root. Archive keeps records;
permanent delete is owner HUD-only, archived-only, into trash, never project
folders/worktrees. Never `_Store._delete` a project. `Daemon.start()` never calls
a model. Quota, title, cost and cache claims need evidence.

The HUD uses **Graphite**: warm off-black/glacier-cyan, amber pending/red error,
overriding the global palette. Preserve dimensions, input behavior and owner
workspace/editor/Browser decisions.

"""
SAFETY = """## Safety design (deliberate, do not loosen without asking)

Read [all safety rules/counterexamples](docs/notes/safety-permissions.md) and
[control-plane rules](docs/notes/control-plane.md) for boundary changes. These
summaries do not replace detailed matchers, exceptions or owner choices.

- **Enforced toolsets.** Tools stay narrow and typed. Keep `run_readonly`
  separate from `run_command`. Dispatch refuses unbound/out-of-toolset calls
  before argument parsing and approval, dangerous or not. New callers bind
  their tools. Deferred groups cannot widen explicit toolsets and require a
  nonempty held core. ContextVars/copied worker contexts carry the caller's
  approver, tools and limits. `unconfined` is code-only, never a model setting.
- **Commands are judged in full.** Every quote-aware segment is judged; DENY
  wins and uncertain work asks. Keep separate rules for inline source,
  substitution, writes/redirects and runner wrappers. A binary allowlist is
  valid only with its original constraints. ALLOW skips a prompt only for a
  human-backed approver; deny-all workers stay deny-all. Allowlist coverage
  requires every segment; substitutions/fetch-execute never gain static
  coverage. Preserve whole-token stem/full-path matching.
- **Permission state cannot authorize its replacement.** Known allowlist
  writes are DENY even in mode “all”; other protected-state writes and uncertain
  paths retain their ASK/deny behavior despite allowlisting. Keep the shared
  detector, v1/v2 parity, indirect/ancestor/symlink checks, routing/model/default
  files, Discord state and saved Codex catalog. Do not silently alter the
  owner's explicit SELF_PROTECTED decisions.
- **Secrets are use-but-never-see.** Agents read no credential `.env*` files;
  they write only permitted `*.example` files. The owner can edit credentials
  in the owner-only HUD editor. Use the shared env-name/path helpers including
  case, Windows trimming, symlink names/targets and fail-closed uncertainty.
  Approved shell commands still cannot print credentials. Retain name refusal,
  command refusal and output scrubbing. Read/search/diff/attachment/preview/
  terminal-read paths never become another credential route. File writes keep
  read-before-write. Tests always use temporary config, never real tokens.
- **No operation of one's own gate.** Only the broker asks the owner. Keep
  identity/listener/Origin/place checks and `config.control_ports()` parity.
  Browser/fetch refuse the control plane before actions, reads and every
  redirect; HUD/API responses refuse framing, and write tools cannot alter
  the installed HUD. Owner restart/rebuild, daemon-unit and daemon-kill
  operations remain agent-refused. A test pass is no authorization to restart.
- **Public web does not open the LAN.** Keep address, redirect/landing and host
  checks, fresh contexts and action budgets. v1 never takes the owner's real
  browser profile. The v2 URL judge, SOCKS gate and action reviewer do different
  jobs: read [Browser notes](docs/notes/browser.md) before changing any. Only
  owner navigation admits a loopback port; agent-triggered auto-open is Direct
  only and never admits one. No frame takes focus without the owner's gesture,
  including behind cards or in hidden panes.
- **Untrusted input stays data.** Strip hidden/invisible web text while keeping
  visible content, fence fetched text and retain provenance. Script review is
  an accident guard, not an adversarial boundary: unsafe denies, errors/unclear
  ask, safe auto-approves only from configured trusted install hosts. Keep its
  opt-out. SVGs are allowlist-sanitized and rendered as noninteractive images;
  avatars cannot repaint pending/error meaning or disguise the HUD title.
- **Delegation inherits limits.** Children intersect tools and inherit the
  approver and depth bound. Workflows keep safe tools/deny-all. Attended tasks
  carry the actual approver object/origin and human-backed flag, and forbid
  spawn recursion/foreground stealing. Shared-browser jobs serialize and keep
  the existing page/action budget; re-snapshot afterward. Session tools read,
  never switch the owner's live session. Speech never authorizes an approval.
- **Background git has its own boundary.** `gitops` judges the full invocation,
  writes owned worktrees/branches only and never merges. Keep hooks, fsmonitor,
  recursive submodule execution, executable config and transports hardened,
  and protected-file commit/push checks. Generic writes cannot alter `.git` or
  global git config. The approved v2 chat grants are not a blanket exception
  for v1 workers, tasks or other paths.
- **Integrations stay scoped.** Consent/setup is human-only and credentials stay
  inside the integration. Gmail sends remain gated. Spotify's intentionally
  ungated reversible playback is absent from unattended workflows, and rotating
  tokens persist before use. Onshape writes target the pinned sandbox; libraries
  are read-only. Desktop tools accept registered app names, not arbitrary
  paths/windows, and exclude terminals/file managers/approval browsers. The
  owner starts the bridge. Keep local voice privacy/offline and owner-only
  stores. VEX tests use fakes; compilation is not a verified robot download.

"""
OPERATIONS = """## Testing and operating the checkout

Use focused free checks, then the required integration sweep. Independent review
and meaningful tests precede merging. Preserve negative cases and mutation
evidence when a subtle invariant guards a boundary. Read actual suite summaries,
not just a wrapper's exit code. Report whether a result is committed, merged,
built, browser-tested, installed or live-validated.

In this WSL workflow, cap each test/build tree with
`systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`. Use the
worktree's PYTHONPATH/shared venv as appropriate. Tests use temporary HOME/config,
fake providers and ephemeral ports, never live 8402/8403/8405 or owner state.
Browser tests need an isolated mock server; do not operate the live daemon.

Under load, wait for the actual state rather than sleeping. Measure computation
with a clock appropriate to the path; calling-thread CPU excludes off-thread
work. A post-return wall assertion cannot interrupt a hang. Keep active deadlines
where needed, pin guard-strength constants and prove costly mutations still fail.
Expiry from a fake monotonic clock must be relative, not assume system uptime.
Count product faults separately from test-owned teardown.

Budget paid probes. Agent-bench, long-bench and SWE-bench answer different
questions; do not pool scores. Use the [SWE-bench runbook](swecompare/RUNBOOK.md)
and [test inventory](docs/notes/testing-benchmarks.md). Verify old prices/model
comparisons. The [old validation list](docs/notes/live-validation.md) is history,
not current completion evidence.

"""
INDEX = """## Area index

Read the matching notes before editing. Current architecture/decisions also
live in the design/plans. Markers refer to the original file\'s lines.

| Area | Notes and current entry point |
|---|---|
| Purpose, WSL and v1 architecture | [Orientation](docs/notes/orientation.md) |
| v2 providers, steering, accounting | [Runtime](docs/notes/providers-runtime.md), [design](docs/jarvis-v2-design.md) |
| Gates, owner routes, session MCP | [Control plane](docs/notes/control-plane.md), [MCP decisions](docs/plans/2026-10-10-terminal-read-mcp-decisions.md) |
| Permissions, secrets, runners, Git | [Safety](docs/notes/safety-permissions.md), [env rule](docs/plans/2026-10-11-env-files.md) |
| Discord commands/channels/reports | [Discord](docs/notes/discord.md), [plan](docs/plans/2026-10-07-discord-plan.md) |
| HUD chats/layout/installed app | [Workspace](docs/notes/hud-workspace.md), [decisions](docs/plans/2026-10-09-hud-workspace-decisions.md) |
| Files, editor, diffs and settings | [Editor](docs/notes/editor.md), [decisions](docs/plans/2026-10-09-hud-editor-decisions.md) |
| Browser, preview and auto-open | [Browser](docs/notes/browser.md), [decisions](docs/plans/2026-10-10-hud-browser-decisions.md), [amendment 2](docs/plans/2026-10-12-hud-browser-plan-amendment-2.md) |
| PTYs and terminal reads | [Terminals](docs/notes/terminals.md) |
| VEX build/device/project work | [VEX](docs/notes/vex.md) |
| Loop/transcript contracts | [Invariant snapshot](docs/notes/invariants.md); active copy below |
| Owner decisions | [Decision snapshot](docs/notes/decisions.md); active copy below |
| Suites and benchmark results | [Tests/benchmarks](docs/notes/testing-benchmarks.md) |
| Routing, reasoning and models | [Models](docs/notes/models.md) |
| Voice, avatars and older face | [Voice/face](docs/notes/voice-face.md) |
| Gmail, CAD, Spotify, schoolwork | [Integrations](docs/notes/integrations.md) |
| Windows app bridge | [Desktop](docs/notes/desktop.md) |
| Earlier goals/sessions/runtime | [Earlier runtime](docs/notes/legacy-runtime.md) |
| Old outstanding probes | [Historical validation](docs/notes/live-validation.md) |

[Manifest](docs/notes/claude-migration.json) and
[migration utility](scripts/slim_claude.py): `python scripts/slim_claude.py --check`
reconstructs the original bytes and verifies retained copies; `--self-test`
detects corruption in temporary copies. Keep archive payloads unchanged.
Update the retained-copy check explicitly if a current invariant or decision
later changes.

"""

def require(ok, message):
    if not ok:
        raise ValueError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def source():
    data = subprocess.run(["git", "-C", str(ROOT), "show", f"{COMMIT}:CLAUDE.md"],
                          check=True, capture_output=True).stdout
    require(sha(data) == SHA and len(data.splitlines(keepends=True)) == SOURCE_LINES, "wrong source")
    return data

def markers(a, b):
    stem = f"claude-source:{a:06d}-{b:06d}"
    return f"<!-- {stem}:begin -->\n".encode(), f"<!-- {stem}:end -->\n".encode()

def excerpt(lines, a, b, name):
    return (f"<!-- retained:{name}:begin -->\n".encode() + b"".join(lines[a-1:b])
            + f"<!-- retained:{name}:end -->\n\n".encode())

def build(data):
    lines = data.splitlines(keepends=True)
    outputs, blocks = {}, []
    for i, (a, area) in enumerate(AREAS):
        b = AREAS[i+1][0]-1 if i+1 < len(AREAS) else len(lines)
        require(a == 1 or not lines[a-2].strip(), f"mid-paragraph boundary {a}")
        path = f"docs/notes/{area}.md"
        if path not in outputs:
            outputs[path] = (f"# {area.replace('-', ' ').title()} — preserved notes\n\n"
                f"Verbatim from `CLAUDE.md` at `{COMMIT}`.\n"
                "Archived instruction/history snapshot, not a new deployment receipt.\n"
                "Both current obligations and dated/superseded findings appear here.\n"
                "Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),\n"
                "read this area's complete rules before changing a boundary, and resolve older\n"
                "conflicts using the latest explicit owner decision and current implementation.\n"
                "Paths in preserved prose are repository-relative. Source labels refer to the\n"
                "original file. Keep payloads verbatim; later notes belong outside the markers.\n\n").encode()
        payload = b"".join(lines[a-1:b])
        begin, end = markers(a, b)
        outputs[path] += f"## Original CLAUDE.md lines {a}–{b}\n\n".encode() + begin + payload + end + b"\n"
        blocks.append(dict(start=a, end=b, path=path, sha256=sha(payload), bytes=len(payload)))
    outputs = {p: v.rstrip(b"\n") + b"\n" for p, v in outputs.items()}
    guide = INTRO.encode()
    for a, b, name in CORE[:2]:
        guide += excerpt(lines, a, b, name)
    guide += (V2 + SAFETY + OPERATIONS).encode()
    guide += INDEX.encode()
    for a, b, name in CORE[2:]:
        guide += excerpt(lines, a, b, name)
    guide = guide.rstrip(b"\n") + b"\n"
    core = CORE
    manifest = dict(format=1, source_commit=COMMIT, source_path="CLAUDE.md",
        source_sha256=SHA, source_bytes=len(data), source_lines=len(lines),
        archive_model="Every source line occurs once in archive payloads; guide excerpts are additional copies.",
        blocks=blocks, notes_sha256_at_migration={p: sha(v) for p, v in sorted(outputs.items())},
        retained_guide=[dict(start=a,end=b,name=n,sha256=sha(b"".join(lines[a-1:b]))) for a,b,n in core],
        guide_bytes_at_migration=len(guide), guide_sha256_at_migration=sha(guide),
        section_map=dict(source_lines=SOURCE_LINES, areas=AREAS, core=CORE))
    outputs["CLAUDE.md"] = guide
    outputs[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2)+"\n").encode()
    return outputs, manifest

def links(root, paths):
    for name in paths:
        text = (root/name).read_text()
        for value in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
            if value.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = value.split("#",1)[0]
            require(not target or ((root/name).parent/target).exists(), f"broken link {name}: {target}")
        require(not re.search(r"^@(?:\./|\.\./|docs/)",text,re.M), f"unexpected auto-import {name}")

def verify(root, original):
    m = json.loads((root/MANIFEST).read_text())
    require(m["source_commit"] == COMMIT and m["source_sha256"] == SHA, "source metadata")
    notes = {}
    for path in m["notes_sha256_at_migration"]:
        notes[path] = (root/path).read_bytes()
    payloads, next_line, ordered = [], 1, {}
    for block in m["blocks"]:
        a,b,path = block["start"],block["end"],block["path"]
        require(a == next_line and b >= a, f"gap/overlap/order at {a}")
        next_line = b+1
        begin,end = markers(a,b)
        raw = notes[path]
        require(raw.count(begin) == raw.count(end) == 1, f"missing/duplicate marker {a}")
        lo,hi = raw.index(begin)+len(begin),raw.index(end)
        require(lo <= hi, f"reversed marker {a}")
        payload = raw[lo:hi]
        require(len(payload)==block["bytes"] and sha(payload)==block["sha256"], f"payload edited {a}")
        require(len(payload.splitlines(keepends=True))==b-a+1, f"line count {a}")
        payloads.append(payload)
        ordered.setdefault(path,[]).append(a)
    require(next_line == SOURCE_LINES + 1, "incomplete coverage")
    for path, starts in ordered.items():
        physical = [int(v) for v in re.findall(rb"<!-- claude-source:(\d+)-\d+:begin -->",notes[path])]
        require(physical==starts, f"area block order: {path}")
    restored = b"".join(payloads)
    require(len(restored)==m["source_bytes"]==len(original), "byte count")
    require(sha(restored)==SHA, "reconstruction hash")
    require(Counter(restored.splitlines(keepends=True))==Counter(original.splitlines(keepends=True)), "line multiset")
    require(restored==original, "byte ordering")
    guide = (root/"CLAUDE.md").read_bytes()
    require(38000 <= len(guide) <= 40000, f"guide size {len(guide)} outside 38000..40000")
    lines = original.splitlines(keepends=True)
    for item in m["retained_guide"]:
        begin = f"<!-- retained:{item['name']}:begin -->\n".encode()
        end = f"<!-- retained:{item['name']}:end -->\n".encode()
        require(guide.count(begin)==guide.count(end)==1, "retained marker")
        payload = guide[guide.index(begin)+len(begin):guide.index(end)]
        require(payload==b"".join(lines[item["start"]-1:item["end"]]), f"retained {item['name']} differs")
        require(sha(payload)==item["sha256"], "retained digest")
    links(root, ["CLAUDE.md",*notes])
    return dict(source_bytes=len(original),source_lines=SOURCE_LINES,guide_bytes=len(guide),
                notes=len(notes),blocks=len(payloads),source_sha256=sha(restored))

def write(root, outputs):
    for name,data in outputs.items():
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)

def self_test(outputs, original):
    with tempfile.TemporaryDirectory(prefix="jarvis-docs-proof-") as tmp:
        root=Path(tmp)
        def reset():
            write(root, outputs)
            for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)",outputs["CLAUDE.md"].decode()):
                if target.startswith("#") or target in outputs:
                    continue
                path=root/target
                if not path.exists():
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.symlink_to(ROOT/target)
        reset()
        verify(root, original)
        m=json.loads(outputs[MANIFEST])
        block=m["blocks"][0]
        path=block["path"]
        raw=outputs[path]
        begin,end=markers(block["start"],block["end"])
        lo,hi=raw.index(begin)+len(begin),raw.index(end)
        payload=raw[lo:hi]
        mutations={
            "deleted line":raw[:lo]+payload.split(b"\n",1)[1]+raw[hi:],
            "duplicated line":raw[:lo]+payload.splitlines(keepends=True)[0]+raw[lo:],
            "reordered lines":raw[:lo]+b"".join(reversed(payload.splitlines(keepends=True)))+raw[hi:],
            "edited byte":raw[:lo]+b"!"+raw[lo+1:],
        }
        for label, mutant in mutations.items():
            reset()
            (root/path).write_bytes(mutant)
            try:
                verify(root, original)
            except ValueError:
                print("detected",label)
            else:
                raise AssertionError("missed "+label)
        reset()
        mutated=json.loads((root/MANIFEST).read_text())
        mutated["blocks"][0],mutated["blocks"][1]=mutated["blocks"][1],mutated["blocks"][0]
        (root/MANIFEST).write_text(json.dumps(mutated))
        try:
            verify(root,original)
        except ValueError:
            print("detected reordered manifest blocks")
        else:
            raise AssertionError("missed reordered manifest")
        reset()
        print("self-test:",json.dumps(verify(root,original),sort_keys=True))

def configure(commit, digest, mapping):
    global COMMIT, SHA, SOURCE_LINES, AREAS, CORE
    require(re.fullmatch(r"[0-9a-f]{40}",commit) is not None, "source commit must be an exact SHA")
    require(re.fullmatch(r"[0-9a-f]{64}",digest) is not None, "source digest must be SHA-256")
    require(type(mapping["source_lines"]) is int and mapping["source_lines"] > 0, "source lines")
    areas=tuple(tuple(v) for v in mapping["areas"])
    core=tuple(tuple(v) for v in mapping["core"])
    require(areas and areas[0][0] == 1, "section map must start at line 1")
    require(all(type(a) is int and 1 <= a <= mapping["source_lines"] and re.fullmatch(r"[a-z0-9-]+",n)
                for a,n in areas), "invalid area map")
    require([a for a,_ in areas] == sorted({a for a,_ in areas}), "area starts must increase")
    require(all(type(a) is int and type(b) is int and 1 <= a <= b <= mapping["source_lines"]
                and re.fullmatch(r"[a-z0-9-]+",n) for a,b,n in core), "invalid core map")
    require(len({n for _,_,n in core}) == len(core), "duplicate core labels")
    require([n for _,_,n in core] == ["chat-git-and-v2","purpose-and-environment","invariants","decisions"],
            "core map must retain the four named guide sections")
    COMMIT,SHA,SOURCE_LINES,AREAS,CORE=commit,digest,mapping["source_lines"],areas,core

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    for name in ("dry-run","apply","check","self-test"):
        mode.add_argument("--"+name,action="store_true")
    parser.add_argument("--source-commit", help="exact inspected source commit")
    parser.add_argument("--source-sha", help="inspected CLAUDE.md SHA-256")
    parser.add_argument("--section-map", type=Path, help="inspected JSON: source_lines, areas, core")
    args=parser.parse_args()
    overrides=(args.source_commit,args.source_sha,args.section_map)
    if any(overrides):
        require(all(overrides), "source override requires commit, SHA-256 and section map together")
        configure(args.source_commit,args.source_sha,json.loads(args.section_map.read_text()))
    elif not args.apply and (ROOT/MANIFEST).exists():
        recorded=json.loads((ROOT/MANIFEST).read_text())
        configure(recorded["source_commit"],recorded["source_sha256"],recorded["section_map"])
    original=source()
    outputs,m=build(original)
    if args.dry_run:
        print(json.dumps(dict(source_bytes=len(original),source_lines=SOURCE_LINES,
            guide_bytes=len(outputs["CLAUDE.md"]),notes=len(m["notes_sha256_at_migration"]),
            blocks=len(m["blocks"]),paths=sorted(outputs)),indent=2))
    elif args.apply:
        require((ROOT/"CLAUDE.md").read_bytes()==original,"working source changed")
        for path in outputs:
            require(path=="CLAUDE.md" or not (ROOT/path).exists(),"existing destination: "+path)
        self_test(outputs,original)
        write(ROOT,outputs)
        print("applied:",json.dumps(verify(ROOT,original),sort_keys=True))
    elif args.self_test:
        self_test(outputs,original)
    else:
        print("verified:",json.dumps(verify(ROOT,original),sort_keys=True))

if __name__=="__main__":
    main()