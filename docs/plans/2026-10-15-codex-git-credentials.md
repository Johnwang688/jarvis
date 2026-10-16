# Codex chats: git, push and pull requests without the GitHub login — plan (2026-10-15)

Status: phase 1 merged ([#223](https://github.com/Johnwang688/jarvis/pull/223)), its leftovers in [#230](https://github.com/Johnwang688/jarvis/pull/230) still in review; phase 2 merged 2026-10-16 ([#224](https://github.com/Johnwang688/jarvis/pull/224), leftovers [#231](https://github.com/Johnwang688/jarvis/pull/231), merged 2026-10-16; branch
`claude/codex-read-gap`; `docs/notes/providers-runtime.md` "Codex permission
profile" has what the build measured beyond §5.1). Decision recorded below (CG-0). Read with
`docs/plans/2026-10-15-pr-merge-close-decisions.md` (merge_gate, M-1..M-15)
and CLAUDE.md "Chat git worktrees".

## CG-0. The owner's decision (2026-10-15)

> The GitHub login stays inside Jarvis. Codex asks to push or open a PR, the
> owner approves, and Jarvis runs the command with the owner's credentials,
> so Codex never holds the token. Merges and closes always ask, via
> merge_gate.

Still in force: Codex asks before a push or a PR (a sandbox widening for
Codex, so asked every time with no Always); merges and closes always ask
through `merge_gate` (M-1..M-5). Codex chats must be able to make worktrees,
commit, push branches, open PRs and, with the owner's yes, merge them.

## 1. Root cause (confirmed)

`codex_config.clean_env()` starts each Codex app-server with
`HOME=~/.local/share/jarvis/v2/codex/<thread>` and
`PATH=/usr/local/bin:/usr/bin:/bin`. Every command Codex runs gets that
environment (`shell_environment_policy.inherit = "all"` inherits the
app-server's, not the owner's). An owner-approved escalated command does run
outside the sandbox with the network on (measured, §2.2). But:

- `gh` (`~/.local/bin/gh`) is not on PATH;
- git reads `$HOME/.gitconfig`, so the owner's
  `credential.https://github.com.helper = !…/gh auth git-credential` is not
  there. An HTTPS fetch or push of a private repo cannot get a username, and
  with prompts off git exits 128. That matches thread d3be37df
  (`git fetch origin && gh repo view …` in `/mnt/c/dev/override-jarvis`, an
  HTTPS remote: approved, exit 128).

"Codex reports no HTTPS access at all" is the sandbox doing its job: plain
(non-escalated) commands have no network, not even loopback (§2.2). Only
escalated commands have it.

The same HOME also hides the owner's `user.name`/`user.email`. Commits made
in a Codex chat today get git's guessed identity, or fail with "Please tell
me who you are", depending on the host name. §4.6 fixes that.

## 2. Evidence

All probes were offline. Nothing used the network with the owner's
credentials, and no credential contents were read or printed. Readability
was tested by `os.open()` and an immediate close, never by reading.

### 2.1 Q1: what sandboxed (non-escalated) Codex commands can read. **They can read the credentials.**

Jarvis sends `sandbox_mode = "workspace-write"` (config, `thread/start`
`sandbox`, verified in the receipt). In the Codex 0.161 protocol,
`SandboxPolicy.workspaceWrite` has `writableRoots`, `networkAccess` and the
tmp flags, and **no readable-roots field**: reads are the whole disk.

Measured twice:

1. `codex sandbox -P :workspace` (0.161.0, the built-in workspace profile).
2. The real `codex app-server --stdio` with Jarvis's config shape
   (`workspace-write`, `network_access = false`, `inherit = "all"`). A
   scripted local model ran the probe as a plain command, inside
   `unshare -Urn` with only loopback (the method of
   `tests/v2/fixtures/subagents/record_codex.py`).

Both runs could open each of these:

| Path | Plain sandboxed command |
|---|---|
| `~/.config/gh/hosts.yml` (the gh token is stored here in plain text; key name checked, value not read) | **openable** |
| `~/.gitconfig` | openable |
| `~/projects/Jarvis/.env` | **openable** |
| `~/.codex/auth.json` (the ChatGPT login) | **openable** |
| `~/.claude/.credentials.json` | **openable** |
| `~/.config/jarvis/`, `~/.aws/`, `~/.local/share/jarvis/browser/`, `~/.local/share/jarvis/v2/` | listable |

Writes outside the writable roots were refused and the network was blocked,
so the sandbox works as designed. It just does not confine reads.

**This is an existing use-but-never-see gap, whatever is built for git.** A
Codex chat can run `cat ~/.config/gh/hosts.yml` (or `.env`, or
`~/.codex/auth.json`) as an ordinary sandboxed command. No approval
callback fires, so Jarvis's gate never sees it, and the value goes into the
model's context at OpenAI. Layer 1's command refusal and output scrubbing
only apply to commands that reach the gate. Plain sandboxed commands never
do. Until §5 (phase 2) lands, "Codex never holds the token" is not true. The
token is one unasked `cat` away. Phase 1 stops Jarvis from *handing* it over
(no `GH_CONFIG_DIR`, no PATH change, no shim); phase 2 stops Codex from
*taking* it.

Claude chats are a different case. Claude Code runs with the owner's real
HOME, and its `PreToolUse` hook sees every Bash and Read call. Layer 1
refuses commands and reads that name credential files. So Claude does not
have this unseen-read gap, though the Claude sandbox does not confine
anything on this WSL either (CLAUDE.md).

### 2.2 Q2: how an owner-approved escalated command runs

Same offline app-server harness. The model asked for
`sandbox_permissions: "require_escalated"` and the client answered
`accept`.

| | Escalated (accepted) | Plain |
|---|---|---|
| Sandbox | **none**: `Seccomp: 0`, `NoNewPrivs: 0` | seccomp 2, NoNewPrivs 1 |
| Network (loopback probe) | **connected** | blocked (EPERM) |
| Writes outside writable roots | allowed | refused |
| Credential files | openable | openable |
| Environment | the app-server's (`clean_env`): private HOME; PATH = Codex's `codex-path` dirs + `/usr/local/bin:/usr/bin:/bin` + `/snap/bin` (from `bash -lc`); a marker variable set on the app-server was inherited | same |
| `gh` on PATH | no | no |

So escalated commands **do** run with network, which the owner wanted.
They fail on credentials only because HOME and PATH lack them. The approval
request carried `additionalPermissions: null` and
`networkApprovalContext: null`, and Jarvis marks every chat command
callback as a widening anyway (`codex.py`).

### 2.3 Codex 0.161 can deny reads, but escalation behaves differently there

0.161 has named permission profiles:
`default_permissions = "<id>"`, `[permissions.<id>]`, `extends = ":workspace"`
and `[permissions.<id>.filesystem]` with `"path" = "deny" | "read" | "write"`
(also `glob_pattern` entries and `glob_scan_max_depth`). On Linux, bwrap
enforces them. Measured with a profile that denies `~/.config/gh`,
`~/.gitconfig` and the repo `.env`:

| | Escalated (accepted) | Plain |
|---|---|---|
| Denied paths | **still denied** | denied |
| Other credentials (not listed) | openable | openable |
| Writes outside roots | allowed | refused |
| Network | **blocked** | blocked |
| Sandbox | seccomp 2 (still sandboxed) | seccomp 2 |

The `thread/start` receipt then carries
`activePermissionProfile: {"id": "jarvis", "extends": ":workspace"}`, and
the legacy `sandbox` field still reads `workspaceWrite`.

So in profile mode, `require_escalated` widens writes but **keeps the
network off and keeps deny-read**. That is good for credentials and bad for
"escalated commands have network". Settled in §5.1: a different per-command route (`with_additional_permissions`) does grant network.

### 2.4 Local git inside the sandbox works today

App-server harness, Jarvis's writable roots (`<repo>/.git` and a worktree
root), plain commands: `git worktree add -b codex/probe <root>/probe`,
`git add` and `git commit` all succeeded. Validated git metadata plus
`CHAT_WORKTREES_DIR` is enough. The only gap is identity (§1, §4.6).
`codex_config.git_dirs('/mnt/c/dev/override-jarvis')` validates its `.git`
(see §6).

### 2.5 Q3: Claude's precedent

The daemon unit sets `PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin` and
the owner's HOME, and the Claude CLI inherits both. Claude chats push and
run `gh` with the owner's own git config and gh login. The hook judges
every command, merge_gate asks for merges and closes, and M-3 lets pushes
and PRs go to Claude's own reviewer under AUTO. Copying that into Codex
(option A below) would give Codex the token *files* in its environment
behind a sandbox that cannot hide them.

## 3. Options considered

**A. Minimal env (rejected).** Add `~/.local/bin` to PATH and set
`GIT_CONFIG_GLOBAL=~/.gitconfig` and `GH_CONFIG_DIR=~/.config/gh`.

- Owner sees: the same widening card as today, and push and PR then work.
- Effort: about 10 lines.
- Leaks: the sandbox already reads those files (§2.1). Pointing the
  environment at them advertises the token. With deny-read (§2.3), escalated
  commands could not read them either, so A only works without deny-read,
  that is, with the gap left open.
- Repo config is Codex-writable (`.git` is a writable root), so an approved
  `git push` would run Codex-planted `credential.helper`, `core.sshCommand`,
  `http.proxy`, `url.*.insteadOf` or hooks with the owner's login in reach.
- It breaks "Integrations stay scoped — credentials stay inside the
  integration".

**B. Jarvis-mediated typed tools (chosen, CG-0).** Push, PR create, PR
merge and PR close become jarvis-mcp session tools. The daemon runs git and
gh with the owner's login after the gate (owner asked every time;
merge_gate's headline for merges and closes). Codex never holds the token.
The network half never runs in a repository Codex can configure (§4.3).

- Owner sees: one card per push, PR, merge or close. Each card shows the
  repository, the branch and the operation.
- Effort: medium, about 600–900 lines with tests. The parts are a new
  module, session-tool entries, the permissions hook, the Codex note and
  env, and tests and mutants.

**C. Credential-helper shim (rejected).** A `git-credential-jarvis` on
Codex's PATH that asks the daemon over a private socket, only for approved
commands.

- The shim hands the token *to a process Codex controls*: git runs it, and
  git's own config (Codex-writable) decides what happens to the answer.
  `credential.helper` chains, `http.proxy` and `insteadOf` all apply.
- Binding a token to "this approved command" across git's process tree
  cannot be enforced.
- It still needs an answer for gh (`GH_TOKEN` in the environment = the
  token in Codex's hands).

## 4. Phase 1: build (option B)

### 4.1 What runs where

```
Codex sandbox (no network, no token)          Jarvis daemon (owner HOME, gh login)
-------------------------------------          ------------------------------------
git worktree add / commit / branch  (today)
calls jarvis tool chat_git_push(worktree)  --> bridge -> permit: owner card (always)
                                               1. read worktree's branch + origin URL by file reads
                                               2. validate: github.com HTTPS repo, pinned per project
                                               3. local fetch worktree -> private mirror (hardened, no creds in env)
                                               4. git push from the mirror (daemon config, gh helper)
calls chat_pr_create(worktree, title, body)--> owner card -> gh pr create --repo O/R --head B (cwd = mirror)
calls chat_pr_merge(worktree, number)      --> merge_gate headline card -> gh pr merge N --repo O/R
calls chat_pr_close(worktree, number)      --> merge_gate headline card -> gh pr close N --repo O/R
calls chat_git_fetch(worktree)             --> fetch origin into the mirror (creds), then Codex runs
                                               `git fetch <mirror>` itself, in its sandbox (local path)
```

### 4.2 Files and functions

1. **New `jarvis/v2/chat_git.py`.** No model-facing text, and pure where it
   can be.
   - `mirror_dir(project_key) -> Path`: under
     `config.V2_DATA_DIR / "chat-git" / <sha256(repo common dir)>`, a bare
     repo made 0700 and refused if it is a symlink. Its config is written
     only by the daemon. It never stores a token: the credential comes from
     the owner's gh helper at run time.
   - `read_target(worktree) -> Target(repo_common_dir, branch, origin_url)`.
     It uses `codex_config.git_dirs` / `_small_text`-style bounded file
     reads (HEAD, `config` parsed with `git config -f <file> --get`
     **under a minimal env**, no includes: `GIT_CONFIG_NOSYSTEM=1`,
     `GIT_CONFIG_GLOBAL=/dev/null`, `HOME=<empty private dir>`). The
     worktree must lie under `config.CHAT_WORKTREES_DIR` or be the thread's
     own `brief.cwd`.
   - `github_repo(url) -> "OWNER/REPO" | None`: accepts only
     `https://github.com/O/R(.git)` and `git@github.com:O/R(.git)`, with
     `[A-Za-z0-9_.-]` names and no userinfo, port or query. Anything else
     is refused.
   - `pin(project_key, owner_repo)`: on the first push for a project,
     record `OWNER/REPO` in `V2_DATA_DIR/chat-git/pins.json` (0600). Every
     later call must match it. A changed origin (Codex can edit
     `.git/config`) is refused with "origin changed; the owner re-pins it
     in the HUD/CLI". The first pin is shown on the card.
   - `gate_line(tool, args, target) -> str`: the exact command the daemon
     will run, as one shell line, for merge_gate and the card. Examples:
     `git push origin refs/heads/B:refs/heads/B`,
     `gh pr create --repo O/R --base main --head B`,
     `gh pr merge 12 --repo O/R --squash`, `gh pr close 12 --repo O/R`.
   - `judge_push(target)`: refuses unasked a detached HEAD, a protected
     branch (`merge_gate.PROTECTED_BRANCHES` or the remote HEAD branch), and
     any force or delete (the tool takes no such option). Landing on main
     is `chat_pr_merge`'s job.
   - `run_git(argv, cwd)` / `run_gh(argv, cwd)`. `gitops._HARDENING` plus
     `-c credential.helper=` (reset) and
     `-c credential.https://github.com.helper=!<abs gh> auth git-credential`,
     `-c http.proxy=`, `-c core.sshCommand=`, `-c protocol.file.allow=user`.
     Env: `GIT_TERMINAL_PROMPT=0`, `GH_PROMPT_DISABLED=1`,
     `GH_NO_UPDATE_NOTIFIER=1`, `GIT_CONFIG_NOSYSTEM=1`, no `GIT_*` from the
     daemon env, and the owner's HOME, so gh finds `~/.config/gh`. gh comes
     from `gitops.gh_binary()` (never under `/mnt/`). Output is passed
     through `jarvis.v2.redact` before it is returned. Timeout and memory
     caps match gitops.
2. **The mirror step: where Codex's config could matter, and how it is
   kept out.**
   - (a) `git -C <mirror> fetch --no-tags <repo_common_dir>
     +refs/heads/B:refs/heads/B`. This is a local transport: git starts
     `upload-pack` in the Codex-writable repo. It runs with **no credential
     in the environment and HOME pointed at an empty private dir** (not
     `run_git`'s env), plus `_HARDENING`.
   - (b) `git -C <mirror> push https://github.com/O/R.git
     refs/heads/B:refs/heads/B` with `run_git`. The mirror's config is the
     daemon's own, and the URL is the validated pin.
   - Residual: step (a) is still the owner's uid. If any repo-config key
     made upload-pack execute a program, that program could read the token
     *file* from disk. The mutant suite (§4.5) plants each known exec key.
     If review finds one that works, wrap (a) in
     `codex sandbox -P <deny-read profile>` (§2.3 shows it denies reads).
     That wrapper is a ready fallback, not built by default.
3. **New `jarvis/v2/tools/chat_git_tools.py`** (registry tools, typed
   schemas from hints, invariant 6):
   - `chat_git_push(worktree)`, `dangerous=True`;
   - `chat_pr_create(worktree, title, body, base="", draft=False)`,
     `dangerous=True` (pushes first if the branch is not on the remote,
     under the same card);
   - `chat_pr_merge(worktree, number, method="squash")`, `dangerous=True`,
     method in {merge, squash, rebase}, no `--admin`/`--auto`;
   - `chat_pr_close(worktree, number)`, `dangerous=True`;
   - `chat_git_fetch(worktree)`, not dangerous (it reads the remote into
     the mirror; it writes nothing Codex owns). It returns the mirror path
     for Codex's own `git fetch <mirror> …`, which needs no network.

   Dangerous registry tools already take `permissions.build_permit`'s
   "dangerous Jarvis tool from a chat's jarvis-mcp: the owner" branch:
   asked every time, no Always, STRICT refuses. That satisfies "Codex asks
   before push/PR".
4. **`jarvis/v2/session_tools.py`.** Add the five entries with
   `providers=frozenset({"codex"})`, `foreground_only=True` (a sub-agent
   cannot push) and `desk_only=False` (the owner card still decides). Claude
   keeps its native path (§2.5). Offering these to Claude later is a
   separate owner choice. `mcp_catalog.py` is unchanged, so external MCP
   does not get them.
5. **`jarvis/v2/permissions.py`.** This is merge_gate visibility. Where
   `_owner_only(...)` is called (layer 2 setup, about line 1000): when
   `ctx.provider == "codex"` and `tool in chat_git.TOOLS`, use
   `chat_git.gate_line(tool, args, target)` as the line, with
   `shell_cwd = target.worktree`. merge_gate then reads the exact command
   the daemon will run. `chat_pr_merge` gets "merges a pull request" and
   `chat_pr_close` gets "closes a pull request" as the card headline (M-5).
   A push to a protected branch would get "pushes to main", but
   `judge_push` refuses that first. If `read_target` fails, the call is
   **DENY** with the reason (unknown means fail closed). The `command`
   shown on the card is `gate_line`, never a model-supplied string.
   `permissions.py` and `merge_gate.py` are SELF_PROTECTED (M-15). The PR
   must say it edits them.
6. **`jarvis/v2/providers/codex_config.py`.**
   - `clean_env()` gains `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
     `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL`. They are read once by
     the daemon from the owner's global `user.name`/`user.email` with
     `git config --global --get` (not secrets). Each must be one line,
     printable and capped at 200 characters, or it is omitted. **No** PATH
     change, `GIT_CONFIG_GLOBAL` or `GH_CONFIG_DIR`.
   - `git_note()` text: "The network is off inside the sandbox and this
     session has no GitHub login. To push, open, merge or close a pull
     request, call the Jarvis tools chat_git_push / chat_pr_create /
     chat_pr_merge / chat_pr_close; each asks the owner. For remote
     updates call chat_git_fetch, then `git fetch <mirror>`. Do not request
     escalation for git push or gh."
7. **`jarvis/v2/providers/codex.py` / `permissions.py` layer 1 (owner
   decision CG-1: yes).** For a Codex chat widening whose line
   runs `gh`, or `git push|fetch|pull|ls-remote` against a non-local remote,
   **decline unasked** with the pointer text from 6. These calls cannot
   succeed (no login), and the card would only cost the owner a click.
   - merge_gate still reads the line first, so a merge spelled this way is
     declined, never run.
   - Implement it with merge_gate's walker (`rules._Walk`) so wrappers and
     `bash -lc` are seen.
   - Unsure means the normal widening ask, never silent.
8. **Docs.**
   - CLAUDE.md "Chat git worktrees": replace "Push/gh therefore request
     escalation through the existing Jarvis gate" with the jarvis-tool path
     and the §2.1 gap's status. No test pins the old sentence (grepped).
   - `docs/notes/control-plane.md` or `providers-runtime.md`: the §2
     measurements.
   - `python scripts/slim_claude.py --check`.

### 4.3 How the token stays out of Codex's reach

- The daemon never puts the token, a token path or a helper into Codex's
  environment (no PATH, `GIT_CONFIG_GLOBAL` or `GH_CONFIG_DIR` change). The
  §4.5 test asserts `clean_env` keys exactly.
- No credentialed git or gh runs in a directory Codex can configure:
  - push runs in the daemon's mirror;
  - gh runs with `cwd = mirror` and an explicit `--repo` (gh otherwise reads
    the cwd's git remotes);
  - the only step that touches Codex's repo is a local fetch with no
    credential in its environment.
- The destination is the pinned `OWNER/REPO`, so an edited `remote.origin.url`
  or `insteadOf` cannot send the push (and the owner's GitHub token) to
  another host.
- Tool output is redacted and capped. A failure prints git's stderr, never
  the environment.
- **Closed in phase 2 ([#224](https://github.com/Johnwang688/jarvis/pull/224), merged 2026-10-16):** until then the sandbox could read `~/.config/gh/hosts.yml`
  unasked (§2.1).

### 4.4 What the owner sees

| Codex does | Card | Always offered |
|---|---|---|
| worktree, branch, commit, local fetch from mirror | nothing (sandboxed) | n/a |
| `chat_git_fetch` | nothing under AUTO (non-dangerous delegate; STRICT refuses) | n/a |
| `chat_git_push` | "Codex wants to push `B` to `O/R`" + `git push …` line | no |
| `chat_pr_create` | the push line + `gh pr create --repo O/R --base main --head B`, title shown | no |
| `chat_pr_merge` | **"merges a pull request"** headline (merge_gate) + line | no |
| `chat_pr_close` | **"closes a pull request"** headline + line | no |
| escalated `git push` / `gh …` | declined unasked with the tool pointer (CG-1); without CG-1, today's widening card and a failure | no |
| first push of a project | the card also says "first push: pins origin to O/R" | no |

### 4.5 Tests and mutants

All tests use a temporary HOME and config, fake gh and fake remotes, and the
`systemd-run` memory cap. None use the live daemon, ports 8402/8403/8405 or
the network.

- **`tests/v2/chat_git_check.py`** (new).
  - A local bare "remote" stands in for GitHub through a test-only URL
    validator seam (`chat_git._ALLOW_TEST_REMOTE`, set in code only, never
    from env or config). gh is a fake script that records its argv and env.
  - Cases:
    - push happy path;
    - detached HEAD, protected branch, a worktree outside the chat root,
      and a branch name with `..`/`:`/a leading `-` are each refused;
    - an origin change after the pin is refused;
    - a non-GitHub URL, userinfo, a port, `ext::` or `file::` are refused;
    - gh is called with `--repo` and `cwd=mirror`;
    - the fake gh sees the owner HOME and no Codex env;
    - redaction of a token-shaped stderr line;
    - `read_target` on a symlinked or oversized `.git` file is refused.
- **Malicious repo-config suite** (in the same file). Each of these is
  planted in the Codex repo's `.git/config` and hooks, and the push still
  succeeds or fails cleanly with no marker file written:
  - `core.hooksPath` plus hooks (pre-push, post-checkout, reference-transaction);
  - `core.fsmonitor`, `core.sshCommand`, `core.alternateRefsCommand`,
    `uploadpack.packObjectsHook`;
  - `include.path` to a file setting the above;
  - `credential.helper=!touch marker`;
  - `url.<x>.insteadOf`, `remote.origin.pushurl`, `http.proxy`.

  This is the guard for §4.2 item 2.
- **`tests/v2/permissions_check.py`** additions:
  - `chat_pr_merge` and `chat_pr_close` ask with the merge_gate headline and
    no Always in every profile;
  - STRICT refuses all four dangerous tools;
  - deny-all declines;
  - an unreadable target is DENY;
  - the card's command equals `gate_line`.
- **`tests/v2/codex_provider_check.py`** additions:
  - `clean_env` keys are exactly the allowed set plus the identity
    variables (no PATH, `GIT_CONFIG_*` or `GH_*` drift);
  - an identity value with a newline or control character is dropped;
  - the git_note text names the tools;
  - CG-1: an escalated `git push`, `gh pr create` or `bash -lc 'gh …'`
    widening is declined unasked with the pointer, while `gh pr merge` is
    still declined (never run);
  - an unsure line still asks.
- **`tests/v2/session_tools` / bridge checks:**
  - the five tools are offered to Codex owner chats only, not to Claude,
    tasks or external MCP;
  - foreground-only (a sub-agent call is refused).
- **Mutants, extending `tests/v2/merge_gate_mutants.py` or a new
  `tests/v2/chat_git_mutants.py`.** Each must make a check fail:
  1. drop the `gate_line` hookup in `permissions.py` (merge headline lost);
  2. mark `chat_pr_merge` non-dangerous;
  3. run gh with `cwd=worktree` instead of the mirror;
  4. skip the pin comparison;
  5. accept any `https://` host;
  6. run the local fetch with the credentialed env;
  7. drop `credential.helper=` reset;
  8. let `clean_env` gain `GH_CONFIG_DIR`;
  9. make CG-1 decline `gh pr merge` *after* allowing it;
  10. drop `foreground_only`.
- **Live (owner-attended, after merge and restart):** one Codex chat in a
  scratch GitHub repo does worktree, commit, `chat_git_push`,
  `chat_pr_create` (approve), then `chat_pr_merge` (approve). Repeat once in
  `/mnt/c/dev/override-jarvis` with push and a draft PR only. Record the
  receipts in the work checkpoint, not as an inference from unit tests.

## 5. Phase 2: close the sandbox read gap (separate PR)

Move Codex chats from `sandbox_mode = "workspace-write"` to a Jarvis
permission profile:

```
default_permissions = "jarvis-chat"
[permissions.jarvis-chat]
extends = ":workspace"
[permissions.jarvis-chat.filesystem]
```

The filesystem table needs these entries:

- "write" for each `git_roots()` entry;
- "deny" for each `config.credential_dirs()` entry, `~/.codex`, `~/.claude`,
  `config.V2_DATA_DIR`, `SESSIONS_DIR` and the gate's protected paths;
- a `glob_pattern` deny for `.env*` under the cwd, with
  `glob_scan_max_depth` set.

`codex.py`'s effective-config and receipt checks change from
`sandbox_mode` to `default_permissions` and `activePermissionProfile`. The
same must hold for tasks, since their sandboxes read the same files.

### 5.1 CG-2 probe result (2026-10-15): **yes**, one approval can grant network to one command

The owner asked for the probe first. It ran offline, the same way as §2:
the real `codex app-server` 0.161.0 in profile mode, a scripted local
model, a loopback-only `unshare -Urn` namespace, a memory cap, and opens
only (no reads). The deny entries covered `~/.config/gh`, `~/.gitconfig`,
the repo `.env` and `~/.codex/auth.json`. Every route asked for:

| Route | Approval the client saw | Approved command: network | Denied paths | Writes outside roots | Next plain command |
|---|---|---|---|---|---|
| `require_escalated`, `accept` | `availableDecisions: [accept, acceptWithExecpolicyAmendment, cancel]` | blocked | denied | **allowed** | blocked |
| `require_escalated`, `acceptForSession` | same | blocked | denied | allowed | blocked |
| `require_escalated` + `prefix_rule`, `acceptWithExecpolicyAmendment ["python3"]` | same | blocked | denied | allowed | blocked; a later identical escalated command ran **unasked**, sandboxed, writes refused |
| `require_escalated`, `applyNetworkPolicyAmendment {host: 127.0.0.1, allow}` (not offered) | same | blocked | denied | n/a | n/a |
| **`with_additional_permissions` + `additional_permissions: {network: {enabled: true}}`** (feature `exec_permission_approvals`), `accept` | `additionalPermissions: {network: {enabled: true}, fileSystem: null}`, `availableDecisions: [accept, cancel]` | **connected** | **denied** | refused | **blocked** |
| same, `decline` | same | did not run ("rejected by user") | n/a | n/a | n/a |
| `with_additional_permissions` asking `file_system.read: [~/.config/gh]`, `accept` | `additionalPermissions.fileSystem.entries: [{path, access: read}]` | blocked | **still denied (deny wins over a read grant)** | refused | n/a |
| `request_permissions` tool (feature `request_permissions_tool`), granted `{network: {enabled: true}}`, `scope: "turn"` | `item/permissions/requestApproval` | connected | denied | refused | **connected (every later command in the turn)** |
| a second profile for approved commands | n/a: `[permissions.<id>]` has only `description`, `extends`, `workspace_roots`, `filesystem`, `network` (0.161 `PermissionProfileToml`). There is no per-command profile selector. | | | | |
| profile `[permissions.jarvis.network] enabled = true` (control) | none | connected | denied | refused | connected (every command) |

**Answer: yes.** Turn on Codex's `exec_permission_approvals` feature and
have the model ask with `sandbox_permissions: "with_additional_permissions"`
plus `additional_permissions: {network: {enabled: true}}`. One owner
`accept` then gives that one command network:

- it runs inside the same bwrap/seccomp sandbox (`Seccomp: 2`);
- the deny-reads still hold, and an approval asking to read a denied path
  does not lift the deny;
- writes stay confined;
- the next command has no network.

`require_escalated` cannot do this in profile mode: the network stays off
(it only widens writes). The execpolicy and network-policy amendments and
`acceptForSession` do not grant network either. `request_permissions`
grants network for the rest of the turn, not one command, so it is not the
route.

Also measured with `codex sandbox -P` on the same profile shape: a
`"<root>" = "write"` entry makes that root writable and other paths stay
refused, and a glob deny works.
`glob_scan_max_depth = 4` with `"<cwd>/**/.env*" = "deny"` denied `.env.local`
and `sub/.env` while `ok.txt` stayed readable. Linux expands globs when a
command starts, so a `.env` created mid-session is covered from the next
command on.

### 5.2 What phase 2 adopts (build-ready)

**Config** (`codex_config.config_text`, chats and tasks; replaces
`sandbox_mode` and `[sandbox_workspace_write]`; the two cannot both be set):

```toml
default_permissions = "jarvis-chat"
[features]
exec_permission_approvals = true
# request_permissions_tool stays off (default): it grants turn-wide
[permissions.jarvis-chat]
extends = ":workspace"
[permissions.jarvis-chat.filesystem]
glob_scan_max_depth = 4
"<each git_roots() entry>" = "write"
"<each config.credential_dirs() entry>" = "deny"
"~/.codex" = "deny"            # absolute paths; commands only, the app-server is unsandboxed
"~/.claude" = "deny"
"~/.gitconfig" = "deny"
"<config.V2_DATA_DIR>" = "deny"
"<SESSIONS_DIR>" = "deny"
"<each protected_paths() entry>" = "deny"
"<brief.cwd>/**/.env*" = "deny"
[permissions.jarvis-chat.network]
enabled = false
```

Paths are absolute, resolved and must not be symlinks (the `_plain_path`
rule). `V2_DATA_DIR` holds the private Codex homes, so the deny also hides
each thread's `config.toml` (it may carry a jarvis-mcp token) from commands.

**Start-up checks** (`codex.py`, replacing the `sandbox_mode` and
`writable_roots` checks). Reading `config/read` returns all of this
(measured):

- `default_permissions == "jarvis-chat"`;
- `sandbox_mode is None`;
- `features.exec_permission_approvals is True`;
- the `permissions["jarvis-chat"]` filesystem map equals what was written
  (every deny present);
- `network.enabled is False`.

The `thread/start`/`resume` receipt must carry
`activePermissionProfile == {"id": "jarvis-chat", "extends": ":workspace"}`,
and its legacy `sandbox.networkAccess` must be false. Anything else is
`BriefRefused`. `permissionProfile/list` lists the profile too.

**Thread params:** drop `"sandbox": "workspace-write"` from
`thread/start`/`thread/resume`; the profile is selected by
`default_permissions`. Verify once in the build that `thread/resume`
needs no sandbox field.

**Approval handling** (`codex.py` `_widening`, layer 1 in `permissions.py`).
The request shape (verified):

```json
{"method": "item/commandExecution/requestApproval",
 "params": {"kind": "command", "itemId": "…", "command": "/bin/bash -lc '…'", "cwd": "…",
            "reason": "…", "additionalPermissions": {"network": {"enabled": true}, "fileSystem": null},
            "proposedExecpolicyAmendment": ["…"], "availableDecisions": ["accept", "cancel"]}}
```

The reply is `{"decision": "accept"}` or `{"decision": "decline"}`. Jarvis
already describes `additionalPermissions` on the widening card ("network
on") and asks every time with no Always. Keep that. Also:

- a filesystem grant inside `additionalPermissions` is judged by layer 1 as
  today;
- deny wins anyway, as measured;
- `require_escalated` still arrives as a plain command approval and stays a
  widening ask. In profile mode it widens writes, not network or reads.
- never send `acceptWithExecpolicyAmendment` (it silently pre-approves
  later commands, as measured) or `acceptForSession`;
- keep answering `item/permissions/requestApproval` with an empty grant.

**Model guidance** (`git_note` / developer instructions): "The network is
off in the sandbox. For a command that needs the internet (npm install,
pip, curl), set `sandbox_permissions` to `with_additional_permissions` with
`additional_permissions: {network: {enabled: true}}`; the owner approves
each one. Do not use `require_escalated` for network. For git push and pull
requests, use the Jarvis tools."

**Risks to record:**

- `exec_permission_approvals` is marked "under development" in 0.161's
  `codex features list`. Pin it with `CODEX_VERIFIED` and re-run this
  probe on every Codex upgrade. A newer Codex that drops the feature fails
  the start-up check (refused, never silently opened).
- An approved network command runs with the owner's uid inside bwrap. It
  can reach the internet but not the denied files, so a `curl` cannot ship
  the token, because the token is unreadable to it.

**Tests and mutants** (`tests/v2/codex_provider_check.py`, fake server):

- the config text carries every deny;
- each start-up check refuses when tampered (a missing deny,
  `sandbox_mode` set, feature off, network on, wrong `activePermissionProfile`);
- an `additionalPermissions.network` approval asks with "network on" and
  no Always;
- `acceptWithExecpolicyAmendment` and `acceptForSession` are never sent.
  Mutants: drop one deny entry, accept a receipt without the profile, send
  `acceptForSession`.
- Free live receipt, offline: re-run this harness (it lives in the lead's
  scratch; the method is `tests/v2/fixtures/subagents/record_codex.py`
  with a `with_additional_permissions` step). Consider committing it as
  `tests/v2/fixtures/cg2_probe.py`.

Phase 2 is what makes CG-0's "Codex never holds the token" true. With it,
the §2.1 table becomes "denied" for every listed path, approved commands
included.

## 6. WSL `/mnt/c` repositories

- `git_dirs('/mnt/c/dev/override-jarvis')` validates its `.git`, so Codex
  may write that metadata and make worktrees under `CHAT_WORKTREES_DIR`.
  The worktrees live on the Linux filesystem, which is fast.
- The Windows-created `.git/config` sets `extensions.worktreeConfig`,
  `core.filemode`, `core.symlinks` and `core.ignorecase`. Linux git honours
  them. It has no `credential.helper`, hooks (beyond samples) or
  `includeIf`, but the plan does not rely on that: §4.3 never runs a
  credentialed command there.
- **Hazard: Windows git can prune the worktree.** A Linux worktree's
  metadata (`.git/worktrees/<n>/gitdir`) holds a Linux path that Windows git
  cannot see. A `git worktree prune` from Windows (VS Code runs it at times)
  can delete it and orphan the chat's worktree.
  - Mitigation: `chat_git` reads the branch from the worktree's own
    `.git` file and HEAD, and fails clearly if the metadata is gone.
  - The committed branch survives in the common dir, and the push takes
    the branch ref from there.
  - Record it in the Codex note: "commit before long pauses".
- The `remote.origin.url` there is HTTPS GitHub, so it passes
  `github_repo()`.
- The local fetch over 9p is slow for a large repo but only moves new
  objects. The mirror is incremental.
- Line endings: the mirror is bare, so no checkout and no autocrlf.

## 7. Hooks and repo config versus the credential path

`.git` is Codex-writable (an explicit owner risk), so everything in the
repository's config and hooks is attacker-controlled data for this design:

- Hooks run when Codex commits inside its sandbox. That is harmless to
  credentials once phase 2 denies reads; today it is no worse than the
  `cat` gap.
- The daemon's own git runs `_HARDENING` (`core.hooksPath=/dev/null`,
  fsmonitor off, `protocol.ext` never, and so on). Credentialed operations
  run only in the mirror; gh runs with an explicit `--repo` outside the
  repo.
- The destination is pinned.
- The one step that touches Codex's repo (the local fetch) has no
  credential in its environment and is the subject of the malicious-config
  suite. If any key gets execution there, wrap it in a deny-read
  `codex sandbox` (§4.2 item 2).

## 8. Decisions

- **CG-0** (owner, 2026-10-15): login stays inside Jarvis; Jarvis runs push
  and gh after the owner's yes; merges and closes via merge_gate. Recorded
  above.
- **CG-1** (owner: **yes**, 2026-10-15): decline escalated `git push` and
  `gh` from Codex chats unasked, with a pointer to the tools (§4.2 item 7).
- **CG-2** (owner: probe first, 2026-10-15): probed. One approval *can*
  grant network to one command in profile mode, through
  `exec_permission_approvals` + `with_additional_permissions` (§5.1).
  Phase 2 adopts it (§5.2), so approved commands keep network and the read
  gap closes.
- **CG-3** (owner: **no**, 2026-10-15): `jarvis/v2/chat_git.py` is **not**
  added to SELF_PROTECTED. The edits to `permissions.py` (already
  SELF_PROTECTED, M-15) still need saying in the PR.
