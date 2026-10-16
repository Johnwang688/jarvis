# Pull-request merges and closes are the owner's — decisions (2026-10-15)

The owner, after the GIT-LIVE test: **"Jarvis chats should match Claude Code's
auto mode — pushing a branch and opening a PR proceed without asking, but
merging or closing a PR always requires the owner's approval."**

What GIT-LIVE showed (`~/.local/share/jarvis/v2/decisions.jsonl`): a Claude
chat with brief profile `auto` and no `always_ask` ran
`git push -u origin <branch>` and `gh pr create --draft` at layer `reviewer`
("no opinion; the provider's own reviewer decides"), so Claude Code's
classifier allowed both and no card appeared. A Codex chat's push and `gh`
were `sandbox-widening` and asked the owner, because Codex sandbox
networking is off by design.

## The decision

- **M-1. Merging or closing a pull request asks the owner, every time.**
  `gh pr merge` (any flags: `--auto`, `--admin`, `--squash`, …),
  `gh pr close`, and the `gh api` calls that do the same (a write to
  `pulls/N/merge`, `pulls/N` or `issues/N` with `closed`, a repo's `merges`;
  a GraphQL `mergePullRequest`, `closePullRequest`,
  `enablePullRequestAutoMerge`, `mergeBranch`, or `updatePullRequest` with
  `CLOSED`).
- **M-2. So do the pushes that amount to a merge or a close.** A push to the
  default branch (main, master, `prod*`/`production*`/`release*`, or a
  remote's HEAD branch) in any refspec spelling, `--all`/`--branches`/
  `--mirror`, `push.default=matching`; a branch deletion (`--delete`, `-d`,
  `:branch`, `--prune`); a force push (`--force`, `-f`, `--force-with-lease`,
  `--force-if-includes`, `+refspec`). A `gh api` write that moves or deletes
  a branch ref or commits a file without naming a non-default branch counts
  too, as does `gh repo sync` onto a remote repository.
- **M-3. Pushing a branch and opening a pull request are unchanged.**
  `git push -u origin <branch>`, `git push origin HEAD` on a feature branch,
  `gh pr create`, `gh pr view`, `gh pr list` and the other `gh pr` verbs keep
  exactly today's answer: under AUTO, still the provider's reviewer.
- **M-4. In every profile, for Claude and Codex, chats and tasks.** It is
  checked at layer 2 by code (`jarvis/v2/merge_gate.py`), before any
  always-ask list, so no always-ask file, project or brief removes it. A
  match goes to a human-backed asker; deny-all, a timeout or no surface
  declines it. An allowlist entry, rules' ALLOW and the provider's own
  reviewer never answer it. **No Always is offered**, and an Always sent
  anyway mints nothing.
- **M-5. The card says what it does, plainly, first:** "merges a pull
  request", "closes a pull request", "pushes to main" (the branch it names),
  "deletes a branch", "force-pushes". A Codex call that is also a sandbox
  widening keeps its widening ask (strict still refuses one) and its headline
  leads with the operation: "merges a pull request · Codex chat requests
  execution beyond its ordinary sandbox; owner approval required".

## Owner's answers (2026-10-15)

- **M-6. `gh pr reopen` does not ask** — the owner: "non-destructive". It
  neither lands code nor ends a review; it undoes a close.
- **M-15. The gate is SELF_PROTECTED.** `jarvis/v2/merge_gate.py` and
  `jarvis/v2/permissions.py` join `jarvis/tools/files.py`'s SELF_PROTECTED,
  on the same footing as `rules.py` and `bridge.py`: no agent write tool
  (write_file, edit_file, a protected-state write) may change them.
  Consequence: the HUD editor serves them read-only (its `safety` row), so
  the owner cannot edit, rename or delete them from the HUD either; changes
  go through a worktree and a pull request.
- **M-7. Codex keeps asking for a push and a PR.** Its sandbox networking
  stays off, so `git push` and `gh pr create` from a Codex chat still ask as
  sandbox widenings (CLAUDE.md: sandbox widening needs a human, never
  Always). Unchanged by this work.

## Lead's defaults (not asked; open to amendment)

- **M-8. Where it cannot tell, it asks** with "may merge or close a pull
  request" or "may push to main (what it pushes cannot be told from the
  command)": a refspec, endpoint, method or `gh` verb that is an expansion
  (`git push origin "$BRANCH"`) or a brace expansion (`{main,}`, `{a..c}`),
  input appended by `xargs`, a `gh` command that is neither built in nor the
  stock `co` alias (an alias or an extension) and `gh extension exec`, an
  unreadable GraphQL query (`-F query=@file`, `--input`) or issue edit, a
  `git -c remote.*/push.*/branch.*` override, `GIT_DIR`/`GIT_WORK_TREE` on
  the line, a `cd` it cannot follow, a `.git` git would not read as a git
  directory or a HEAD that is neither a `ref:` nor an object name, and a line
  too complex to walk. `git push -u origin "$(git branch --show-current)"`
  counts as the current branch.
- **M-9. A push with no refspec, or of `HEAD`, is judged where it runs.**
  The current branch is read from the repository's `.git` (git is never run;
  a symlinked HEAD is read as old gits wrote it): in the shell's own folder
  (Claude's hook input `cwd`, Codex's item `cwd`; never a `cwd` the model
  writes into the arguments), else the brief's, then each `cd` on the line (a
  `cd` into an existing folder on a line with no subshell is where what
  follows runs), `git -C`, and a wrapper's folder option (`env -C`,
  `--directory`, kept beside the shell's folder). It asks only when that
  branch is protected, or, under `push.default=upstream`, when its upstream
  is; `push.default=matching` asks. Config is read in git's order and the
  last value wins, comments cut.
- **M-10. An earlier program on the same line counts** (review H-1). A
  `git checkout`/`switch` of a branch (not of a file), `git branch -m/-M`,
  `git symbolic-ref HEAD …`, `gh pr checkout` moves HEAD: a later plain or
  HEAD push is judged by the branch it moved to (with `;`, `||` or a newline
  the line may go on after a failed switch, so the old branch counts too),
  and asks "may push to main" when that cannot be told (`git checkout -`, a
  PR's branch). A `git config` write or `git branch -u` makes a later plain
  push, and a later unknown git subcommand (a new alias), ask.
- **M-11. Git aliases are followed** from `-c alias.*` on the line and from
  the repository's and the owner's git config (`!` aliases are judged as
  lines); `git submodule foreach`, `git rebase -x` and `git bisect run`
  lines are judged; inline interpreter code (`python -c`, `node -e`, a
  here-document fed to one) is read as text, and a bare push there is a push
  of the current branch.
- **M-12. The raw-line backstop** (review M-2). Where the line runs a program
  that may run text it is handed — a shell or `eval`, an interpreter, `ssh`,
  `xargs`, `cmd.exe`/`wsl.exe`, `curl`/`wget`, a program this reader does not
  know, a function, a variable, a git exec setting (`core.sshCommand`, pager,
  editor, `credential.helper`, `diff.external`, …, or `GIT_SSH_COMMAND`/
  `GIT_EDITOR`/`GIT_PAGER`), `git difftool`, `filter-branch`, `rebase`,
  `bisect` (widened by M-16) — any spelling of a merge, a close or a push to
  main anywhere on the line asks, saying it may. A line of only git, gh and
  data programs (a commit message or PR body that mentions a merge) is read by
  structure.
- **M-13. GraphQL** also asks for `enqueuePullRequest` (merge queue),
  `deleteRef`, `updateRef`/`updateRefs` (forced or not: one may move main)
  and `createCommitOnBranch` unless it names a non-default branch.
  `branchName: $b` is read from a literal `-f`/`-F b=…`; a variable given
  no literal value (`@file`, `--input`, an expansion, a query in double
  quotes where `$b` is the shell's) asks "may push to main". Every
  `branchName` in the query is read.
- **M-14. One card under Claude.** A human's yes is held by the call's
  `tool_use_id` for 120 s, and if the CLI asks `can_use_tool` about the same
  call (same id, tool and identical input, once) it is answered from that
  yes. Jarvis registers `can_use_tool` only off AUTO, and under AUTO the CLI
  consulted it for nothing (design R2), so under AUTO this is inert: there
  the classifier runs after the owner's yes (the hook returns `{}`) and can
  still deny, but cannot put a second Jarvis card in front of the owner. No
  blanket explicit allow was added.
- **M-16. The backstop is set off by any git exec setting** (the delta
  review of #208, MED-2). Its scope stays per program, because always
  reading the raw line asks for ordinary lines — `git diff main...HEAD &&
  git push -u origin HEAD`, `git branch -d old && git push origin topic`,
  `grep -n 'gh pr merge' merge_gate.py` — that no data-option stripping
  can tell from a push to main. What sets it off is no longer a fixed list:
  any `git -c` or `--config-env`, any `git config` write, any upper-case
  assignment word on the line (`PAGER=`, `EDITOR=`, `VISUAL=`,
  `SSH_ASKPASS=`, `LESSOPEN=`, `GIT_CONFIG_COUNT=`/`KEY_n`/`VALUE_n`, by
  prefix, `env` or `export`), and the options that hand git a program
  (`fetch`/`pull`/`ls-remote`/`clone --upload-pack`, `clone`/`ls-remote
  -u`, `push --receive-pack`/`--exec`, `grep -O`/`--open-files-in-pager`,
  `archive --exec`). The reviewer's other half is taken too: wherever the
  backstop runs, the values of data options — `git commit -m`/`--message`/
  `-F`, `gh pr|issue create|edit|comment|review --title`/`--body`/`-t`/`-b`,
  `gh api -f body=`/`title=` — are left out of its text where the line
  spells them right after their option (at most 64 values; past that none
  are), so `make && gh pr create --body '…gh pr merge…'` asks nothing.
  `git checkout main -- && git push` switches to main (MED-1): only words
  after `--` are paths.

## Known gaps (accident guard, not an adversarial boundary)

A script file (`./ship.sh`, `python merge.py`) or one written on an earlier
call, an alias in a repository whose folder cannot be told, git config
`include` files, git hooks (a `pre-push` hook, `core.hooksPath` set outside
the line) that run on a push, a program the walk does not know that spells
nothing on the line, and `gh issue close` on a pull request's number are not
read. Nor are (the delta review of #208): a git exec setting written by an
earlier call (`git config core.pager …` on one call, `git log` on the next —
the backstop reads one line); `--exec-path` and `GIT_EXEC_PATH`, which make
git run the programs in a folder, not text on the line (`GIT_EXEC_PATH=`
still sets the backstop off); and other git and GitHub front ends —
`git-town`, `hub`, `lab` — which are programs this reader does not know, so
they ask only when the line spells an operation. A commit message or PR body
spelled any way but right after its option (a here-document in `"$(cat
<<EOF …)"`, a value split across quotes) stays in the backstop's text and may
ask.

## For the owner

1. Under Claude AUTO the classifier can still deny a command after the
   owner's yes (M-14). Should an owner yes at this gate return an explicit
   allow from the hook instead? The review advised against a blanket one.
