# Integrations — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 7903–7935

<!-- claude-source:007903-007935:begin -->
**Gmail shipped (2026-07-31)** — the first real integration, and the
template for the rest: `jarvis auth google <client.json>` runs the one-time
OAuth consent (PKCE + state, loopback redirect, human-only CLI command),
writes the scoped refresh token to `~/.config/jarvis/google_token.json`
(600), and `tools/gmail.py` exposes `gmail_search` / `gmail_read` /
`gmail_send` — send is `dangerous=True`, so it hits the CLI y/N or the HUD
authorization card. `google_auth.access_token()` refreshes behind a cache;
tools retry once on 401. Token bundle covered by all three secrets layers
(name, glob, scrub — see Safety design). Free synthetic suite:
`tests/gmail_check.py`. Setup steps live in `jarvis auth google` (no
argument). Google-side gotchas: the OAuth app must be **published to
production** or refresh tokens die after 7 days; a re-consent needs the old
grant revoked at myaccount.google.com/permissions or Google returns no new
refresh token. Validated end-to-end by the owner 2026-07-31: a gated send
(draft → CLI y/N showing exact to/subject/body → delivered) and a 7-day
inbox summarization (parallel gmail_reads, $0.0017). Note that mailbox
content now flows through the orchestrator tier, so the model routing
policy applies to it.

**Internet access opened (2026-07-31).** `web_search`/`fetch_page` were
already unrestricted; the browser was the localhost-only piece. The default
allowlist is now `["localhost", "127.0.0.1", "*"]` where `'*'` means the
*public* internet: `config.is_lan_host()` refuses private/CGNAT/link-local
space and unresolvable names, judged by what a hostname resolves to (so
`lvh.me` → 127.0.0.1 is not a route to local services or the face). Enforced
on the requested URL and re-checked after goto/click/submit
(`_guard_landing()`); `fetch_page` gained the same LAN block plus a
post-redirect check — it had been wide open to the LAN. System prompt gained
open-web conduct rules (no credentials or personal data into pages, no
outward-facing submissions unless asked). Verified live (example.com loads
and snapshots; 192.168.1.1 refused with a clear message) and synthetically
(`tests/browser/policy_check.py`); approval + secrets suites still pass.

<!-- claude-source:007903-007935:end -->

## Original CLAUDE.md lines 8386–8478

<!-- claude-source:008386-008478:begin -->
**Onshape CAD shipped (2026-07-31), live validation pending.** The second
use-but-never-see integration (see Safety design). `jarvis auth onshape`
pastes API keys created under My account → Developer → API keys (Basic
auth on the wire — no HMAC needed for first-party use; individual accounts
are capped at 2 active keys), then creates or adopts the one sandbox
document Jarvis may write to and records read-only parts-library documents.
`tools/onshape.py`: cad_status / cad_find_part / cad_create_assembly /
cad_insert / cad_assembly / cad_move / cad_delete / cad_render (shaded PNG
via ToolResult.image_b64 — the model's eyes; text readback in inches is
ground truth). API facts verified against the docs, worth keeping:
cross-document inserts must reference a *version* of the source document,
so cad_find_part resolves each library's latest version and errors usefully
on never-versioned docs; insert-with-placement is a single call
(`transformedinstances`: 16-float row-major matrix, meters, absolute in
root-assembly coords); `shadedviews` requires a 12-float view matrix — it
*rejects* named views despite what the generated client docs say, so
cad_render carries a `_VIEWS` name→matrix table — and `pixelSize=0` means
zoom-to-fit; `occurrencetransforms` takes the flat
`{occurrences, transform, isRelative}` body (the `transformDefinitions`
wrapper belongs to `/modify` and 400s). The tools speak inches/degrees
(VEX lives on a 1/2" grid) and convert internally.

**Validated live 2026-07-31** on the owner's account: sandbox created
(public — free plan), and the 14 official VEX V5 library documents
auto-discovered and written into the bundle via the documents-search API
(`q='description:"Official VEX V5 Library"'`, `filter=4` = public, then
keep only owner == "Onshape" — a name search surfaces user copies first
and misses the real ones, which are matched by *description*). Full
pipeline exercised tool-by-tool: find "c-channel" across 14 libraries →
create assembly → insert two 35-hole aluminum c-channels → readback →
move → iso/top renders. The renders earned their keep immediately: a 12"
Y-offset overlapped the 17.5"-long rails (channels run lengthwise along Y
in their local frame), which the readback stated and the render made
obvious; one cad_move fixed it. Still to build: mates via mate
connectors, configurable cut-to-length (the "(Configurable)" library
docs), and whiteboard→CAD wiring. API-key note: keys live under My
account → Developer → API keys (the dev-portal URL is OAuth-apps only
now); individual accounts cap at 2 active keys.

**CAD improvement round 1 shipped (2026-08-05)** — items 1–3 of
`docs/cad-improvement-plan.md`; mates (item 4) deliberately wait until the
bench has measured whether 1–3 absorbed the failure.

- **cad-bench** (`jarvis/cadbench.py`, `jarvis bench --family cad`) — the
  fourth family. Five tasks (place / pair / frame / revise / repair) graded
  the agent-bench way: the assembly is read back from Onshape afterwards and
  scored on instance count, positions, rotations, and clearances computed
  from real part bounding boxes — never the prose. Categories: placement,
  clearance, revision, discipline. Each run builds its own
  `cadbench-<task>-<runid>` assembly in the pinned sandbox and deletes it in
  a `finally:` (a failed cleanup prints the assembly name loudly). The write
  pin is untouched. Costs OpenRouter *and* Onshape quota — the graders make
  API calls too. `pair` is the recorded c-channel regression (a stated gap
  that requires knowing the part is 17.5" long); `revise`/`repair` are the
  tasks predicted to discriminate until mates exist.
  `JARVIS_CADBENCH_TRACE=1` keeps each task's full call sequence and
  transcript under /tmp. Cleanup is verified live: deleting the assembly
  cascades to the BOM element Onshape auto-creates beside it, so a bench
  sweep leaves the sandbox exactly as it found it.

  **Baseline (Luna, 2026-08-05/06):** place ✓ · pair ✓ · frame ✓ ·
  revise ✓ · repair ✗ (2/11, hit the 18-step cap) — 83% overall, $0.0076,
  130s. **The readback upgrade absorbed most of the predicted failure**:
  `pair` — the recorded 12-inch c-channel regression — passed because the
  model computed the gap from the new extents. The `repair` failure did
  not reproduce: an immediate re-run passed 11/11 in 10.7s with the
  textbook sequence (status → readback → one absolute cad_move restating
  position with zeroed rotation → readback → render). Same lesson as
  agent-bench: a single run is a sample, not a verdict. Consequence for
  item 4 (mates): the ladder as it stands no longer demands them — build
  them when a real task does, or when the bench gains a task where a
  moved rail must carry attached parts (which is what mates actually
  solve).
- **Readback fidelity** (`tools/onshape.py`): `cad_assembly` now reports
  actual rotation via `_angles()` — the exact inverse of `_rotation`, same
  fixed-frame X→Y→Z degrees `cad_insert`/`cad_move` accept, gimbal lock
  collapses into rx with rz=0 — plus each instance's **world-frame extents**
  from its source part's bounding box, so overlap is computable from the
  text channel. `cad_find_part` carries each match's local-frame extents
  (which axis is long, and how long) so offsets can be computed before
  placing. Boxes degrade silently to the old output when unavailable.
- **`skills/cad.md`** — the CAD discipline as instructions (absolute
  placement / no solver, local-frame axes, insert→readback→render, the
  half-inch grid, incremental verification, read-only libraries).
- **API facts verified live 2026-08-05** (probes under `tests/probe_*.py`):
  part bounding boxes at
  `GET /parts/d/{did}/v/{vid}/e/{eid}/partid/{pid}/boundingboxes` — flat
  `lowX..highZ` payload, meters; the assembly definition's version key is
  **`documentVersion`**, not `versionId`; element delete is
  `DELETE /elements/d/{did}/w/{wid}/e/{eid}`; part names carry invisible
  LRM marks (`‎`) that must be stripped before matching; the 35-hole
  c-channel measures 17.5" along local Y, origin at one end, X centered.

<!-- claude-source:008386-008478:end -->

## Original CLAUDE.md lines 8613–8631

<!-- claude-source:008613-008631:begin -->
**vercel-deploy skill (2026-08-01).** Build → verify locally in his own
browser → private GitHub repo (`gh repo create --private --source --push`)
→ **stop and ask the owner for the Vercel project name** (it decides
`<name>.vercel.app`) → preview deploy → owner approves the preview → only
then `--prod`, on a fresh yes. Exact commands are written into the skill
(including `--cwd`/`-C` flags instead of `cd`, and the nohup/pkill pattern
for the throwaway local server). Everything routes through run_command's
approval gate; repos are private unless the owner explicitly says public.

**Self-improvement, governed (2026-08-01).** `skills/self-improve.md` lets
Jarvis edit his own codebase when the owner asks: read CLAUDE.md first, git
checkpoint before/after, run the relevant free suites, never report success
past a red test, one change per request (no autonomous loops). The boundary
that makes it safe is mechanical, not prose: **write_file refuses
SELF_PROTECTED** (`tools/files.py`: secrets.py, files.py itself,
tools/__init__.py, approvals.py, permissions.py) — the layers that gate him
change only by the owner's hand or per-approved run_command.
`tests/self_improve_check.py` guards the guard.

<!-- claude-source:008613-008631:end -->

## Original CLAUDE.md lines 9313–9356

<!-- claude-source:009313-009356:begin -->
**Spotify shipped (2026-08-21) — and with it, deferred tool groups.** Ten tools
(`tools/spotify.py`): `spotify_play` (a plain-words query, an exact URI, or a
list of them; resumes when called bare), `spotify_status`, `spotify_search`,
`spotify_playlists`, `spotify_pause`, `spotify_skip`, `spotify_queue`,
`spotify_settings` (volume/shuffle/repeat), `spotify_library` (liked, top,
recent) and `spotify_artist_tracks`. Setup, credential handling and the two
confinements are in *Safety design* above; `jarvis auth spotify` with no
argument prints the whole dashboard walkthrough.

Four API facts this is built around, each checked against Spotify's current
documentation rather than remembered, because three of them are places a
reasonable guess is wrong:

- **The redirect URI must be `http://127.0.0.1:8406/callback`, literally.**
  Spotify requires HTTPS except for a loopback address and **rejects the
  spelling `localhost`** outright. The port is therefore fixed rather than
  ephemeral like the Google flow's, because the owner registers this exact
  string by hand.
- **Playback control is Premium-only.** Every `/me/player/*` write 403s on a
  free account, so `_fail` names the plan — a bare "403 Forbidden" reads as a
  Jarvis bug and sends the owner to the wrong place entirely.
- **Spotify's recommendation endpoints are gone for new apps.**
  `/recommendations`, `/related-artists`, `/audio-features` and the editorial
  playlist endpoints were retired on 2024-11-27 for apps registered after that
  date, which ours is; they 403 with no deprecation notice. So "play something
  like X" is `spotify_artist_tracks` blended with the owner's own top and saved
  tracks, and the tool text says that is what it is rather than passing it off
  as Spotify's recommender. Tutorials still reference the old endpoints because
  pre-2024 apps kept access.
- **A device must exist before anything can play**, and a freshly-launched
  client takes seconds to register with Spotify Connect. `_pick_device` launches
  and polls rather than returning an error the owner has to act on, and names
  the device it chose, because "playing on the desktop because your phone was
  the only other option" otherwise reads as picking at random.

**The tool-group mechanism is the more reusable half** (invariant 10). Ten more
tools would have cost ~1,318 tokens of JSON schema on *every* request forever —
11% of a registry that already weighs ~11k before anything is said. It now
costs 0 when Spotify was never connected, 525 when it is, and the full 1,318
only in a conversation that actually turned into one about music. Nothing else
uses groups yet; `gmail`, `onshape`/`cad_*` and `desktop_*` are the obvious
candidates and are left alone until someone measures whether the round trip is
worth it for them.

<!-- claude-source:009313-009356:end -->

## Original CLAUDE.md lines 9597–9623

<!-- claude-source:009597-009623:begin -->
**Schoolwork integration shipped (2026-08-23).** The owner's Windows-side
schoolwork dashboard (`C:\myday\schoolwork` — Canvas + MySchoolApp merged into
one local SQLite board, read-only toward the school systems, with an agent
surface of its own: `sw review --json`, `sw prep --json`, a triage contract and
an AGENTS.md) is reachable from Jarvis via `skills/schoolwork.md` plus a WSL
wrapper at `~/.local/bin/sw`. Decisions, so they are not relitigated:

- **A skill driving the existing CLI, not a tool group.** The module already
  speaks agent — typed tools would re-describe a surface that exists. Same call
  as the wharton skill; revisit as a deferred group (invariant 10) only if
  measured usage says the round trips are worth it.
- **The wrapper exists for the approval gate, not convenience.** The raw
  invocation is `cmd.exe /c sw.cmd …`, and an ALWAYS on that mints a `cmd.exe`
  allowlist entry — arbitrary Windows execution, the `git -c` shape again. The
  wrapper's stem is `sw`, so the allowlistable unit is exactly this tool. First
  use asks; one ALWAYS covers it.
- **The DB is unreachable from WSL, verified by execution**: it is WAL-mode
  SQLite, and over the 9p mount even a `mode=ro` open fails with
  `disk I/O error` (WAL's shared-memory sidecar does not survive 9p). The CLI
  is the only channel — do not point the sqlite tool at it.
- **Network-touching subcommands stay owner-triggered** (`sync`, `login-msa`,
  `msa-probe`) — the module's own rule, carried into the skill. Sync already
  runs on a Windows scheduled task.
- **This is not the Membean case.** The dashboard tracks work and never
  submits; Jarvis plans, reports and builds study materials on request, and
  doing or submitting the schoolwork itself stays declined.

<!-- claude-source:009597-009623:end -->
