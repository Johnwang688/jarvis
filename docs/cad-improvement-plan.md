# CAD improvement plan

Draft 2026-08-05. A handoff document: everything a fresh Claude Code session
needs to make Jarvis's Onshape CAD work hold up on assemblies more complex
than "insert two parts and move one."

**Status 2026-08-05 (later the same day): items 1–3 are DONE.** cad-bench
shipped (`jarvis/cadbench.py` + `tests/cadbench_check.py`, 55 synthetic
checks green), `skills/cad.md` exists, and the readback upgrade landed with
`tests/onshape_check.py` extended and a live read-only smoke
(`tests/probe_readback_live.py`) confirming angles + world extents on real
assemblies. Endpoints were verified live first (`tests/probe_cadbench_api.py`,
`tests/probe_cadbench_delete.py`) — findings folded into CLAUDE.md. **Next
step: a human-run baseline sweep (`jarvis bench --family cad`), then item 4
(mates), judged against that baseline.**

**Read `CLAUDE.md` first** — especially *Invariants*, *Safety design* (the
Onshape sandbox pin), and the Onshape section of *Current state*. This plan
assumes those rules and does not restate them.

## The problem, stated honestly

CAD works on basic tasks and degrades badly on complex ones. That is the
owner's observation, not a measurement — **there is no cad-bench**, so nobody
can currently say whether the cause is the model, the prompt, or the tools.
Work item 1 exists to fix that before anything else is tuned.

The diagnosis below was done 2026-08-05 by inspection of `jarvis/tools/
onshape.py` (485 lines, 8 `cad_` tools) and the system prompt. Three gaps
found, none of which a better model would fix.

### Gap 1 — the readback cannot describe the state it reports

`cad_assembly` returns, per instance: id, part name, position in inches, and
a bare `rotated` boolean (`_is_rotated`). It does **not** return:

- the actual rotation (which axis, how many degrees) — only *that* it is rotated
- any part dimensions, extents, or bounding box

This is the direct cause of the failure already recorded in CLAUDE.md: two
35-hole c-channels were inserted 12" apart and overlapped, because nothing in
any tool result said the rails are 17.5" long. The render caught it; the text
channel — which CLAUDE.md correctly calls "ground truth" — could not, because
the ground truth omits the geometry. A model cannot compute clearance from
data it was never given, so on a complex assembly it is guessing, and the
guesses compound.

### Gap 2 — there are no mates

Zero mate-connector support (grep for `mate` in `tools/onshape.py` returns
nothing). Every part is placed by absolute 4×4 matrix in root-assembly
coordinates. For two parts that is fine. For a real assembly, each part's
correct position is a function of other parts, there is no constraint solver,
and one change cascades into redoing every dependent placement by hand — with
no way to express "this face against that face" as an invariant that survives
the next move. This is a missing capability, not a prompting failure.

### Gap 3 — zero CAD guidance reaches the model

Verified 2026-08-05: the 5,523-char `config.SYSTEM_PROMPT` contains no
occurrence of `CAD`, `cad_`, `Onshape`, `render`, `assembl`, or `inch`, and
there is no CAD skill in `skills/`. The model receives nine tool docstrings
and nothing else — no workflow discipline, no conventions, no accumulated
lessons.

Everything hard-won about this integration lives in `CLAUDE.md`, which the
agent never reads. So Jarvis rediscovers the local-frame axis convention by
making the same mistake every session. Compare the browser, which got an
explicit snapshot→act→re-snapshot discipline that CLAUDE.md notes "held
without prompting beyond the task text." CAD has no equivalent.

## Work items, in dependency order

Do them in this order. Each is independently shippable, and items 2–4 are
unverifiable without item 1.

### 1. `cad-bench` — a fourth bench family

`jarvis/cadbench.py`, CLI `jarvis bench --family cad`, registered in
`FAMILIES` in `jarvis/__main__.py` alongside `tools` / `vocab` / `agent`.

Follow **agent-bench's scoring philosophy exactly** (`jarvis/agentbench.py` is
the model to copy): *grade the world the agent left behind, not the prose it
wrote.* Every check reads the resulting assembly back from Onshape — instance
count, positions, rotations, clearances — never the model's summary of what it
claims to have built.

Inherited rules, all of which `tests/agentbench_check.py` already enforces for
agent-bench and which `tests/cadbench_check.py` must enforce here:

- every task scores **near zero on an empty run** (the single most important
  check — an early agent-bench safety grader paid 56% for doing nothing)
- checks are tagged by category so one task feeds several ratings
- a conjunctive check says so in its name ("built it *and* left the library
  alone"), or a reader misreads the failure
- report variance; a single run is a sample, not a verdict

Suggested task ladder, easy → hard, each scored on readback:

| task | what it puts under load |
|---|---|
| `place` | insert one part at a stated position/rotation; readback matches |
| `pair` | two parts at a stated offset **without overlapping** — the c-channel case, regression-tested |
| `frame` | 4+ parts forming a rectangular frame; corner positions and no interpenetration |
| `revise` | build, then "move the left rail out 2 inches" — does the rest stay correct? |
| `repair` | hand it a deliberately broken assembly (one part 90° wrong) and ask it to fix it |

`revise` and `repair` are the ones that should discriminate — they are where
absolute-placement-without-mates is predicted to fall apart, so they also
measure whether work item 4 paid off.

**Sandbox hygiene is the hard part.** agent-bench gets a temp cwd; CAD writes
to a real Onshape document. Options, in order of preference:

1. bench-owned assemblies inside the existing pinned sandbox, named
   `cadbench-<task>-<runid>`, deleted in a `finally:` (must survive an
   exception — `tests/agentbench_check.py` tests exactly this property for
   the agent-bench sandbox)
2. if per-run document creation is cheap on the owner's plan, a throwaway doc

Whichever is chosen, **the write pin must not be weakened.** `tests/
onshape_check.py` asserts every write URL targets the pinned sandbox
document; that test must still pass unchanged. If the bench needs a different
document, it changes the pin through `onshape_auth`, never by letting a
`cad_` tool accept a document id.

This bench costs real API calls and real Onshape quota — say so in the CLI
help, the way `jarvis bench` already warns that the tools family costs money.

### 2. The CAD skill

`skills/cad.md`, following the existing skills pattern (frontmatter
`description` written as a *trigger* — "Use when the owner asks to build,
modify, or inspect a CAD assembly…"). It reaches the model through the
two-tier index, so keep the body focused; the index only carries the
description.

Content it must carry, all of it already known and currently only in
CLAUDE.md:

- **Placement is absolute**, in root-assembly coordinates, inches and degrees.
  There is no constraint solver — until item 4 lands, nothing holds two parts
  together after a move.
- **Local frames differ from the assembly frame.** VEX c-channels run
  lengthwise along **Y** in their local frame. Always read a part's extents
  before choosing an offset.
- **Verification discipline: insert → `cad_assembly` readback → `cad_render`.**
  Readback in inches is ground truth; the render is the check that catches
  what the numbers do not say. Render after every few changes, not at the end.
- **VEX lives on a 1/2" grid** — prefer placements on it.
- Work incrementally; verify each part before adding the next. A wrong early
  placement propagates into every later one.
- Parts libraries are **read-only**; all writes go to the sandbox.

Write this as instructions to follow, not background reading.

### 3. Readback fidelity (fixes Gap 1)

Two changes to `jarvis/tools/onshape.py`:

- **`cad_assembly` reports actual rotation**, not a boolean. Decompose the
  transform's rotation block into the same fixed-frame X→Y→Z degrees that
  `cad_insert` / `cad_move` accept, so the number read back is the number that
  can be typed straight back in. Keep `_is_rotated` for the "no rotation" fast
  path if useful, but the output must state the angles.
- **Part extents are surfaced.** Bounding box in inches, ideally on both
  `cad_find_part` (so size is known *before* placing) and `cad_assembly` (so
  clearance is checkable after). Onshape exposes bounding boxes per part
  studio / part; confirm the exact endpoint against the live API before
  building the request — the CLAUDE.md Onshape notes list several places the
  generated client docs are wrong.

This is the cheapest item with the highest expected effect: it converts
overlap from something only a human eye catches into something the text
channel states.

### 4. Mate connectors (fixes Gap 2)

The real capability gap, and deliberately last — item 1 must exist first so
its value is measurable, and items 2–3 may absorb more of the failure than
expected.

Design constraints inherited from the existing tools:

- **No `cad_` tool may accept a document id for a write target.** That
  structural confinement is why no CAD tool needs `dangerous=True`, and
  `tests/onshape_check.py` asserts it. Mate tools follow the same rule:
  assembly eid plus instance ids, never a document.
- Mate connectors are referenced by part + connector id, so the discovery
  half (`cad_mate_connectors(part_ref)` or similar, listing available
  connectors) is as important as the mating half. A model cannot mate to a
  connector it cannot enumerate.
- Verify the request body shape against the live API before writing the
  tool. CLAUDE.md records two specific traps already paid for in this area:
  `shadedviews` **rejects** named views despite the generated docs, and
  `occurrencetransforms` takes the flat `{occurrences, transform,
  isRelative}` body while the `transformDefinitions` wrapper belongs to
  `/modify` and 400s.

## Onshape API facts already paid for — do not rediscover these

From CLAUDE.md, all verified live 2026-07-31:

- Cross-document inserts must reference a **version** of the source document;
  `cad_find_part` resolves each library's latest version, and a
  never-versioned library is unusable.
- Insert-with-placement is a single call: `transformedinstances`, a 16-float
  **row-major** matrix, **meters**, absolute in root-assembly coordinates.
  (`_INCH = 0.0254`; the tools speak inches/degrees and convert internally.)
- `shadedviews` requires a 12-float view matrix and rejects named views —
  hence the `_VIEWS` table in `onshape.py`. `pixelSize=0` means zoom-to-fit.
- `occurrencetransforms` body shape as above.
- The official VEX V5 libraries are found by **description**
  (`q='description:"Official VEX V5 Library"'`, `filter=4`, then keep only
  `owner == "Onshape"`). A name search surfaces user copies and misses the
  real ones.
- API keys live under My account → Developer → API keys. The dev-portal URL is
  OAuth-apps only now. Individual accounts cap at 2 active keys.

## Invariants this work must not break

- **Tool schemas are generated from type hints** (`Annotated[str, "…"]`).
  Never hand-write JSON schema.
- **Tool failures return as text, never raise.** Every `cad_` tool already
  catches `AuthError` / `_CadError` / `httpx.HTTPError` and returns a string
  the model can act on. New tools do the same.
- **Image-producing tools return `ToolResult(text=…, image_b64=…)`** and the
  loop attaches the image as a separate user message. `cad_render` is the
  existing example.
- **The sandbox write pin**, above — the property `tests/onshape_check.py`
  guards.
- The key bundle (`~/.config/jarvis/onshape_keys.json`) stays covered by all
  three secrets layers; nothing in this work should print or log it.

## Testing

- `tests/cadbench_check.py` — free synthetic checks for the new harness, in
  the shape of `tests/agentbench_check.py`: graders score a hand-built correct
  world 100% and catch their specific failure, every task scores ~0 on an empty
  run, and the sandbox cleanup runs even on exception.
- `tests/onshape_check.py` — must keep passing unchanged. Extend it for items
  3 and 4 against the existing fake transport (request shapes, the rotation
  round-trip, and the write pin for any new write tool).
- Live validation is human-run, like the original CAD validation was.

## Out of scope

- **Do not swap the model as part of this work.** A 2026-08-05 sweep confirmed
  `openai/gpt-5.6-luna` still wins on agent-bench and vocab-bench (see the
  challenger table in CLAUDE.md), and both DeepSeek V4 models are text-only,
  which makes them non-starters for CAD. Model comparison for CAD is a
  *follow-up*, run against cad-bench once it exists — the candidates are
  `openai/gpt-5.6-terra` and `google/gemini-3.1-pro-preview`. Published
  research puts general-purpose VLMs at only 33–38% on CAD-drawing
  understanding, so do not expect a model swap to rescue a tooling gap.
- Whiteboard→CAD wiring and configurable cut-to-length parts (the
  "(Configurable)" library documents) remain on the backlog, untouched here.
