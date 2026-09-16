# WP10a — Discord threads, embeds, templates

## Built
- `jarvis/v2/discord`: injectable synchronous REST, pure templates, background Reporter.
- REST reuses v1 `_load_bundle`, `_fail`, Bot authorization and 30-second timeout.
- Reporter creates/adopts one task thread, persists its id through the fixed store,
  and keeps `discord_status_message_id` in sibling `tasks/<id>/discord.json`.
- Sidecar also records started-message id, channel, last phase and open question.
- Runner status alone supplies embed fields; title uses project name/task id metadata.
- Timer coalesces the latest state; edits are spaced >=5 seconds per Discord channel.
- Blocked/failed transitions, verified then done, changed questions; no provider chatter.
- Milestones truncate with ellipsis at 400; report preview 1500; full approval preserved.
- `counters`: skipped events without channels, errors, threads, posts, edits.
- Failures are logged by exception class only, counted and isolated from bus publishing.

## Verified Discord REST contract (2026-09-15; API v10)
- [Create channel](https://docs.discord.com/developers/resources/guild#create-guild-channel): `POST /guilds/{guild_id}/channels`, `{name,type:0}`.
- [Create thread](https://docs.discord.com/developers/resources/channel#start-thread-without-message): `POST /channels/{channel_id}/threads`, `{name,type:11}`; names 1–100 characters.
- [Create/edit message](https://docs.discord.com/developers/resources/message): `POST /channels/{channel_id}/messages`; `PATCH /channels/{channel_id}/messages/{message_id}`; `content`, `embeds:[embed]`.
- [Multipart](https://docs.discord.com/developers/reference#uploading-files): `payload_json`, `files[n]`, matching `attachments:[{id:n,filename}]`; never split long text across posts.
- [Unarchive](https://docs.discord.com/developers/resources/channel#modify-channel): `PATCH /channels/{thread_id}`, `{archived:false}` before status edits.
- [Thread posting](https://docs.discord.com/developers/topics/threads#active--archived-threads) auto-unarchives unlocked threads; locked threads require MANAGE_THREADS.
- [Message/embed limits](https://docs.discord.com/developers/resources/message#embed-object-embed-limits): content 2000, title 256, description 4096, <=25 fields, field value 1024, aggregate embed text 6000.
- [Rate limits](https://docs.discord.com/developers/topics/rate-limits): dynamic route buckets include channel id; no guaranteed fixed edit quota is published. Five seconds is our policy, not a claimed Discord quota.
- 429 retries use `retry_after` seconds or `Retry-After`; no hardcoded retry delay.
- Endpoint methods also checked against Discord's official `discord-api-docs` MDX sources.

## Validation
Prefix each script below with:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`
| Script | Last lines (exit 0) |
| --- | --- |
| `tests/v2/discord_render_check.py` | Ran 21 tests in 5.368s; OK |
| `tests/discord_check.py` | all discord checks passed |
| `tests/v2/daemon_check.py` | Ran 15 tests in 1.548s; OK |
All Discord traffic used fake transports; no live credential bundle/network calls.
Daemon suite needed loopback sandbox escalation (initial socket PermissionError).

## Handoff / workarounds / proposed interfaces
- Instantiate `Reporter(stores, rest, bus)` once; it subscribes immediately. Call `close()` at shutdown; REST is caller-owned.
- WP7 only emits `task_created`. Runner must publish `task_status_changed` (alias `task_updated`) with `{kind,task_id,project_id,data:to_json(task)}` after writes; snapshots preserve intermediate transitions. No merged modules changed.
- Missing `data` falls back to the saved task; complete snapshots are preferred.
- `report_text` returns a `str` subclass with `.overflow`: full report body or None. This reconciles the requested string return and separate attachment; propose a named result if callers prefer explicit tuple unpacking.
- `rest.post(channel, report_text(report))` attaches overflow automatically. Reporter attaches it explicitly when combining the done template and report preview.
- Files are `(filename, str_or_bytes)` pairs; long `post` content becomes intact `message.txt`. Use `approval_text(...)` then `rest.post(...)`, never the capped approval milestone alone.
- `Report.cost` has no unit tag: values are rendered without inventing dollar/token units; propose typed cost units.
- Guild creation/routing and approval code generation, posting and resolution remain WP10b; raw provider approval/question events are deliberately not consumed.
- Brief takes precedence over design §11.1: create threads on `task_created`, not clarifying.
- Uses store `_lock` and `_write_bytes` for fresh task-id merges and atomic sidecars; propose public metadata update/sidecar APIs.
- Delivery is best effort: bus `subscription.dropped` exposes overflow; no durable outbox. A crash between HTTP success and sidecar save can duplicate a post.
- Shutdown discards pending edits and joins for at most two seconds; an in-flight REST call/retry can outlive that join. A later snapshot refreshes status.
