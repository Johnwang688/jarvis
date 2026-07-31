# Jarvis

A personal agent with a hand-rolled tool-calling loop, routed through OpenRouter
so any model — frontier or open-weight — is a one-line config change.

## Setup

```bash
cp .env.example .env      # add your OpenRouter key
jarvis config             # confirm model tiers
jarvis tools              # see what it can do
jarvis                    # start talking
```

Already installed: `~/.local/bin/jarvis` points at this repo's venv, so edits to
the source take effect immediately.

## Commands

| Command | What it does |
|---|---|
| `jarvis` | Interactive session |
| `jarvis ask "..."` | One prompt, then exit |
| `jarvis bench [models...]` | Stress-test models on tool calling |
| `jarvis tools` | List registered tools |
| `jarvis config` | Show model tiers |

## How it works

**The loop** (`agent.py`) is the whole idea and it is ~40 lines: ask the model,
run whatever tools it asked for, hand the results back, repeat until it stops
asking. Three details matter and everything else is bookkeeping — append the
assistant turn verbatim so its `tool_call` ids survive, return every result in
one batch keyed to those ids, and return tool *failures* as text so the model
can correct itself instead of the loop crashing.

**Tools** (`tools/`) are plain Python functions. The `@tool` decorator derives
the JSON schema from type hints, so there is no schema to maintain by hand:

```python
@tool
def read_file(path: Annotated[str, "Path to the file"]) -> str:
    """Read a UTF-8 text file from disk."""
```

Two design rules borrowed from good harnesses: tools are **narrow and typed**
rather than one god-tool (so the loop can gate, log, and render each one), and
side-effecting tools are marked `dangerous=True` and require your approval at
the prompt. `write_file` also enforces read-before-write, so the agent can never
clobber a file it hasn't seen.

**Memory** (`tools/memory.py`) is one markdown file per fact under `memory/`.
Plain files mean you can read, grep, and version-control what it believes about
you. No database, no embeddings.

**Routing** (`config.py`) names three tiers — `orchestrator`, `worker`, `cheap`.
The orchestrator runs the loop; `agent.delegate()` hands bulk text work down to
a cheaper tier. That split is deliberate: cheap models are fine at drafting
prose and bad at multi-step tool calling, so keep them off the driver's seat.

## Benchmarking

```bash
jarvis bench                                  # the default cheap roster
jarvis bench openai/gpt-5.6-luna              # one model
jarvis bench -t chain -t recovery <model>     # specific tasks
```

Eight tasks, each targeting a way cheap models actually fail:

| Task | Tests |
|---|---|
| `abstain` | Knows when *not* to call a tool |
| `single` | One no-arg call |
| `args` | Passes a string argument correctly |
| `decoy` | Picks the right tool from six |
| `parallel` | Two independent calls |
| `chain` | Feeds one tool's output into the next |
| `recovery` | Recovers from a tool error instead of giving up |
| `enum` | Fills an enum field plus multiple required args |

Tools in the bench are fakes with canned results — no filesystem, no network.
Output is a per-model table plus a summary ranked by pass rate then cost.

## Layout

```
jarvis/
  agent.py      the loop
  llm.py        OpenRouter client (the only file that knows about HTTP)
  config.py     model tiers, paths, system prompt
  bench.py      tool-calling stress test
  tools/        clock, files, memory, shell, web
memory/         one markdown file per remembered fact
```

## Browser control

Playwright drives Chromium from inside WSL. **Headed by default** — WSLg renders
the window on the Windows desktop, so you watch the agent work in real time and
close the window to stop it. `JARVIS_BROWSER_HEADLESS=1` for batch runs,
`JARVIS_BROWSER_SLOWMO` to pace the actions (default 250ms).

Every session writes a Playwright trace to `traces/`. Review any run frame by
frame, with before/after DOM snapshots:

```bash
.venv/bin/playwright show-trace traces/<name>.zip
```

Two ways for the agent to perceive a page, which is what makes text-vs-vision
benchmarking possible on identical tasks:

| Tool | Channel | Works with |
|---|---|---|
| `browser_snapshot` | Text: element refs + page text | Any model |
| `browser_screenshot` | PNG image (~1.3k tokens) | Vision models only |

Three controls are structural rather than advisory:

- **Allowlist** — navigation off-list is refused. Default `localhost`, `127.0.0.1`.
- **Clean context** — a fresh profile per run, never your real Chrome. No
  cookies, no logins, so a confused agent has no credentials to misuse.
- **Action budget** — 120 actions per session, then it must stop and report.

First-time setup needs Chromium's system libraries:

```bash
sudo .venv/bin/playwright install-deps chromium
```

Web search uses Brave when `BRAVE_API_KEY` is set, DuckDuckGo otherwise (with a
fallback to DDG's lite endpoint when the primary one throws its bot challenge).
Fetched pages are stripped to readable text; JS-only pages and PDFs are refused
with an honest error. Web content is treated as untrusted — the system prompt
tells the model to never follow instructions found inside fetched pages.

## Roadmap

- Real integrations: calendar, email, and MCP servers for things that have them
- Proactivity: a cron job that runs a morning briefing unprompted
- `windows/bridge.py` — a Windows-side HTTP daemon for GUI automation, since a
  WSL process cannot drive the Windows desktop directly. Set
  `networkingMode=mirrored` in `C:\Users\johnw\.wslconfig` first so `localhost`
  works both ways.
