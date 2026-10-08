"""Small synchronous REST adapter using v1 credentials and request conventions.

Two credentials pass through here and neither may leave it:

* the **bot token**, only ever in the `Authorization` header;
* an **interaction token** (S1), a 15-minute credential that can post as the
  application, only ever in a URL path — `/interactions/{id}/{token}/callback`
  and `/webhooks/{app}/{token}/…`.

So nothing here logs, and no error string carries a URL: a transport failure
is reported by exception class only, and an HTTP failure by status and
Discord's own message, with every secret the call used redacted from it.
Callers log the operation name, never the path.
"""
from __future__ import annotations

import json
import math
import re
import time

import httpx

from jarvis import config
from jarvis.tools import discord as v1

DiscordError = v1.DiscordError

EPHEMERAL = 64
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,500}$")
_SNOWFLAKE = re.compile(r"^[0-9]{1,24}$")


class DiscordHTTPError(DiscordError):
    """A non-2xx answer. `status` lets a caller tell a dead interaction token
    (401/404) from anything else; the message never holds a path or a token."""

    def __init__(self, message: str, status: int, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def _snowflake(value) -> str:
    value = str(value)
    if not _SNOWFLAKE.match(value):
        raise DiscordError("Error: Discord id is not a snowflake")
    return value


def _token(value) -> str:
    value = str(value)
    if not _TOKEN.match(value):
        raise DiscordError("Error: interaction token has an unexpected shape")
    return value


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

    def _api(self, method, path, *, secrets=(), **kwargs):
        token = v1._load_bundle()["bot_token"]
        hidden = [s for s in (token, *secrets) if s]

        def redact(text: str) -> str:
            for secret in hidden:
                text = text.replace(secret, "[REDACTED]")
            return text

        while True:
            try:
                response = self._request(
                    method, f"{config.DISCORD_API}{path}",
                    headers={"Authorization": f"Bot {token}"}, timeout=30, **kwargs)
            except Exception as exc:
                # Transport exception messages can contain the URL, headers or
                # request data: the class is all that leaves.
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
                    raise DiscordHTTPError(redact(v1._fail(response)), 429) from None
                time.sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                code = None
                try:
                    code = response.json().get("code")
                except (ValueError, AttributeError):
                    pass
                raise DiscordHTTPError(redact(v1._fail(response)), response.status_code, code)
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
    def _message(content, embed, components=None, flags=None):
        body = {"allowed_mentions": {"parse": []}}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        if components is not None:
            body["components"] = components
        if flags:
            body["flags"] = flags
        return body

    def _payload(self, content=None, embed=None, files=(), components=None, flags=None):
        """-> request kwargs: JSON, or multipart when anything is attached."""
        files = list(files)
        overflow = getattr(content, "overflow", None)
        if overflow is not None:
            files.append(("report.txt", overflow))
        if content is not None and len(content) > 2000:
            files.append(("message.txt", content))
            content = "Full message attached (unabridged)."
        body = self._message(content, embed, components, flags)
        if not files:
            return {"json": body}
        body["attachments"] = [{"id": i, "filename": name} for i, (name, _) in enumerate(files)]
        return {"data": {"payload_json": json.dumps(body, ensure_ascii=False)},
                "files": [(f"files[{i}]", (name, data.encode("utf-8") if isinstance(data, str)
                                          else data, "text/plain; charset=utf-8"))
                          for i, (name, data) in enumerate(files)]}

    def post(self, channel_id, content=None, embed=None, files=(), components=None) -> str:
        return self._id("POST", f"/channels/{channel_id}/messages",
                        **self._payload(content, embed, files, components))

    def edit(self, channel_id, message_id, content=None, embed=None, components=None):
        self._api("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                  json=self._message(content, embed, components))

    def unarchive(self, thread_id):
        self._api("PATCH", f"/channels/{thread_id}", json={"archived": False})

    # -- interactions (S1) -------------------------------------------------
    # The interaction token is in every path below, so every call passes it as
    # a secret to redact, and none of these paths is ever logged.

    def callback(self, interaction_id, token, kind, data=None):
        """The initial response: 4 message, 5 deferred, 6 deferred update,
        7 update message, 8 autocomplete, 9 modal."""
        token = _token(token)
        body = {"type": int(kind)}
        if data is not None:
            body["data"] = data
        self._api("POST", f"/interactions/{_snowflake(interaction_id)}/{token}/callback",
                  secrets=(token,), json=body)

    def edit_original(self, app_id, token, *, content=None, embed=None, files=(),
                      components=None):
        token = _token(token)
        self._api("PATCH", f"/webhooks/{_snowflake(app_id)}/{token}/messages/@original",
                  secrets=(token,), **self._payload(content, embed, files, components))

    def followup(self, app_id, token, *, content=None, embed=None, files=(),
                 components=None, ephemeral=False) -> str:
        token = _token(token)
        return self._id("POST", f"/webhooks/{_snowflake(app_id)}/{token}",
                        secrets=(token,), **self._payload(content, embed, files, components,
                                                          EPHEMERAL if ephemeral else None))

    # -- command registration (S1) -----------------------------------------
    # The path is spelled only in discord/commands.py; this module just sends.

    def get_json(self, path):
        try:
            return self._api("GET", path).json()
        except ValueError:
            raise DiscordError("Error: Discord response is not JSON") from None

    def get_commands(self, app_id) -> list:
        from .commands import commands_path

        response = self._api("GET", commands_path(app_id))
        try:
            body = response.json()
        except ValueError:
            raise DiscordError("Error: Discord command list is not JSON") from None
        if not isinstance(body, list):
            raise DiscordError("Error: Discord command list is not a list")
        return body

    def put_commands(self, app_id, payload) -> list:
        from .commands import commands_path

        response = self._api("PUT", commands_path(app_id), json=list(payload))
        try:
            return list(response.json())
        except (ValueError, TypeError):
            return []
