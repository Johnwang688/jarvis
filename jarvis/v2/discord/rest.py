"""Small synchronous REST adapter using v1 credentials and request conventions."""
from __future__ import annotations

import json
import math
import time

import httpx

from jarvis import config
from jarvis.tools import discord as v1

DiscordError = v1.DiscordError


class DiscordRest:
    """transport: httpx BaseTransport, or callable with httpx.request's signature.

    files are (filename, str-or-bytes body) pairs, retained in memory for retries.
    Long content is attached intact, including approval commands.
    """

    def __init__(self, transport=None):
        self._client = (httpx.Client(transport=transport)
                        if isinstance(transport, httpx.BaseTransport) else None)
        self._request = self._client.request if self._client else (transport or httpx.request)

    def close(self):
        if self._client:
            self._client.close()

    def _api(self, method, path, **kwargs):
        token = v1._load_bundle()["bot_token"]
        while True:
            try:
                response = self._request(
                    method, f"{config.DISCORD_API}{path}",
                    headers={"Authorization": f"Bot {token}"}, timeout=30, **kwargs)
            except Exception as exc:
                # Transport exception messages can contain headers or request data.
                raise DiscordError(f"Error: Discord transport failed ({type(exc).__name__})") from None
            if response.status_code == 429:
                try:
                    delay = response.json().get("retry_after")
                    if delay is None:
                        delay = response.headers["Retry-After"]
                    delay = float(delay)
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError("invalid retry delay")
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise DiscordError(v1._fail(response).replace(token, "[REDACTED]")) from None
                time.sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                raise DiscordError(v1._fail(response).replace(token, "[REDACTED]"))
            return response

    def _id(self, method, path, **kwargs):
        response = self._api(method, path, **kwargs)
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError):
            raise DiscordError("Error: Discord API response has no id") from None

    def create_channel(self, guild_id, name) -> str:
        return self._id("POST", f"/guilds/{guild_id}/channels", json={"name": name, "type": 0})

    def create_thread(self, channel_id, name) -> str:
        return self._id("POST", f"/channels/{channel_id}/threads", json={"name": name, "type": 11})

    @staticmethod
    def _message(content, embed):
        body = {"allowed_mentions": {"parse": []}}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        return body

    def post(self, channel_id, content=None, embed=None, files=()) -> str:
        files = list(files)
        overflow = getattr(content, "overflow", None)
        if overflow is not None:
            files.append(("report.txt", overflow))
        if content is not None and len(content) > 2000:
            files.append(("message.txt", content))
            content = "Full message attached (unabridged)."
        body = self._message(content, embed)
        kwargs = {"json": body}
        if files:
            body["attachments"] = [{"id": i, "filename": name} for i, (name, _) in enumerate(files)]
            kwargs = {"data": {"payload_json": json.dumps(body, ensure_ascii=False)},
                      "files": [(f"files[{i}]", (name, data.encode("utf-8") if isinstance(data, str)
                                                else data, "text/plain; charset=utf-8"))
                                for i, (name, data) in enumerate(files)]}
        return self._id("POST", f"/channels/{channel_id}/messages", **kwargs)

    def edit(self, channel_id, message_id, content=None, embed=None):
        self._api("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                  json=self._message(content, embed))

    def unarchive(self, thread_id):
        self._api("PATCH", f"/channels/{thread_id}", json={"archived": False})
