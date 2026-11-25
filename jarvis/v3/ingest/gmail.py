"""Read-only Gmail ingestion. Daemon job wiring is deferred.

MIME traversal has a depth limit but can scan thousands of inline siblings.
"""

from __future__ import annotations

import base64
import binascii
from email.errors import HeaderParseError
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from itertools import chain, islice
import math
import re
import time

from jarvis.v2 import redact

from . import Doc, RateLimited


_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_EXCLUDED = {"TRASH", "SPAM", "DRAFT", "CHAT", "CHATS"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"}
_ID = re.compile(r"[a-zA-Z0-9_-]{1,256}")
_CHARSET = re.compile(r"""\bcharset\s*=\s*["']?([\w.-]+)""", re.I)
_REPLY = re.compile(r"(?i)(?:on\s+.{1,720}\s+wrote|le\s+.{1,720}\s+a écrit)\s*:")
_REPLY_DETAIL = re.compile(r"\b\d{4}\b.*\b\d{1,2}:\d{2}\b|<[^<>\s]+@[^<>\s]+>")
_ORIGINAL = re.compile(r"(?i)-{2,}\s*(?:original message|forwarded message)\s*-{2,}")
_MAIL_FIELD = re.compile(r"(?i)(?:from|sent|date|to|subject):")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_SHARED_WORD_CHARS = (r"A-Za-z0-9_\-", r"A-Za-z0-9_", r"A-Za-z0-9/+", r"A-Za-z0-9/+=")
_MAIL_BOUNDARY = re.compile(r"(\(\?<?!)\[(?:"
                            + "|".join(map(re.escape, _SHARED_WORD_CHARS)) + r")\]\)")
_MAIL_SHAPES = tuple(
    (kind, re.compile(_MAIL_BOUNDARY.sub(r"\1[A-Za-z0-9])", pattern.pattern)))
    for kind, pattern in redact.SHAPES
    if _MAIL_BOUNDARY.search(pattern.pattern)
)
_KEY_RUN = re.compile(r"[A-Za-z0-9_-]+")
_RUN_PREFIX = {
    "API key": re.compile(r"(?<![A-Za-z0-9])sk-"),
    "JWT": re.compile(r"(?<![A-Za-z0-9])eyJ"),
}
_MAIL_PREFIX = re.compile(
    redact._CANDIDATE.pattern + r"|(?i:bearer\b|basic\b|(?:sk|oat|rt)[-_])"
    r"|[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\."
)
_MAIL_SEPARATOR = re.compile(r"[_/+=-]")
_FRAMED_FIELD = re.compile(
    r"(?:\b(?:basic|bearer)[ \t]+|(?:" + redact._AWS_LABELLED.pattern.removeprefix("(?i)")
    + r""")["']?[ \t]*[:=][ \t]*["']?)(?P<value>[A-Za-z0-9_.~+/=-]+)""",
    re.I,
)
_AWS_ID = next(pattern for kind, pattern in redact.SHAPES if kind == "AWS access key id")
_AWS_NEAR_FIELD = re.compile(_MAIL_BOUNDARY.sub(r"\1[A-Za-z0-9])", redact._AWS_NEAR.pattern))
_HEADER_CHARS = 1_000
_BODY_CHARS = 16_000
_BODY_BYTES = 256 * 1024
_MAX_ATTACHMENTS = 100
_HISTORY_PAGES = 10


class HistoryExpired(Exception):
    """The history checkpoint needs a new backfill."""


class GmailError(Exception):
    """A Gmail request failed without exposing the response or credentials."""


def _object(value):
    return value if isinstance(value, dict) else {}


def _array(value):
    return value if isinstance(value, list) else []


def _opaque(value):
    return isinstance(value, str) and 0 < len(value) <= 2048


def _message_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _decode(raw, charset):
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", errors="replace")


def _header(value):
    if not isinstance(value, str):
        return ""
    value = value[:4 * _HEADER_CHARS]
    try:
        parts = decode_header(value)
    except (HeaderParseError, ValueError):
        parts = [(value, None)]
    text = "".join(_decode(part, charset) if isinstance(part, bytes) else part
                   for part, charset in parts)
    return " ".join(text.split())[:_HEADER_CHARS]


def _headers(part):
    headers = {}
    for entry in _array(part.get("headers")):
        field = _object(entry).get("name")
        if isinstance(field, str):
            headers.setdefault(field.lower(), entry.get("value"))
    return headers


class _HTMLText(HTMLParser):
    _blocks = {"address", "br", "div", "hr", "li", "p", "section", "table", "tr",
               "h1", "h2", "h3", "h4", "h5", "h6"}
    _skip = {"head", "script", "style", "template", "title"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.text = []
        self._hidden = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        quoted = "gmail_quote" in (attributes.get("class") or "").split()
        if tag == "blockquote" and not self._hidden:
            attributed = self._follows_attribution()
            quoted = quoted or attributed or (attributes.get("type") or "").lower() == "cite"
            if attributed:
                # Give the text reply stripper a quote line to pair with the attribution.
                while self.text and not self.text[-1].strip():
                    self.text.pop()
                if self.text:
                    self.text[-1] = self.text[-1].rstrip()
                self.handle_data("\n>\n")
        if self._hidden or tag in self._skip or "hidden" in attributes or quoted:
            if quoted and not self._hidden:
                self.handle_data("\n")
            if tag not in self._void:
                self._hidden.append(tag)
        elif tag in self._blocks:
            self.handle_data("\n")

    def handle_endtag(self, tag):
        if self._hidden:
            if tag in self._hidden:
                while self._hidden.pop() != tag:
                    pass
        elif tag in self._blocks:
            self.handle_data("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.text.append(data)

    def _follows_attribution(self):
        # The reply pattern permits at most 720 characters between On and wrote.
        # Read a bounded tail even if the HTML has many blockquotes.
        tail = []
        remaining = 2048
        for piece in reversed(self.text):
            if not remaining:
                break
            tail.append(piece[-remaining:])
            remaining -= len(tail[-1])
        lines = "".join(reversed(tail)).rstrip().split("\n")
        return _reply_width(lines[:-1], lines[-1]) > 0


def _body_data(part, content_type):
    data = _object(part.get("body")).get("data")
    if not isinstance(data, str):
        return ""
    # Decode only a bounded number of complete base64 groups.
    data = data[:4 * (_BODY_BYTES // 3)]
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return ""
    charset = _CHARSET.search(content_type)
    return _decode(raw, charset[1] if charset else None)


def _reply_width(previous, line):
    for width in range(1, min(3, len(previous) + 1) + 1):
        prior = previous[len(previous) - width + 1:]
        attribution = " ".join(s.strip() for s in [*prior, line])
        if _REPLY.fullmatch(attribution) and _REPLY_DETAIL.search(attribution):
            return width
    return 0


def _strip_replies(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = []
    fence = ""
    quoting = False
    for i, line in enumerate(lines):
        if marker := _FENCE.fullmatch(line):
            run, rest = marker.groups()
            if not fence:
                fence = run
            elif run.startswith(fence) and not rest.strip():
                fence = ""
            kept.append(line)
            quoting = False
            continue
        if fence:
            kept.append(line)
            continue
        if quoting:
            if not line.strip() or line.lstrip().startswith(">"):
                continue
            if kept and kept[-1].strip() and not lines[i - 1].strip():
                kept.append("")
        quoting = False
        if i + 1 < len(lines):
            following = lines[i + 1].lstrip()
            if _ORIGINAL.fullmatch(line.strip()) and _MAIL_FIELD.match(following):
                break
            if following.startswith(">") and (width := _reply_width(kept, line)):
                del kept[len(kept) - width + 1:]
                quoting = True
        if not quoting:
            kept.append(line)
    return "\n".join(kept).strip()


def _parts(payload):
    """Walk MIME parts in order with a depth limit."""
    stack = [(iter((payload,)), 0)]
    while stack:
        siblings, depth = stack[-1]
        try:
            part = next(siblings)
        except StopIteration:
            stack.pop()
            continue
        if not isinstance(part, dict):
            continue
        headers = _headers(part)
        attachment = _is_attachment(part, headers)
        yield part, headers, attachment
        if depth < 32 and not attachment:
            stack.append((iter(_array(part.get("parts"))), depth + 1))


def _is_attachment(part, headers):
    return bool(part.get("filename") or _object(part.get("body")).get("attachmentId")
                or _header(headers.get("content-disposition")).lower().startswith("attachment"))


def _mail_body(payload):
    bodies = {"text/plain": [], "text/html": []}
    remaining = dict.fromkeys(bodies, _BODY_CHARS)
    files = []
    parts = _parts(payload)
    while len(files) < _MAX_ATTACHMENTS or any(remaining.values()):
        item = next(parts, None)
        if item is None:
            break
        part, headers, attachment = item
        if attachment:
            if len(files) < _MAX_ATTACHMENTS and (name := _header(part.get("filename"))):
                files.append(name)
            continue
        mime = part.get("mimeType")
        if not isinstance(mime, str):
            continue
        kind = mime.partition(";")[0].strip().lower()
        if not remaining.get(kind):
            continue
        content_type = headers.get("content-type")
        text = _body_data(part, content_type[:4096] if isinstance(content_type, str) else mime[:4096])
        if text and kind == "text/html":
            parser = _HTMLText()
            try:
                parser.feed(text)
                parser.close()
            except (AssertionError, ValueError):
                # A malformed declaration or character reference can stop the
                # parser. Keep only the text it had already emitted.
                pass
            text = "".join(parser.text)
        text = _strip_replies(text)
        if text:
            budget = remaining[kind]
            bodies[kind].append(text[:budget])
            remaining[kind] = max(0, budget - len(text) - 2)
    return "\n\n".join(bodies["text/plain"] or bodies["text/html"]), files


def _stamp(record):
    try:
        stamp = float(record.get("internalDate")) / 1000
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return stamp if math.isfinite(stamp) else 0.0


def _deleted(mid):
    return Doc(mid, "", 0, (), deleted=True)


def _shape_matches(kind, pattern, text):
    prefix = _RUN_PREFIX.get(kind)
    if prefix is None:
        yield from pattern.finditer(text)
        return
    # Later prefixes in the same run have the same suffix and less room to
    # meet the minimum length. Check each run once, even on long bodies.
    for run in _KEY_RUN.finditer(text):
        start = prefix.search(text, run.start(), run.end())
        end = run.end() if kind == "API key" else len(text)
        if start and (match := pattern.match(text, start.start(), end)):
            yield match


def _field_tail_spans(text):
    """Isolate a wrapped or framed credential's ending in a bounded field."""
    wrapped = ((offset, part, redact._SPLIT_BREAK.search(part).end())
               for offset, part in redact._split_candidates(text))
    framed = ((match.start(), match[0], match.start("value") - match.start())
              for match in _FRAMED_FIELD.finditer(text))
    for offset, candidate, value_start in chain(wrapped, framed):
        separators = list(_MAIL_SEPARATOR.finditer(candidate, value_start))
        if not separators or any(stop == len(candidate) for _, stop, _ in redact.spans(candidate)):
            continue
        for separator in reversed(separators):
            end = separator.start()
            found = [(offset + start, offset + stop, kind)
                     for start, stop, kind in redact.spans(candidate[:end]) if stop == end]
            if found:
                yield from found
                break

    # The shared redactor also accepts an unlabelled AWS secret near a key ID.
    # Preserve that context when isolating a candidate from adjacent punctuation.
    for key in _AWS_ID.finditer(text):
        lo = max(0, key.start() - redact._AWS_BEFORE)
        hi = min(len(text), key.end() + redact._AWS_AFTER)
        for candidate in _AWS_NEAR_FIELD.finditer(text, lo, hi):
            start, stop = candidate.span()
            if (start > 0 and _MAIL_SEPARATOR.fullmatch(text[start - 1])
                    or _MAIL_SEPARATOR.fullmatch(text[stop:stop + 1])):
                probe = key[0] + " " + candidate[0]
                if any(kind == "AWS secret key" for _, _, kind in redact.spans(probe)):
                    yield start, stop, "AWS secret key"


def _mail_boundary_spans(text, *, field):
    spans = []
    for kind, pattern in _MAIL_SHAPES:
        for match in _shape_matches(kind, pattern, text):
            start, stop = match.span()
            if ((start > 0 and _MAIL_SEPARATOR.fullmatch(text[start - 1]))
                    or _MAIL_SEPARATOR.fullmatch(text[stop:stop + 1])):
                # Present the candidate to the shared rules with both boundaries
                # removed, while retaining its offsets in the original field.
                spans.extend((start + a, start + b, label)
                             for a, b, label in redact.spans(match[0]))
    # Expose prefixes independently of the surrounding punctuation so the
    # shared rules can also recognize wrapped tokens and framed credentials.
    boundaries = {candidate.start() - 1 for candidate in _MAIL_PREFIX.finditer(text)
                  if candidate.start() and _MAIL_SEPARATOR.fullmatch(text[candidate.start() - 1])}
    probe = text
    if boundaries:
        probe = "".join(" " if i in boundaries else char for i, char in enumerate(text))
        spans.extend(redact.spans(probe))
    if field:
        spans.extend(_field_tail_spans(probe))
    return spans


def _mail_text(text, *, field=True):
    """Check punctuation-delimited credentials without changing surrounding mail text."""
    view = redact._View(text)
    spans = []
    if _MAIL_SEPARATOR.search(view.view):
        spans.extend((*view.span(a, b), kind)
                     for a, b, kind in _mail_boundary_spans(view.view, field=field))
    if field and view.views is not None:
        # A mark the shared normalizer drops may itself separate a field's
        # prefix or suffix from a token. Try it as a space as well.
        separated = redact._SPECIAL.sub(
            lambda match: " " if redact._fold(match[0]) == "" else match[0], text,
        )
        if separated != text:
            alternate = redact._View(separated)
            spans.extend(redact.spans(separated))
            if _MAIL_SEPARATOR.search(alternate.view):
                spans.extend((*alternate.span(a, b), kind) for a, b, kind in
                             _mail_boundary_spans(alternate.view, field=True))
    if not spans:
        return text
    # Merge before editing so a newly recognized inner span cannot mask a
    # credential the shared redactor already sees in the original text.
    spans.extend(redact.spans(text))
    return redact._apply(text, redact._merge(spans), redact.withheld)


def _mailbox(address):
    local, _, domain = address.casefold().rpartition("@")
    local = local.partition("+")[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if local and domain else ""


def _document(mid, record, owner, labels, ts):
    payload = _object(record.get("payload"))
    headers = _headers(payload)
    raw_sender = headers.get("from")
    mailbox = parseaddr(raw_sender[:4 * _HEADER_CHARS])[1] if isinstance(raw_sender, str) else ""
    from_owner = int("SENT" in labels or bool(owner and _mailbox(mailbox) == owner))
    sender, to, date, title = (_mail_text(_header(headers.get(key)))
                               for key in ("from", "to", "date", "subject"))
    body, files = _mail_body(payload)
    text = [f"From: {sender}\nTo: {to}\nDate: {date}"]
    if body:
        text.append(_mail_text(body, field=False))
    if files:
        text.append("Attachments: " + ", ".join(map(_mail_text, files)))
    meta = {"untrusted": 1, "from_owner": from_owner, "thread": _mail_text(_header(record.get("threadId"))),
            "from": sender, "to": to, "labels": list(map(_mail_text, labels))}
    return Doc(mid, title, ts, [("owner" if from_owner else "sender", "\n\n".join(text), ts)], meta)


class GmailIngester:
    name = "gmail"

    def __init__(self, api, *, backfill_days=180, clock=time.time, page_docs=50):
        self._api = api
        self._backfill_days = backfill_days
        self._clock = clock
        self._page_docs = max(1, int(page_docs))

    def poll(self, cursor):
        return self.poll_indexed(cursor, None)

    def poll_indexed(self, cursor, index):
        """Use the index to verify stored messages after a history gap."""
        recovering = False
        if isinstance(cursor, dict) and _opaque(cursor.get("history")):
            if "verify" in cursor:
                # An older cursor's position only covered rows inside the
                # backfill window. Restart that sweep from the beginning.
                after = None if type(cursor.get("cutoff")) is int else cursor["verify"]
                if index is not None and (after is None or (
                    isinstance(after, list) and len(after) == 2 and type(after[0]) is float
                    and math.isfinite(after[0]) and isinstance(after[1], str)
                )):
                    return self._verify(cursor["history"], index, after)
            elif "cutoff" not in cursor:
                try:
                    if self._history_cursor(cursor):
                        return self._history(cursor)
                except HistoryExpired:
                    recovering = index is not None
            elif self._backfill_cursor(cursor):
                return self._backfill(cursor)
        profile = self._profile()
        checkpoint = self._checkpoint(profile)
        cutoff = int(self._clock() - self._backfill_days * 86400)
        cursor = {"history": checkpoint, "cutoff": cutoff, "page": None, "pending": []}
        if recovering:
            cursor["recovering"] = True
        return self._backfill(cursor, profile)

    @staticmethod
    def _backfill_cursor(cursor):
        page, pending = cursor.get("page"), cursor.get("pending")
        return (type(cursor["cutoff"]) is int and (page is None or _opaque(page))
                and isinstance(pending, list) and all(_message_id(mid) for mid in pending))

    def _profile(self):
        return _object(self._api.profile())

    @staticmethod
    def _checkpoint(response):
        checkpoint = response.get("historyId")
        if not _opaque(checkpoint):
            raise GmailError("Missing Gmail history checkpoint.")
        return checkpoint

    @staticmethod
    def _next_page(response):
        token = response.get("nextPageToken")
        if token is not None and not _opaque(token):
            raise GmailError("Invalid Gmail page token.")
        return token

    def _backfill(self, cursor, profile=None):
        cutoff, page, pending = cursor["cutoff"], cursor["page"], cursor["pending"]
        if not pending:
            response = self._api.list(f"after:{cutoff}", page)
            pending = [entry["id"] for entry in _array(response.get("messages"))
                       if _message_id(_object(entry).get("id"))]
            next_page = self._next_page(response)
            if next_page is not None and next_page == page:
                raise GmailError("Repeated Gmail page token.")
            page = next_page
        batch, remaining = pending[:self._page_docs], pending[self._page_docs:]
        changes = self._documents(dict.fromkeys(batch, False), cutoff=cutoff, profile=profile)
        new_cursor = {"history": cursor["history"]}
        if page is not None or remaining:
            new_cursor.update(cutoff=cutoff, page=page, pending=remaining)
            if cursor.get("recovering") is True:
                new_cursor["recovering"] = True
        elif cursor.get("recovering") is True:
            new_cursor["verify"] = None
        return changes, new_cursor

    def _verify(self, checkpoint, index, after):
        rows = index.ingest_refs(self.name, after=after, limit=self._page_docs + 1)
        changes = []
        for ref, _, _ in rows[:self._page_docs]:
            if not _message_id(ref):
                continue
            record = self._api.get(ref)
            if record is None or _EXCLUDED.intersection(self._labels(record)):
                changes.append(_deleted(ref))
        cursor = {"history": checkpoint}
        if len(rows) > self._page_docs:
            _, stamp, doc_id = rows[self._page_docs - 1]
            cursor["verify"] = [stamp, doc_id]
        return changes, cursor

    @staticmethod
    def _history_cursor(cursor):
        page, pending = cursor.get("page"), cursor.get("events", {})
        return ((page is None or _opaque(page)) and isinstance(pending, dict)
                and all(_message_id(mid) and type(deleted) is bool for mid, deleted in pending.items())
                and (page is not None or not pending or cursor.get("complete") is True))

    def _history(self, cursor):
        checkpoint, page = cursor["history"], cursor.get("page")
        events = dict(cursor.get("events", {}))
        complete = cursor.get("complete") is True
        seen = set()
        for _ in range(_HISTORY_PAGES):
            if complete or len(events) >= self._page_docs:
                break
            seen.add(page)
            response = self._api.history(checkpoint, page)
            for record in _array(response.get("history")):
                row = _object(record)
                for event in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
                    for change in _array(row.get(event)):
                        mid = _object(_object(change).get("message")).get("id")
                        if _message_id(mid):
                            events[mid] = event == "messagesDeleted"
            page = self._next_page(response)
            if page is None:
                checkpoint = self._checkpoint(response)
                complete = True
            elif page in seen:
                raise GmailError("Repeated Gmail page token.")
        batch = {mid: events.pop(mid) for mid in list(islice(events, self._page_docs))}
        next_cursor = {"history": checkpoint}
        if events or not complete:
            next_cursor.update(page=page, events=events, complete=complete)
        return self._documents(batch), next_cursor

    @staticmethod
    def _labels(record):
        return [label for label in _array(record.get("labelIds")) if isinstance(label, str)]

    def _documents(self, changes, *, cutoff=None, profile=None):
        docs = []
        for mid, deleted in changes.items():
            record = None if deleted else self._api.get(mid)
            if record is None:
                docs.append(_deleted(mid))
                continue
            ts = _stamp(record)
            if cutoff is not None and ts < cutoff:
                continue
            labels = self._labels(record)
            if _EXCLUDED.intersection(labels):
                docs.append(_deleted(mid))
                continue
            if profile is None:
                profile = self._profile()
            owner = _mailbox(_header(profile.get("emailAddress")))
            docs.append(_document(mid, record, owner, [label[:_HEADER_CHARS] for label in labels], ts))
        return docs


def _retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None


class HttpGmail:
    """GET-only Gmail API using an injected access-token callable."""

    def __init__(self, token, *, transport=None, timeout=30):
        import httpx

        self._token = token
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def close(self):
        self._client.close()

    def profile(self):
        return self._get("profile")

    def list(self, query, page_token=None):
        params = {"q": query}
        if page_token is not None:
            params["pageToken"] = page_token
        return self._get("messages", params)

    def get(self, mid):
        if not _message_id(mid):
            raise GmailError("Invalid Gmail message identifier.")
        return self._get(f"messages/{mid}", {"format": "full"}, missing_ok=True)

    def history(self, start_history_id, page_token=None):
        params = {"startHistoryId": start_history_id}
        if page_token is not None:
            params["pageToken"] = page_token
        return self._get("history", params)

    def _get(self, route, params=None, *, missing_ok=False):
        response = None
        try:
            response = self._client.get(f"{_API}/{route}", params=params,
                                        headers={"Authorization": f"Bearer {self._token()}"})
        except Exception:
            pass
        if response is None:
            raise GmailError("Gmail request failed.")
        status = response.status_code
        if status == 404:
            if route == "history":
                raise HistoryExpired()
            if missing_ok:
                return None
        if status == 429:
            raise RateLimited(_retry_after(response.headers.get("Retry-After")))

        body = None
        if status in (200, 403):
            try:
                body = response.json()
            except (ValueError, RecursionError):
                pass
        if status == 200 and isinstance(body, dict):
            return body
        if status == 403:
            error = _object(_object(body).get("error"))
            details = _array(error.get("errors")) + _array(error.get("details"))
            if any(_object(item).get("reason") in _RATE_REASONS for item in details
                   if isinstance(_object(item).get("reason"), str)):
                raise RateLimited(_retry_after(response.headers.get("Retry-After")))
        raise GmailError(f"Gmail returned HTTP {status}.")
