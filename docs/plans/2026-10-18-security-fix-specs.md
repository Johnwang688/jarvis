# Security fix specs from the duplicate-helper probe (2026-10-18)

Source: five Haiku scouts searched for duplicate helpers (reports in
`~/.cache/jarvis-lead/workflows/dup-scout-*.md`). Where helpers had been copied,
some security checks had drifted apart. The lead read the code behind every
item below. SEC-1's core claim was also run in-process, with a temporary HOME
and no DNS lookup. Nothing here has been exploited against a live server.

Each spec is a task card in the `claude-workflows` format. SEC-1, SEC-2 and
SEC-3 own separate sets of files and can be built in parallel. SEC-4 is low
severity and can wait for the helper-consolidation work.

| PR | Severity | Tier (implement / review) | Owner decision needed first |
|---|---|---|---|
| SEC-1 `127.`-prefix names pass the LAN refusal | HIGH | Opus / Opus | No (it tightens a check; see the behaviour change) |
| SEC-2 Invisible/bidi characters on the approval card's origin | MED | Sonnet / Opus | No |
| SEC-3 Outbound surfaces miss the key-shape scrub | MED | Sonnet / Opus | Answered: Q1 yes (2026-10-18) |
| SEC-4 Credential files created, then chmodded | LOW | Sonnet / Sonnet | No |

Common to all four PRs: a worktree per PR and a branch `claude/sec-N-…`.
Acceptance tests are written before the implementation, by someone other than
the implementer, and the implementer may not edit them. Every new negative case
must fail against current `main`; record that failure in the PR body. Tests use
a temporary HOME and config, monkeypatch `socket.getaddrinfo` (never real DNS),
and run under `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`.
Before changing any docstring or notes sentence, grep `tests/` for it: tests pin
docs text.

---

## SEC-1: a hostname beginning `127.` skips the LAN refusal

**goal / behaviour:** only *literal* addresses in 127.0.0.0/8 count as this
machine. A *name* whose first label is `127` (`127.rebind.example`,
`127.0.0.1.nip.io`) is a DNS name like any other. It gets resolved, and it is
refused when it resolves to loopback or to a private address, as
`is_lan_host`'s docstring already promises (`lvh.me` → 127.0.0.1 counts as LAN).

**the defect, verified:** `config.is_loopback` (`jarvis/config.py:64`) returns
`host.startswith("127.")` for any string. `is_lan_host` (`config.py:181`)
returns `False` on `is_loopback(host)` before calling `getaddrinfo`, so
`is_lan_host("127.rebind.example") == False` with no lookup at all. Two callers
are affected:

- `jarvis/tools/web.py:_refusal`, which `fetch_page` runs before the first
  request and every redirect hop. A page, or a redirect, to
  `http://127.anything.example/` whose DNS answer is `192.168.1.1` reaches the
  LAN.
- `jarvis/browser.py:298`, the v1 browser's `*` allowlist. Same hole.

The v2 Browser's SOCKS gate is **not** affected: `browser/policy.host_kind`
classifies `127.rebind.example` as `"name"` and resolves it. But
`browser/policy.py:158` (the `JARVIS_PORT` refusal) and `:272` (detecting an
owner-local URL) call `config._names_this_machine`, which calls `is_loopback`,
so their behaviour changes too (see the behaviour change below).

**change (interfaces, not bodies):**
- `is_loopback(host)`: `LOCAL_HOSTS`, `*.localhost` (Chrome maps these itself;
  keep), or a string that `ipaddress.ip_address` parses as an address in
  127.0.0.0/8. No prefix match on names.
- `is_lan_host(host)`: the early "this machine, not LAN" return uses
  `_names_this_machine(host)`, so the short, hex, octal and integer spellings
  (`127.1`, `0x7f000001`) and IPv4-mapped literals stay owner-local without a
  DNS lookup, exactly as they are today. Everything else goes to `getaddrinfo`.
- Update the docstrings that say "127.x" (`is_lan_host`'s "Loopback
  *spellings* (localhost, 127.x)") so they say literals.

**behaviour change (tightening; record it in the PR body):**
- `fetch_page` and the v1 `*` browser now refuse a `127.<name>` that resolves
  to loopback (`"lan"`); today they allow it.
- `is_face_origin("http://127.rebind.example:8402/")` becomes `False`. The URL
  must still be refused by the next check in every caller (`"lan"` in v1). In
  v2, the gate must still refuse the connection once it has resolved the name.
  Prove both with tests (below), not by argument.
- v2 `policy.py:272`: an owner typing `127.rebind.example:3000` no longer
  counts as opening a local server. That is correct under BR-1a, because only
  the owner's real loopback spellings admit a port.

**concurrency model:** pure functions, no shared state. `getaddrinfo` now runs
for `127.<name>` hosts where it never did, so a hanging resolver costs what it
already costs any other name.

**reuse map:** `_names_this_machine` (config.py:100) already parses every literal
spelling; reuse it and do not add another parser. Leave
`browser/policy.host_kind`/`address_class` alone; they are v2's own and
already correct. Do not touch the Host-header allowlists (separate work).

**non-goals:** merging v1's and v2's address classifiers; DNS pinning (TOCTOU
between check and connect is a known limit, not this PR); the duplicated
`local=` predicate in `browser/drive.py:125` and `look_capture.py:21`.

**files owned:** `jarvis/config.py`, `tests/browser/policy_check.py` (or a new
`tests/loopback_names_check.py`), and the v2 browser gate test file that holds
the resolved-name case. **Must not touch:** `jarvis/v2/browser/policy.py`
(unless a test proves it needs a change; then stop and report), `rules.py`,
`protected_state.py`.

**acceptance tests (DNS monkeypatched):**
1. `is_loopback` is `True` for `127.0.0.1`, `127.5.6.7`, `[::1]`, `localhost`,
   `foo.localhost`, `0.0.0.0`, `""`; `False` for `127.rebind.example`,
   `127.0.0.1.nip.io`, `127.`.
2. `is_lan_host("127.rebind.example")` → `True` when it resolves to
   `192.168.1.10`, and `True` when it resolves to `127.0.0.1`. `getaddrinfo` was
   called.
3. `is_lan_host` on `127.1`, `0x7f000001`, `2130706433`, `::ffff:127.0.0.1`,
   `localhost.` → `False`, and `getaddrinfo` was **not** called.
4. `web._refusal("http://127.rebind.example/")` → `"lan"` (resolving to
   192.168.1.10). The same holds for a redirect hop to that URL in
   `fetch_page`'s loop (fake transport).
5. `web._refusal("http://127.rebind.example:8402/")` is not `None` when it
   resolves to 127.0.0.1.
6. v1 `Session` allowlist with `*`: `127.rebind.example` → not allowed.
7. v2: a Browser navigation to `127.rebind.example:<control port>`, with that
   name resolved to 127.0.0.1, is refused by the gate. Use the existing
   isolated gate harness; never the live daemon.
8. Unchanged: every case already in `tests/browser/policy_check.py`,
   `tests/v2/owner_browser_check.py`, `tests/v2/devurl_check.py` and
   `tests/v2/terminal_check.py` still passes.
9. Mutation: restoring `startswith("127.")` makes cases 2, 4 and 6 fail.

**tier + reason:** Opus. The check guards a security boundary, a mistake is
silent (a hole, not a crash), and the change has v1/v2 callers whose
behaviour is easy to miss. The review is also Opus, focused on every caller of
`is_loopback` and `_names_this_machine`.

**Amendment (lead, 2026-10-18): tighten-only.** CLAUDE.md forbids loosening a
safety check without the owner, so no URL that is refused today may become
allowed. Three points replace the card's text above where they disagree:

- `is_lan_host` keeps its early "not LAN" return on the narrowed
  `is_loopback`. It does **not** switch to `_names_this_machine`. The card
  said the short, hex, octal, integer and IPv4-mapped spellings and
  `localhost.` "stay owner-local without a DNS lookup, exactly as they are
  today". That was wrong about today: on main, `is_lan_host` sends
  `0x7f000001`, `2130706433`, `::ffff:127.0.0.1` and `localhost.` to
  `getaddrinfo`, and refuses them when they resolve to loopback, so
  `fetch_page` and the v1 `*` browser refuse them on every port. They still
  go to `getaddrinfo`, and `127.1` now does too (a tightening). Only
  `LOCAL_HOSTS`, `*.localhost` and the literals `ipaddress.ip_address`
  parses in 127/8 skip the lookup. Acceptance case 3 is rewritten to match.
- `is_face_origin` keeps refusing everything it refuses today. A host whose
  first dot-separated label is `127` (`127.rebind.example`,
  `127.0.0.1.nip.io`) on a control port is still `True`, although
  `is_loopback` no longer counts it as local. On any other port it is `False`
  (`is_lan_host` refuses it). This replaces the behaviour-change bullet that
  made `is_face_origin("http://127.rebind.example:8402/")` `False`.
- A new case 10, "no loosening": for every host spelling the cases use, on
  no port, 3000, a control port and 8403, with the `127.` names answering
  loopback or a LAN address, every URL that `web._refusal` and the v1 `*`
  allowlist refuse on main is still refused. The comparison uses a fixed
  table recorded from main, not the old code. The canonical loopback
  spellings off the control ports, and a public name, stay allowed.

`policy.loopback_port` returning `None` for `127.<name>` stays (case 7d): such
a name can no longer count as an owner-opened local server.

---

## SEC-2: invisible and bidi characters reach the approval card's origin line

**goal / behaviour:** the attribution string on approval surfaces (`task <id>
"<brief>"`) contains no zero-width, bidi-override or other unseen characters,
in v2 and in v1.

**the defect, verified:** `jarvis/v2/approvals.py:103 label()` (2026-09-15)
collapses `\s+` and strips backticks only. U+202E, U+200B and the other
`untrusted.UNSEEN_CATEGORIES` characters are not whitespace, so they survive.
`permissions.py:225 origin()` feeds it `task.brief`, which is model-chosen, and
the result is the `origin` field on HUD and Discord approval cards
(`approvals.py:310`, `:578`). A model-written brief can reorder or hide part of
the text the owner reads before approving. v1's `jarvis/tasks.py:135 _label`
is the original copy and has the same gap. `clean_line` (same file, line 54,
2026-10-08) already does it right and says it is "`label()`'s rule
generalised".

**change:** `label()` uses `clean_line(..., cap=60)` for both `name` and
`origin_id`. v1 `_label` uses the same rule. Prefer importing `clean_line`;
if v1 must not import `jarvis.v2` (no v1 module under `jarvis/` or
`jarvis/tools/` does today), use `untrusted.unseen` plus the backtick strip
directly and say why in a comment. A truncated label now ends in "…"; it is
no longer a hard cut.

**also check and report (no change unless broken):** whether the HUD approval
card and the Discord DM renderer strip these characters themselves. Fixing it
server-side is required either way; the report says whether it was
exploitable or only latent.

**concurrency model:** pure function.

**reuse map:** `approvals.clean_line`, `untrusted.unseen` and
`untrusted.UNSEEN_CATEGORIES`. Write no new character table.

**non-goals:** other single-line cleaners (`commands._clean`,
`owner_ops._clean`). They are consolidation candidates, not part of this PR.

**files owned:** `jarvis/v2/approvals.py`, `jarvis/tasks.py`,
`tests/tasks_check.py`, `tests/v2/permissions_check.py` (or the approvals test
that holds `label`). **Must not touch:** `permissions.py` logic, the renderers.

**acceptance tests:**
1. `label("t1", "ok‮evil")`, `"a​b"`, `"x y"`, `"x⁦y"` and
   a NUL: none of those characters appear in the result, and the result is a
   single line.
2. The same characters in `origin_id` are stripped too.
3. A name longer than 60 characters is capped and ends in "…". Backticks are
   removed.
4. v1 `_label` gives the same results for the same inputs.
5. Existing label-format assertions in `tests/tasks_check.py` and
   `tests/dispatch_toolset_check.py` still hold. If one pins the old
   hard-cut-at-60 behaviour, update it in the same PR and say so.
6. Mutation: reverting `label()` makes case 1 fail.

**tier + reason:** Sonnet. Two small functions with a canonical helper already
in place and fully testable. It sits on an authorization surface, so the
review is Opus.

---

## SEC-3: outbound surfaces miss the key-shape layer of the scrub

**goal / behaviour:** text Jarvis sends **off the machine** goes through
`redact.scrub_text` (credential-file values, provider key shapes, bearer/JWT
shapes). Any text that cannot be scrubbed is withheld, never sent as is.

**the defect, verified:** `redact.scrub_text` (2026-10-15) says it is "one
scrub for model-written text bound for a surface". Older surfaces still call
`secrets.scrub` alone, which only knows Jarvis's own credential values. A
`ghp_…`, `sk-…` or JWT that is not in a Jarvis-owned file passes:

- `jarvis/v2/discord/reporter.py:140 _clean`: Discord task reports.
- `jarvis/v2/discord/mirror.py:123, 154, 164, 181, 1099, 1116`: Discord chat
  mirror (names, replies, spoken text).
- `jarvis/v2/diff_review.py:235 _clean` uses `secrets.scrub` and has **no
  failure guard**: an exception propagates, whereas `subagents._clean`
  withholds.
- `jarvis/v2/chat_git.py:621 _scrub` strips URL userinfo with
  `gitops._URL_USERINFO`, which matches only `https?://`. Use
  `secrets.strip_url_credentials` (any scheme) first. This one is LOW
  severity, because git output rarely carries non-http credentials.

**Q1 — ANSWERED 2026-10-18 by the owner: yes, the Discord chat mirror gets the
full key-pattern scrub (`scrub_text`) too.** The original question: should the Discord **chat mirror**
get the full `scrub_text`? `TOKENISH` (`\b(sk|oat|rt)[-_]…{12,}`) can redact
ordinary identifiers (`rt_tokenizer_config`) in mirrored replies. The
recommendation is yes, because Discord leaves the machine and a false
`[redacted]` costs little. The reporter and diff_review changes do not depend
on this answer.

**change:** the listed call sites call `scrub_text`. Each Discord site
withholds on exception, with a marker matching `chat_git._scrub`'s
("[output withheld: it could not be scrubbed]"), and posts nothing
unscrubbed. `diff_review._clean` withholds the same way. Keep the reporter's
"scrub before rendering caps" order (`_scrubbed`). `scrub_text` replaces
`[redacted]` spans, so check that every cap still holds after replacement.
`chat_git._scrub` calls `secrets.strip_url_credentials(text)` instead of the
gitops regex. Leave gitops's own use alone (gitops is out of scope).

**not in scope (deliberately left on `secrets.scrub`):** `tools/__init__.py:481`
(tool results to the model), `hud_api.py:623/903`, `titles.py`,
`speech_summary.py` and `mcp.py:95`. These are on-machine surfaces or text
sent to the model. Listing them here means the next reader does not
"fix" them by accident; changing them is a separate owner call.

**concurrency model:** `scrub_text` reads credential files, so call it outside
any lock that matters (redact.py's own note). The reporter and mirror post
from their own threads; do not move the scrub inside a send lock.

**reuse map:** `redact.scrub_text`; `secrets.strip_url_credentials`; the
withhold pattern in `chat_git._scrub` and `subagents._clean`.

**files owned:** `jarvis/v2/discord/reporter.py`, `jarvis/v2/discord/mirror.py`, `jarvis/v2/diff_review.py`, `jarvis/v2/chat_git.py`, and
their check files under `tests/v2/`. **Must not touch:** `redact.py`,
`credential_patterns.py`, `secrets.py`, `discord/rest.py` (its token-only
`redact` is a different job).

**acceptance tests (fake Discord transport, temporary config, fake key
strings built at runtime so no real-looking key is committed):**
1. A report whose text contains a `ghp_`+36 string and a JWT that are in no
   Jarvis file: the posted payload contains neither.
2. Make `scrub_text` raise: the reporter posts the withheld marker and never
   the raw text. The same holds for the mirror.
3. `diff_review._clean` with `scrub_text` raising returns the withheld
   result and does not raise.
4. `chat_git._scrub("fatal: postgresql://u:pw@h/db")` does not contain `pw`.
5. Render caps (400/1500/1024/6000) still hold when a short secret is
   replaced by the longer marker.
6. Mutation: reverting any one site makes its case fail.

**tier + reason:** Sonnet. A mechanical switch at known call sites with a
canonical helper. Credentials are involved, so the review is Opus.

---

## SEC-4 (LOW): credential files are created with the umask mode, then narrowed

**goal / behaviour:** token files are created at 0600 from the start, never
widened, never followed through a symlink.

**the defect, verified:** `jarvis/tools/discord.py:261` (bot token) and
`jarvis/google_auth.py:282` call `write_text`, then `chmod(0o600)`.
`spotify_auth._store` does the same through a temporary file. On this
single-user machine, `google_auth` already makes the parent folder 0700 first,
so the real exposure is small. `discord.py` and `spotify_auth` need checking.
Separately, `jarvis/v2/subagents.py:648 _compact` creates with `O_EXCL` 0600
but without `O_NOFOLLOW` or fsync, unlike the recipe in
`browser/host._write_private`.

**change:** one shared private-write helper (bytes in, 0600, `O_EXCL |
O_NOFOLLOW`, fsync, `os.replace`, unlink on failure), moved from
`browser/host._write_private` into a v1-level module that both v1 and v2 may
import. `host.py` re-exports or calls it. The three credential writers and
`subagents._compact` call it. `stt_settings.update` (an inline copy of the
same recipe) may switch in the same PR.

**files owned:** the new helper module, `jarvis/v2/browser/host.py` (import
only), `jarvis/tools/discord.py`, `jarvis/google_auth.py`,
`jarvis/spotify_auth.py`, `jarvis/v2/subagents.py`, `jarvis/stt_settings.py`,
and the matching check files. **Must not touch:** `secrets.py` credential-name
rules, the `.env` handling.

**acceptance tests:** with a umask of 0o022, each writer's file is 0600 at
every point (check the mode of the temporary file before the rename, by
patching `os.replace`); an existing symlink at the target path is refused, not
followed; a failed write leaves no temporary file behind; Spotify's
rotating-token write still persists before use (integration rule).

**tier + reason:** Sonnet. Medium blast radius (three integrations' setup
paths), a clear pattern to copy, low severity.

---

## Found but not filed as security fixes

- Seven inline Host-header allowlists (`routes.py:91`, `hud_api.py:1410`,
  `terminals.py:1937`, `vex.py:2315`, `devservers.py:751`, `projects.py:165`,
  `daemon.py:2781`). They are identical today; consolidate them behind one
  `config` helper in the cleanup work so a future spelling is added once.
- The HUD's two `isModifierKey` key sets disagree (CapsLock vs
  Super/Hyper/FnLock: `lib/frameguard.ts:49`, `lib/remote.ts:303`). Possibly
  relevant to the "no frame takes the keyboard" rule, so ask before merging
  them.
- `protected_state._under` returns `False` for path == folder where the other
  three copies return `True`. Probably deliberate; confirm before any
  consolidation.
