"""Incremental sentence boundaries over the speakable text of a reply."""

import re

MIN_SENTENCE = 12
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_URL = re.compile(r"<?\b(?:https?://|www\.)[^\s<>()]*>?", re.IGNORECASE)
_OPEN_MARKUP = re.compile(r"!?(?:\[|<)|`|(?<!\w)(?:\*{1,2}|_{1,2}|~~)(?=\S|$)")
_ABBREVIATION = re.compile(r"\b(?:e\.g\.|i\.e\.|mr\.|dr\.|vs\.)", re.IGNORECASE)
_PARTIAL_ABBREVIATION = re.compile(r"\b(?:e\.g?|i\.e?)$", re.IGNORECASE)
_END = re.compile(r"""[.!?]+["'”’)]*|\n""")


def _clean(line, *, complete):
    # Keep the daemon's startup path independent from the voice/model stack.
    from jarvis.voice import speakable

    text = speakable(line)

    def remove_url(match):
        url = match[0]
        if not complete and match.end() == len(text):
            return ""
        return url[len(url.rstrip(".!?")):]

    text = _URL.sub(remove_url, text)
    if not complete:
        # A link label or emphasis may only become speakable once its
        # closing markup arrives. Keep that portion of the line in the buffer.
        opening = _OPEN_MARKUP.search(text)
        if opening:
            text = text[:opening.start()]
    return text


def _sentences(text, *, complete):
    abbreviations = {i for match in _ABBREVIATION.finditer(text)
                     for i in range(match.start(), match.end())}
    partial = _PARTIAL_ABBREVIATION.search(text) if not complete else None
    if partial:
        abbreviations.update(range(partial.start(), partial.end()))
    start = 0
    for end in _END.finditer(text):
        pos = end.start()
        if pos in abbreviations:
            continue
        if end[0] == "." and pos > 0 and text[pos - 1].isdigit():
            if (pos + 1 < len(text) and text[pos + 1].isdigit()) or (end.end() == len(text) and not complete):
                continue
        sentence = " ".join(text[start:end.end()].split())
        if len(sentence) >= MIN_SENTENCE and any(char.isalnum() for char in sentence):
            yield sentence, end.end()
            start = end.end()


class Sentences:
    def __init__(self):
        self._line = ""
        self._fenced = False
        self._line_read = 0
        self._pending = ""
        self.has_fragment = False

    def feed(self, chunk, *, final=False):
        lines = chunk.split("\n")
        for part in lines[:-1]:
            yield from self._feed_line(part, complete=True)
        yield from self._feed_line(lines[-1], complete=final, final=final)

    def _feed_line(self, part, *, complete, final=False):
        self._line += part
        fence = bool(_FENCE.match(self._line))
        if not (self._fenced or fence):
            line = _clean(self._line, complete=complete)[self._line_read:]
            tail = line + ("\n" if complete else "")
            text = self._pending + tail
            offset = 0
            for sentence, offset in _sentences(text, complete=complete):
                yield sentence
            pending_read = min(offset, len(self._pending))
            self._line_read += offset - pending_read
            self._pending = text[offset:] if complete else self._pending[pending_read:]
            self.has_fragment = any(char.isalnum() for char in text[offset:])
        if complete:
            if fence:
                self._fenced = not self._fenced
            self._line = ""
            self._line_read = 0
        if final:
            remainder = " ".join(self._pending.split())
            self._pending = ""
            self.has_fragment = False
            if any(char.isalnum() for char in remainder):
                yield remainder
