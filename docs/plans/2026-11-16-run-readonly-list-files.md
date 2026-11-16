# run_readonly: options that read paths or settings from another file (2026-11-16)

## Why

`run_readonly` (`jarvis/tools/shell.py`) is ungated: its checks are the whole
boundary. It judges every word on the line as a path (the credential-folder
judge, and `secrets.is_credential` after PR #388). A program that reads *the
paths it opens from another file* opens files that are never on the line.
PR #388's Sol verifier showed `file -f list.txt` (list naming `.netrc`, with a
`%s` magic rule) printing the protected file's first string.

The comment above `rules._NO_HAZARD` says options that read a named file
(`grep -f`, `sort --files0-from`, `date -f` …) are not hazards because "the
file is named on the line". That holds for files whose **content** is the
input (patterns, dates). It is wrong for files whose content is **more
paths**, and for the wrapper options that change where the paths resolve or
what the program runs with.

## Audit (installed binaries, `--help` plus probes; temp HOME, fake contents)

Allowlist: ls cat head tail wc grep rg find file stat du df date whoami
hostname uname pwd which env ps uptime tree echo ss. Installed: uutils
coreutils 0.10.0 (ls cat head tail wc stat du date whoami uname pwd env echo),
GNU coreutils 9.7 df, GNU grep 3.12, ripgrep 15.1.0, GNU findutils 4.10.0,
file 5.46, procps-ng 4.0.4, iproute2 6.19.0 ss, hostname 3.25; tree is not
installed.

Probe: `~/.cache/jarvis-lead/listfiles/probe.py` (fake `~/.ssh/id_rsa` in a
temp HOME, the list files in a temp project). Results at `main` 07a9b4ac:

| Form | Reads | At main |
|---|---|---|
| `file -f L`, `-fL`, `--files-from=L` (and prefixes) | every path in L, with the magic rules | **content leak** |
| `file -m A:B` (colon list) | B as a magic file; parse warnings quote it | **content leak** |
| `file -m DIR` | every file in DIR as magic, symlinks followed | **content leak** (`DIR/link -> ~/.ssh/id_rsa`) |
| `env -f V PROG`, `--file=V`, `-fV`, `--fil=V`, `env -S'-f V' PROG` | variables from V: `PATH`, `LD_PRELOAD` … | **runs any program** |
| `env -C D PROG`, `--chdir=D` | relative operands resolve in D, judged in the project | **content leak** (`env -C ~/.ssh cat id_rsa`) |
| `wc --files0-from=L`, `du --files0-from=L` | every path in L | counts/sizes and names |
| `find -files0-from L` | starting points from L | names below them |
| `grep -f`, `rg -f`, `date -f`, `ss -F`, `du -X`, `grep --exclude-from`, `rg --ignore-file`, `date -r` | the named file only, as patterns/dates/filters/metadata | judged on the line; **not changed** |
| `tree --fromfile`, `--infofile` | operands/value, named | not installed; judged on the line; not changed |
| ls cat head tail stat df date uname pwd whoami uptime echo ps | no file-list option (`--files0-from` rejected by each) | — |

`env -a NAME` (argv0) was checked: these uutils binaries are per-utility, so
argv0 changes only the message prefix. Other wrappers `run_readonly` peels
(`timeout`, `nice`, `nohup`, `setsid`, `stdbuf`, `ionice`, `command`, `time`)
have no option that reads a file or changes folder (`time -o` is already
refused as a write).

## Decision

Refuse, in `run_readonly`, before anything runs (cheap and certain; judging
every listed path would mean reading the list, NUL/newline/colon formats and
resolving each against the right folder):

1. **env, in wrapper or program position** (every `env` word up to and
   including the program word): `-f`/`--file`, `-C`/`--chdir`,
   `-S`/`--split-string` — in a cluster before a value letter, glued, `=`,
   or any unambiguous long prefix (clap's infer-long-args and GNU getopt
   both take prefixes). `-S` was partly refused already (`unwrap_sure`), but
   `env -S'-f V' ls` ran.
2. **file**: `-f`/`--files-from` (cluster, glued, prefix); `-m`/`--magic-file`
   whose value holds `:` or does not place to an existing regular file.
3. **wc, du**: any long option that is a prefix of `--files0-from`.
4. **find**: the primary `-files0-from`.
5. **every allowlisted program**: a long option starting `--files0-from` or
   `--files-from` in full (a safety net if an implementation gains one).

Program options are scanned word by word without consuming values, so a
word that could be either an option or a value counts as an option: errs
toward refusing. Clusters are walked as getopt walks them, so `file
-bmfoo.magic x` names magic file `foo.magic` and is not read as `-f`.

Not changed: the read-a-named-file options above; v2 shells (Claude/Codex
layers judge their own commands; `run_command` asks).

## Verifier round 1 (GPT-6.1 Sol, 2026-11-16)

The run was stopped by OpenAI's cybersecurity filter partway through; its
probe results (`~/.cache/jarvis-lead/listfiles/cx1/repo/tests/
readonly_verifier_03bf04d5/initial.json`) showed three more, all fixed here:

6. **`file` with no `-m` loads `~/.magic.mgc`, else `~/.magic`, else
   `~/.magic/magic.mgc`.** A planted `~/.magic -> ~/.ssh/id_rsa` was quoted
   in a parse warning by a plain `file x`. Refused when any of those is a
   symlink or lies in a credential folder. `file -m X` also loads `X.mgc`
   first: same rule for `X` and `X.mgc`.
7. **Glued bare tails** (older than this change): `date -fkeylink`, `file
   -mkeylink`, `keylink` a relative symlink to the key, printed it, because a
   glued tail was judged only when it looked like a path. Every tail is now
   judged by the folder judge; only path-like tails take the recursive
   holds-a-credential-folder check (no new false refusals in `grep -rn`,
   `head -n5`, `ps -ef`, `df -h`, `du -sh`). PR #388 judges bare tails by
   name; whichever merges second keeps both judges.
8. **`ionice -p/-P/-u`** (any position up to the program, long prefixes):
   acts on processes already running — `ionice -c 3 -p PID cat x` set PID's
   I/O class to idle, then failed on `cat`.

## Review round 2 (Opus, adversarial, 2026-11-16)

Findings and what was done (probes in `~/.cache/jarvis-lead/listfiles/review/`):

| # | Finding | Sev | Done |
|---|---|---|---|
| 1 | `[[:class:]]` patterns: Python's glob matched nothing, so nothing was judged; dash expanded `~/.s[[:lower:]]h/id_ed25519` (older) | HIGH | fixed by 9 |
| 2 | A pattern expanding to a planted `-f`/`--files0-from`/`-p<pid>` file became an option none of the checks saw; also `find -de?ete` with a planted `-delete` | HIGH | fixed by 9 |
| 3 | A word starting with `-` was never judged whole: `cat -- -keylink` (older) | HIGH | fixed by 9 |
| 4 | A literal (quoted, escaped, glued, after `=`) `~` judged as if expanded (older; and in `_magic_value`) | HIGH | fixed: both readings judged |
| 5 | Any `-m` word, even another option's value (`file -F -m m x`), skipped the home-magic check | HIGH | fixed: always checked |
| 6 | Home magic as a folder (`~/.magic.mgc/link`) read every file in it | HIGH | fixed: anything but a plain regular file refused |
| 7 | procps `watch` (no `-x`) runs its words through `sh -c` (older) | HIGH | fixed: refused like xargs |
| 8 | rg reads `.ignore`/`.rgignore` from searched folders, links followed; invalid glob lines quoted (older) | MED | **deferred**, own task |
| 9 | Other processes' environments: `ps eww`, `/proc/PID/environ` (older, another class) | MED | **deferred**, own task |
| 10 | The any-program `--files-from` net refused `rg -F -e --files-from src` | LOW | fixed: not for grep/rg/echo |
| 11 | `.mime.mgc`/`.mime` companions under `-i` | LOW | fixed: judged with `.mgc` |

**9. dash expands the words itself.** Python's glob and shlex only guess at
what dash makes of a line. Once the line holds no operator, substitution or
`$`, `/bin/sh -c 'printf "%s\0" LINE'` can only expand quotes, `~` and
patterns into its printf builtin, which runs nothing. `run_readonly` then:
judges every resulting word as a path, as it stands; runs the name check over
them (`cat .e[[:lower:]]v`); refuses an option word that only a pattern
produced; and refuses any pattern among the wrappers' words or as the program
(`timeout * ls` beside files `5` and `rm` runs `rm`). Too slow (5 s), too
many words (2,000) or a syntax error: refused. The shell runs in the same
folder and environment as `_run`, so locale and HOME agree. grep patterns
such as `'[[:space:]]'` keep working.

Accepted false refusals, deliberately: a literal file named like a refused
option after `--` (`file -- -foo`, `wc -- --files0-from`) and a refused
letter as another option's value (`file -F -f x`). Honouring `--` needs exact
value consumption (`file -F -- -f list` makes `--` the separator and `-f`
live), so the scan stays fail-closed.

Recorded, not changed (names and sizes, never content, and older than this
change): `ls -Ra ~`, `find ~ -type f`, `du -ab ~` and `find -L`/`du -L`
through a nested symlink list credential file names and sizes. The design
refuses recursion only for the searchers that read content (`grep -r`, `rg`).

## Tests

`tests/readonly_list_files_check.py`: hermetic (temp HOME/project, fake key,
`runtime.working_dir` patched, no daemon), every leaking form above refused
and leaks nothing, the sentinel never appears and the fake program never
runs; negatives that must still run (`file -m one.magic x`, `file -bmone.magic
x`, `wc -l`, `du -sh .`, `grep -n env -C 2 f`, `grep -f pats f`, `date -f
dates`, `env LANG=C ls`, `timeout 5 ls`, `env`, `find . -name x`). Mutation:
each refusal removed in turn makes the suite fail (41 of 41).
