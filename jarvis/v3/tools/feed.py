"""Session-bound agent posting for the brief feed.

Offering brief_post in the jarvis-brain profile is out of scope until B2.
"""

from functools import wraps
from typing import Annotated

from jarvis.v3.feed import Feed, FeedError


def _tool(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except FeedError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: Brief posting could not complete."
    return call


class FeedTools:
    def __init__(self, feed: Feed, *, session: str):
        self._feed = feed
        self._session = session

    @_tool
    def brief_post(
        self,
        kind: Annotated[str, "Brief kind: decision, news or digest."],
        ask: Annotated[str, "One line for the owner, at most 120 characters."],
        rank: Annotated[int, "Priority from 3 to 8, with 3 the highest."] = 3,
        why_now: Annotated[str, "Why this matters now, at most 200 characters."] = "",
        options: Annotated[list[str] | None, "Up to three short option labels."] = None,
        default_action: Annotated[str | None, "Required for a decision. Use none for a no-op."] = None,
        default_at: Annotated[str | None, "Default deadline as an ISO datetime with a zone."] = None,
        due: Annotated[str | None, "Due time as an ISO datetime with a zone."] = None,
    ) -> str:
        """Post a brief to the owner's batch feed. All content is display text."""
        fields = {"kind": kind, "ask": ask, "rank": rank, "why_now": why_now, "due": due}
        if options is not None:
            if not isinstance(options, list) or len(options) > 3:
                raise FeedError("Options must be a list of at most three labels.")
            fields["options"] = [{"label": label} for label in options]
        if default_action is not None or default_at is not None:
            fields["default"] = {"action": default_action, "at": default_at}
        self._feed.post(fields, actor="agent", session=self._session)
        return "Brief posted."
