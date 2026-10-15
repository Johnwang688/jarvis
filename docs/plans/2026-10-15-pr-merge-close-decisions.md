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

## Lead's defaults (not asked; open to amendment)

- **M-6. `gh pr reopen` does not ask.** It neither lands code nor ends a
  review; it undoes a close, which is what the owner wants to control. Adding
  it is one line in `merge_gate._gh` if the owner wants it.
- **M-7. Codex's push still asks as a sandbox widening** (networking off),
  unchanged. Matching Claude there would mean turning sandbox networking on
  for chats, which is a separate owner decision (CLAUDE.md: sandbox
  widening needs a human, never Always).
- **M-8. Where it cannot tell, it asks** with "may merge or close a pull
  request" or "may push to main (what it pushes cannot be told from the
  command)": a refspec, endpoint, method or `gh` verb that is an expansion
  (`git push origin "$BRANCH"`), input appended by `xargs`, a `gh` command
  that is neither built in nor the stock `co` alias (an alias or an
  extension), an unreadable GraphQL query (`-F query=@file`, `--input`), a
  `git -c remote.*/push.*/branch.*/alias.*` override, `GIT_DIR`/
  `GIT_WORK_TREE` on the line, a `cd` it cannot follow, and a line too
  complex to walk. `git push -u origin "$(git branch --show-current)"` counts
  as the current branch.
- **M-9. A push with no refspec is judged where it runs.** The current
  branch is read from the repository's `.git` (git is never run): in the
  shell's own folder (Claude's hook input `cwd`, Codex's item `cwd`; never a
  `cwd` the model writes into the arguments), else the brief's, then each
  `cd` on the line (a `cd` into an existing folder on a line with no subshell
  is where what follows runs), `git -C`, and a wrapper's folder option
  (`env -C`, `--directory`, kept beside the shell's folder). It asks only
  when that branch is protected, or, under `push.default=upstream`, when its
  upstream is. This closes the WP5 "bare push" gap without asking about every
  push.
- **M-10. Git aliases are followed** from `-c alias.*` on the line and from
  the repository's and the owner's git config (`!` aliases are judged as
  lines); `git submodule foreach`, `git rebase -x` and `git bisect run`
  lines are judged; inline interpreter code (`python -c`, `node -e`, a
  here-document fed to one) is read as text for the same operations.

## Known gaps (accident guard, not an adversarial boundary)

A script file (`./ship.sh`, `python merge.py`), an alias in a repository
whose folder cannot be told, git config `include` files, a program the walk
does not know, `curl` to the GitHub API, and `gh issue close` on a pull
request's number are not read. Under Claude AUTO, the owner's yes at layer 2
returns `{}` from the hook as every layer-2 yes does, so Claude Code's
classifier still runs after it; if the classifier also asks, the owner may
see a second card for the same merge.

## For the owner

1. Should `gh pr reopen` ask too (M-6)?
2. Should Codex chats get sandbox networking for `git push`/`gh pr create`,
   so they match Claude's auto mode (M-7)? Today they ask as widenings.
3. Should an owner yes at this gate return an explicit allow from Claude's
   hook under AUTO, so the classifier cannot ask a second time?
