# Heredoc bodies in v2's layer 2: plan (2026-11-16)

Status: **narrowed by the owner, 2026-11-16** (answers in §10). Only the
reported `credentials-read` case is patched now (HD-0); the rest is deferred.
Lead: Claude (Opus).

## 1. The report

A Claude sub-agent ("B3 stages 2-4 fixes", VEX-override) ran a read-only log
check and got an `always-ask rule credentials-read` card:

```
cd …-wt-b3stages1116 && grep -n "lx:" build-sim/b1.log | head; …; python3 - <<'EOF'
import csv,re
for f in ['build-sim/s1.log']:
    pass
EOF
grep -c "TIMED OUT" build-sim/s1.log
```

The Python line `pass` was judged as a shell command, and `pass` (the
password store) is in `_CREDENTIAL_READ_STEMS`
(`jarvis/v2/permissions.py:260`). Reproduced in-process with a temp HOME:
the same heredoc with `x = 1` in place of `pass` is not asked.

## 2. Root cause

`always_ask_match` (`jarvis/v2/permissions.py`, the loop over
`rules.segments(text)`), `_segments_of` (the inner strings) and
`protected_state._analyse` (`rules.segments(command)`, the gate-state and
env-file reading) split the **raw** line. `rules.segments` breaks at
newlines and knows nothing of here-documents, so every body line becomes a
"command".

v1 already solved this (review round 3, `rules.py` "here-documents"):
`_inline_heredocs` moves each body into its operator as a here-string, and
`_Walk` reads a body as commands only where a shell is fed it
(`_shell_command(...)` → reads stdin). `decide`, the owner-op reading and
the glob reading use it. v2's layer-2 readings above never adopted it.

## 3. How often (from `~/.local/share/jarvis/v2/decisions.jsonl`)

Always-ask asks with a here-document on the line, against all asks of
that rule:

| rule | with heredoc | all |
|---|---|---|
| gate-state | 108 | 109 |
| credentials-path | 25 | 94 |
| vex-device | 22 | 88 |
| git-push-force (merge reason) | 22 | 24 |
| git-push-unsure | 12 | 16 |
| credentials-inner | 6 | 6 |
| credentials-read | 4 | 4 |

The samples are almost all `python3 - <<'EOF'` file edits and
`cat > file <<'EOF'` writes on VEX work (C++ `static Foo*` read as a glob,
JS `.map((m) => a > b)` read as a redirect into `/`, a comment saying
`git push --force`). A heredoc row is not proof of a false positive; HD-1's
replay (§6) classifies each one.

## 4. Baseline that must not regress (current main, `always_ask_match`)

Asked today, and must still ask:

- `bash <<'EOF'⏎pass show github⏎EOF`, `sh <<EOF`, `zsh`, `bash -s`,
  `env bash`, `uv run bash`
- `source /dev/stdin <<'EOF'`, `. /dev/stdin <<'EOF'`
- `ssh host <<'EOF'`, `xargs -I{} sh -c {} <<'EOF'`
- `cat <<'EOF' | sh`, `cat <<'EOF' | bash -s`
- `eval "$(cat <<'EOF' … EOF)"` (credentials-inner)
- `bash <<'EOF'⏎cat ~/.ssh/id_rsa⏎EOF` (credentials-path)
- the multi-field cases in `tests/v2/permissions_check.py`
  `command_field_checks` (`{"command": "cat <<EOF", "script": "git push
  --force origin topic"}` and the others): each field is judged alone too.

Not asked today, and out of HD-1's scope (gap G, §7):
`echo $(pass show github)`, `` x=`pass show github` ``,
`bash <<< 'pass show github'`, `cat <<EOF⏎$(git credential fill)⏎EOF`.
None of them is denied at layer 1 either; v1 `decide` says ASK, so they go
on past layer 2 (the allowlist cannot cover a substitution; under AUTO the
provider's reviewer sees them).

Inline source is not read by layer 2 in any spelling (`python3 -c
"…open('~/.ssh/id_rsa')…"` is not asked), so treating a Python body as data
loses no coverage that `python -c` has. Layer 1's name refusal reads the
whole text and is unchanged (`python3 - <<'EOF'⏎open('.env')` is still
refused).

## 5. Design

### 5.1 One shared reading: `rules.layer2_readings(text)`

Returns the texts layer 2 should segment, in place of the raw line:

1. The line with every body moved into its operator as a here-string
   (`_inline_heredocs(keep=True)`), so the operator's own segment keeps its
   redirects (`cat > ~/.config/jarvis/allowlist.json <<'EOF'` still writes
   the gate's state).
2. **Each body a shell may run, as its own text**, judged like any line
   (segments, `started_commands`, `_inner_segments`, `_cd_bases`).

A body counts as **data** (not added) only when **all** hold:

- its consumer, the program of the segment holding the operator, read past
  wrappers and runners (`starts`, as `started_commands` does), is in a
  short closed table `HEREDOC_DATA_CONSUMERS`: `cat`, `tee`, the `_PYTHON`
  family, `node`, `wc`, `grep` to start;
- that segment's output is not piped on (it is the last member of its
  pipeline);
- the delimiter is quoted, **or** the body holds no `$(` and no backtick
  (an unquoted body's substitutions are live in bash).

Anything else is **shell**, today's reading: a consumer in `SHELLS`, a
`source`/`.`, `eval`, `ssh`, `xargs`, `$X` or any unknown program, or a
pipe onward (`cat <<EOF | sh`). **The table lists what is safe, not what is
dangerous**, so a missing entry costs a prompt, never a hole.

### 5.2 Fail toward today

- `_inline_heredocs` raising `_TooComplex`, or flagging `unsure`: return the
  raw line unchanged (today's reading: every body line a command). Never
  less strict than main.
- A consumer whose program cannot be read: shell.
- The body text still reaches every whole-text check that reads it today
  (layer 1's name refusal, `secret_service_reading`, the vex reading and its
  body backstop).

### 5.3 Wiring

- **HD-1** `always_ask_match`, `_segments_of` and `_inner_segments` call
  `layer2_readings` per judged text. This fixes credentials-read/-path/
  -command, the git push/merge rules and every stem rule at once.
- **HD-2** `protected_state._analyse` (the gate-state and env-file reading)
  segments `layer2_readings(command)`. That detector is shared by v1 and v2
  (CLAUDE.md: "keep the shared detector, v1/v2 parity"), so both change
  together. It sits next to SELF_PROTECTED state: Opus reviews it.

### 5.4 Not changed

- The vex body backstop (`rules._body_marker`, V-2/VX-1) reads every body
  on purpose. It stays unless the owner says otherwise (question 3).
- Layer 1, the multi-field reading, the allowlist and the reviewer layers.

## 6. Tests and evidence

- New `tests/v2/heredoc_layer2_check.py` (temp HOME, fake config, no
  daemon):
  - **False positives fixed**: sanitized copies of logged lines (Python
    `pass`, `git config --list` inside a Python body, `cat > notes.md` with
    `vercel --prod`/`gh pr merge 5`, C++ `static Foo*`, JS `a > b`).
  - **Still asked**: every §4 line, plus `$X <<'EOF'`, `cat <<'EOF' | tee
    f | sh`, an unquoted body with `$(…)`, a `<<-` body, two heredocs on
    one line (one data, one shell), a body with no terminator, an unsure
    delimiter (falls back to raw).
  - **Mutation evidence**: the "still asked" set fails with `bash` added to
    the data table, with the pipe check removed and with the quoted-or-no-
    substitution check removed; the "fixed" set fails against main.
- `tests/v2/permissions_check.py` and `tests/rules_check.py` pass unchanged
  (the multi-field heredoc cases especially), under
  `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`.
- **Offline replay**: run `always_ask_match` (HD-1) and `command_touch`
  (HD-2) over every heredoc line in `decisions.jsonl`, old vs new, in a temp
  HOME. Report the counts that stop asking and hand-check **every** line
  that changes: each must be a data body by §5.1. Sum only; the log is not
  copied into the repo.

## 7. Gap G (separate, tightening): substitutions and here-strings

`always_ask_match` reads no `$(…)`, backticks or `bash <<< '…'`, so
`echo $(pass show x)` skips credentials-read, which is meant to be asked
with no Always and never to be the reviewer's. **HD-3** would add each
substitution's inner text (`rules._substitutions`) and each here-string
fed to a shell (`_shell_command`'s stdin) to `layer2_readings`, with the
same bounded nesting as `_inner_segments` (past the bound:
`credentials-inner`'s ask). Tighten-only; it adds asks.

## 8. Packages

| id | what | review |
|---|---|---|
| HD-1 | `layer2_readings` + always-ask wiring + tests + replay | Sol verifier, then Opus (boundary) |
| HD-2 | gate-state/env-file reading via `layer2_readings` | Sol verifier, then Opus (SELF_PROTECTED-adjacent) |
| HD-3 | gap G: substitutions and shell-fed here-strings | Sol verifier, then Opus |

Implementers: GPT-6.1 Sol `codex exec` in a `--shared` clone, lead
commits (owner 2026-11-15). HD-2 builds on HD-1's helper; HD-3 is
independent of HD-2.

## 9. Questions for the owner

1. Scope: HD-1 alone, or HD-1 and HD-2 (gate-state, the ~108 prompts)?
2. Gap G: fix now as HD-3, or later?
3. The vex body backstop: keep reading every body (as decided in V-2), or
   let data bodies (§5.1) skip it too?

## 10. Owner answers (2026-11-16)

1. **"Let's just patch this one and then defer the rest."** The owner plans
   to give Jarvis more autonomy as trust grows, which would make the wider
   work redundant. So: **HD-0**, `credentials-read` only, below. HD-1 and
   HD-2 are deferred.
2. Gap G: **later.**
3. The vex body backstop: **let data bodies skip it**, recorded for when
   the deferred work is picked up. Not part of HD-0.

### HD-0 (built now)

Only the `credential_read` kind in `always_ask_match` reads a line through
the heredoc-aware reading. Every other rule, `_inner_segments` and
`protected_state` are unchanged. Per judged text, all or nothing:

- the line's bodies are inlined as here-strings (`rules._inline_heredocs`);
- that reading is used only on a **plain line**, where **every** here-string
  (a former body or one written as `<<<`) goes straight to `cat`, `tee`,
  `wc`, `grep`, `python3 -` or `node -`, as a bare name with no wrapper; is
  not piped on; and has no redirection beyond plain path reads and writes
  (no `>&`, no `/dev/fd`, `/proc`);
- a plain line has no unquoted `( ) { } <( >(`, nothing live (`$(`,
  backticks) outside single quotes, no program run by path, no `PATH=`-style
  assignment, and no shell or name-changer anywhere on it (`source`, `.`,
  `eval`, `exec`, `coproc`, `alias`, `shopt`, `xargs`, `ssh`, `env` …);
- otherwise, or when the inlining raises or flags itself unsure, or a segment
  or here-string cannot be placed: the raw line, as on main.

The first version checked only the consumer's name and the text right after
it; the Opus review of #396 found bodies reaching a shell through `{ …; } |
sh`, `if …; fi | sh`, `> >(sh)`, `tee >(sh)`, `exec 3> >(sh)`, `coproc`, a
`cat` function or alias, `./cat`, `PATH=`, write-then-`sh file`, and
`python3 -c`/`node -e` reading stdin. Each now keeps the raw reading and is
a case in the regression test.

So HD-0 only ever removes asks whose cause was a data body; it never reads
less than main.
