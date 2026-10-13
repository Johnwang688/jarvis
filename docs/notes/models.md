# Models — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 7177–7198

<!-- claude-source:007177-007198:begin -->
## Model facts (verified 2026-07-30 via the OpenRouter catalog)

| Model | Tool calling | Vision | Context |
|---|---|---|---|
| `openai/gpt-5.6-luna` | 8/8 | yes | 1.05M |
| `openai/gpt-oss-20b` | 8/8 | **no** | 131k |
| `qwen/qwen3.7-flash` | 8/8 | yes | 1M |
| `mistralai/mistral-nemo` | 6/8 (fails multi-step) | no | 131k |
| `meta-llama/llama-3.1-8b-instruct` | 6/8, flails | no | 131k |
| `google/gemma-3-12b-it` | 2/8 — emits ` ```tool_code ` text, not native calls | yes | 131k |
| `inclusionai/ling-2.6-flash` | 8/8 | no | 262k |
| `deepseek/deepseek-v4-flash-0731` | not run | **no** | 1.05M |
| `deepseek/deepseek-v4-pro` | not run | **no** | 1.05M |

Two findings worth keeping:

- **Cheap per token ≠ cheap per task.** llama-3.1-8b is priced below Luna and
  cost 65% *more* on the bench, because wasted turns re-send the whole
  transcript. Optimize `$/completed task`.
- **Tool-calling support varies by provider**, not just by model. Gemma's
  behavior depended on which backend OpenRouter routed to.

<!-- claude-source:007177-007198:end -->

## Original CLAUDE.md lines 9357–9445

<!-- claude-source:009357-009445:begin -->
**Reasoning effort is set explicitly, and clamped per model (2026-08-22).**
Luna's own default effort is **medium**; the orchestrator now asks for `max`
(`config.REASONING_EFFORT`, env `JARVIS_REASONING_EFFORT`, `""`/`default` hands
the decision back to the provider). The orchestrator is the call that plans,
picks tools and recovers from errors — the one place in this system where a
better answer is worth more than a faster one.

Three things decide how it behaves, and each was checked against the live API
rather than assumed:

- **Scope is the agent loop, not every call.** `llm.chat` sends nothing unless
  a caller passes `effort=`, and the only caller that does is `run_turn` (plus
  the exhaustion handoff). The cheap and worker tiers do bulk text nobody
  reasons about — measured on gpt-oss-20b, asking for effort at all took a
  5-token completion to 80 — so `delegate("…", tier="cheap")` stays untouched.
  Sub-agents inherit it for free, because they are Agents.
- **The ladder is not the same on every model**, so `models.effort_for()`
  clamps to what the model publishes in `reasoning.supported_efforts`: Luna
  goes to `max`, gpt-oss-20b stops at `high`, and a model with no reasoning
  block gets nothing at all rather than a parameter that reads as though it did
  something. It reads the **already-cached** catalog and never fetches —
  `cached_info()` exists precisely so a model call cannot block on the catalog
  endpoint to find out how hard to think.
- **A cold cache still thinks hard.** An effort a model does not publish is
  accepted and clamped upstream, not refused — verified live: `max` on
  gpt-oss-20b returned 200 and spent *fewer* reasoning tokens than `high` did.
  So an unknown model gets the requested effort rather than none, because
  thinking less because a cache is cold is the worse failure. A *typo* is the
  opposite case and is dropped with a one-time warning, since a garbage enum
  value is the shape that could 400 every request for the rest of the run.

**Effort is settable per model, from the picker (2026-08-23).** Each roster row
carries a control listing that model's *own* advertised levels, plus AUTO —
which names what the global default resolves to on that model, because on a
model whose ladder stops short of `max` those are different numbers. The pin
lives in `models.json` beside the roster and beats `config.REASONING_EFFORT`;
AUTO clears it, which is the way out of any choice made there.

Three rules, each the opposite of what a global default does:

- **A pin is refused off-ladder rather than clamped.** Clamping is right when a
  global default meets a model that cannot reach it; this is a choice made
  about one named model, and storing something other than what was asked would
  leave the picker showing a level nobody selected.
- **A model with no reasoning control cannot be pinned at all**, and gets no
  control drawn. Storing a pin `effort_for` would then ignore is the worst of
  both.
- **A pin does not outlive its model.** Removing a model drops its override, so
  re-adding it later starts from the default rather than resurrecting a setting
  the owner made once, months ago.

**BYOK routing is automatic, and it broke every dollar budget (found and fixed
2026-08-23).** The owner added a Moonshot key to OpenRouter and asked whether
requests route to it. They do, with no code change — verified live on
`moonshotai/kimi-k3`: `is_byok: true`. But the same response carried `cost: 0`,
because `usage.cost` is what OpenRouter billed **to your credits**, and a BYOK
call spends on the provider account instead. `llm.Reply.cost_usd` read that
field directly, so on a BYOK model the goal runner's dollar ceiling (which
parks a runaway goal on *spend*), the HUD's SESSION COST and every bench cost
column would all have read zero while real money moved — a goal could only ever
park on slices or hours.

`llm._cost()` now takes `max(usage.cost, cost_details.upstream_inference_cost)`.
A **max, not a sum**, and the measurement is the reason: on a credit-billed call
the two fields are *the same number* (Luna: both 9e-06), so adding them would
double count; on a BYOK call they are 0 and the real figure (kimi-k3: 0 and
0.0006264). Generalises past this one field — **a number that arrives as zero
is not the same as a number that is zero**, and every ceiling in this system is
denominated in the one that was wrong.

**And the fix to the model selector that the owner's own first use exposed.**
`models.tier()` reads `~/.config/jarvis/models.json`, so the moment a real
selection existed, `tests/context_check.py` and `tests/fleet_check.py` — which
assert that children run on `config.TIERS[...]` — started failing against
whatever was picked in the HUD. Both point `config.MODELS_PATH` at a temp file
now. The lesson is the allowlist lesson with the arrow reversed: a suite must
not **read** live machine state any more than it may write it, and adding a
resolution layer under a value silently hands every test that asserts on it a
new dependency.

**The trade, stated plainly: this buys quality with latency.** A/B on the same
two-step task, same tools (2026-08-22): default 2.8s / $0.00169, max 5.5s /
$0.00144 — roughly **2x the wall clock**, with cost a wash on that sample
(one sample is not a verdict). On the voice surface that is time the owner
waits through, so `JARVIS_REASONING_EFFORT=medium` is the knob if the HUD
starts to feel slow. It also means **bench numbers taken before this change
are not comparable to numbers taken after it** — pin the variable when
re-running agent-bench or long-bench against the older baselines above.

<!-- claude-source:009357-009445:end -->

## Original CLAUDE.md lines 9492–9596

<!-- claude-source:009492-009596:begin -->
**Model selector shipped (2026-08-22)** — which model Jarvis runs on is now a
choice made in the window, not an edit to `config.py`. `models.py` holds both
halves: the **catalog** (every OpenRouter model that can run this loop, fetched
from the public `/api/v1/models` listing and cached at
`~/.cache/jarvis/openrouter-models.json`) and the **roster** (the owner's
shortlist plus which entry is selected, at `~/.config/jarvis/models.json` —
machine-local state chosen through the HUD, so it lives beside the allowlist
and the avatar pointer). In the HUD the **CORE row opens it**: the shortlist,
with ADD MODEL opening the full catalog behind a search box and two filters.

**`models.tier(name)` is the new resolution point, and that is the thing to
remember.** `config.TIERS` is still where the defaults live, but anything that
reads it *directly* silently ignores the owner's selection — the same shape of
failure as iterating `tools.REGISTRY` after deferred groups shipped (invariant
10): not an error, just the mechanism quietly not working. `agent.Agent`,
`delegate`, `_summarize`, `run_subagent`, `command_review`, the face's
`/config` and the daemon's `/status` were all moved onto it.

Which tiers move is decided by the configuration itself: **a tier whose
configured model *is* the configured orchestrator follows the selection**, and
one pointed somewhere else stays put. That is exactly the set that was already
following it — subagent, compaction, review — so "every child is the
orchestrator tier" stays true after a switch, while `worker` and `cheap` keep
their own model. The selection also beats an explicit `JARVIS_ORCHESTRATOR`,
on the avatar precedent: an environment variable is a default and a click made
a moment ago is not. Anything that must not move passes `model=` explicitly.

Four decisions worth not relitigating:

- **Eligibility is a refusal, not a badge.** The catalog keeps only models
  whose `supported_parameters` include `tools` — this is a tool-calling loop,
  and a model that cannot emit `tool_calls` cannot read a file or ask for an
  approval; it can only talk. So `models.add` validates against the catalog and
  refuses, because an id that reaches the roster is one the owner can select,
  and the resulting failure would surface a turn later with nothing pointing
  back at the picker. A catalog that cannot be reached is a refusal too, not a
  shrug. Also dropped: models with no text output, `openrouter/auto` (a router
  priced at -1 — the readout would name a model that is not the one answering),
  and **`<model>:batch`**, which is the same model addressed through the
  asynchronous batch endpoint and cannot be waited on by a turn. The tell for
  batch is the price: 55 of the 60 variants with a base model are listed at
  *exactly* half its completion price (checked live 2026-08-22). 290 of 421
  catalog entries survive all of that.
- **The intelligence numbers are reported, never computed.** OpenRouter
  republishes Artificial Analysis's `intelligence_index` inside its own catalog
  for 117 of the 290 eligible models, and that is what the IQ badge and filter
  read. An invented score in a picker reads exactly like a measured one. The
  corollary is that **unrated is `None`, never 0** — a zero would sort and
  render as "measured and terrible" — so unrated models sort last, and a
  minimum-IQ filter *hides* them, which the note under the list says out loud.
- **The agent has no tool for this.** He can change his avatar and his voice,
  but not the model he thinks with: it is the one setting that changes his own
  judgement and his own cost, and a model that could switch itself could switch
  itself cheap. Same reasoning as the MIC mute having no tool.
- **A switch mutates the live agent rather than rebuilding it.** Which model
  answers is a property of the *next request*, not of the conversation, so
  unlike `/session` it must not cost the transcript. `/model` takes
  `_agent_lock` for the same reason `/session` does — a switch requested
  mid-turn lands after that turn instead of changing models underneath one.

Degradation is one-directional, toward showing something: a failed fetch serves
the disk cache however old it is and the picker prints why it is stale (a list
drawn from a two-week-old cache with no sign of it is how a missing model
becomes a mystery), a 200 that parses to nothing eligible keeps the previous
list, and only an empty cache becomes an error the picker has to render. The
catalog is sent whole (~129KB for 290 entries) and filtered in the window,
because a search box that round-trips per keystroke is one nobody types into;
the row cap states what it dropped, per the no-silent-caps rule.

Known gap: the roster and the selection are reachable only from the HUD, so an
owner who picks a bad model and closes the window changes it back by reopening
the window (CONFIG DEFAULT is the first row) or by editing
`~/.config/jarvis/models.json`. A `jarvis model` CLI is the obvious follow-up
and was left out rather than guessed at.

**The HUD's choice is the default, and any model unpins (2026-10-08).** The
owner could not remove a pinned model in the v2 HUD (no control), could not
remove the env model at all (`_load` re-seeded it on every read), and read
the default as env-only. Now: the v2 Model picker's "Set as default" is
`select()` — persisted, beating `JARVIS_ORCHESTRATOR`, followed by every
default fast-path thread — and the env model is labelled "config default
(JARVIS_ORCHESTRATOR)", used only while nothing is selected, with "Reset to
config default" (`select("")`, which re-lists it). Every row has an × and
the catalogue's pinned rows say Unpin. **The rule that holds it together:
the effective default is always on the roster.** `remove()` raises
`RosterRefused` (409, its sentence shown in the picker) for the last model
and for any removal that would leave the effective default unlisted
("choose another default first"); an unpinned env model is remembered as
`removed_default` (naming the model, so a changed env var still seeds), and
`_load` re-seeds it anyway whenever nothing is selected. `describe()` gained
`default_source`. The v2 route maps `RosterRefused`/`NotEligible`/
`LookupError` to `fail()` because the daemon reflects only an `APIError`'s
text — a bare exception reads "request failed (RosterRefused)".
Review round (same day): 404 is `NotOnRoster` only (a bare `LookupError`
also caught real bugs' `KeyError`s); the refusal rule is the pure
`models.removal_refusal`, which the HUD mock imports instead of copying;
`describe()` reads the roster **once** (it used to re-read per field and per
row, so a concurrent change could make `selected` and `current` disagree);
`_load` treats a non-UTF-8 file as corrupt (it raised out of `effort_for`,
i.e. every v1 turn); `_save` is atomic; an `effort` beside add/remove is a
400; × on the HUD-chosen default succeeds but says the default fell back;
and the picker's "Pin a model…" opens the catalogue pin-only on top of it.
The no-tool guards (`fastpath_check`, `models_check`) cover every roster
write.

<!-- claude-source:009492-009596:end -->
