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

## Tests

`tests/readonly_list_files_check.py`: hermetic (temp HOME/project, fake key,
`runtime.working_dir` patched, no daemon), every leaking form above refused
and leaks nothing, the sentinel never appears and the fake program never
runs; negatives that must still run (`file -m one.magic x`, `file -bmone.magic
x`, `wc -l`, `du -sh .`, `grep -n env -C 2 f`, `grep -f pats f`, `date -f
dates`, `env LANG=C ls`, `timeout 5 ls`, `env`, `find . -name x`). Mutation:
each refusal removed in turn makes the suite fail.
