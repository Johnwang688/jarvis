"""Small local voice intent model. Fits normalized TF-IDF class centroids from
authored phrases once per process (Rocchio). No downloads, history or owner data.
A predicted action additionally needs a complete supported argument frame.
"""
from collections import Counter, defaultdict
from functools import lru_cache
from math import log, sqrt
import re

from windows.voicetype import (JARVIS_ADDRESS, NARRATIVE_VERB, OPEN_APP_FRAME, POLITE_PREFIX,
                               app_name, sanitize, shortcut_open_target)

# Examples intentionally use varied slots. The token features include leading
# words and adjacent pairs so "open Calendar" differs from "when I open Calendar".
_TRAIN = {
    "open_app": (
        "open Calculator", "launch Paint", "start Spotify", "bring up Word",
        "could you open Teams", "please open the Outlook app", "start up Firefox",
        "pull up Notepad", "can you launch Excel", "fire up Discord",
        "would you open the Photos application", "open up OneNote",
    ),
    "switch_app": (
        "switch to Calendar", "switch over to Slack", "focus Word",
        "go back to Teams", "bring Excel to the front", "give Outlook focus",
        "return to Spotify", "please focus the Firefox window", "go to Paint",
        "can you bring Calculator to the foreground", "take me back to OneNote",
    ),
    "search": (
        "search for mountain trails", "google how to repair a bicycle",
        "look up the train schedule", "search the web for music lessons",
        "find online results for film cameras", "look online for meteor showers",
        "could you google ceramic mugs", "do a web search for viola tuning",
        "browse the web for postage rates", "please search for local galleries",
    ),
    "read_selection": (
        "read the selection", "read this selected text", "read the highlight aloud",
        "please read my highlighted text", "can you read back what I selected",
        "say the selected passage out loud", "read aloud the current selection",
        "read what I have highlighted", "speak the selected words", "read my selection",
    ),
    "task": (
        "monitor the build and tell me when it completes", "watch my PR checks and report back",
        "write a report on transit funding", "put together a spreadsheet for my budget",
        "create a presentation and save it", "draft a project proposal for me",
        "investigate the failing tests and fix them", "build a website for the club",
        "implement the requested feature", "make a chart of revenue by quarter",
        "keep an eye on the deployment", "review my pull request and leave a summary",
        "prepare a document comparing these options", "track the job until it finishes",
    ),
    "ask": (
        "what is a binary search", "is this app installed", "what does read selection mean",
        "can I switch accounts", "tell me why stars shine", "explain why that happened",
        "how do I make a chart", "how would you build a website", "who wrote the report",
        "could you explain how to monitor a process", "do you know where the build went",
        "why did the test fail", "what time is it", "summarize that for me",
        "what should I do next", "help me understand this code",
    ),
    "dictate": (
        "open source projects need maintainers", "the search results were outdated",
        "a switch to daylight time is coming", "read receipts are enabled",
        "I said open the calendar", "we should launch next month", "I need to buy eggs",
        "write this down on the form tomorrow", "the window is already open",
        "report writing takes practice", "monitor color varies", "start small and learn",
        "this is my note", "I wonder what time it is", "finish the sentence here",
        "launch dates change often", "focus group feedback was positive",
        "Google bought another company", "Google maps is slow", "open registrations start soon",
        "type one is a common condition", "search for evidence takes patience",
    ),
}

_FLAGS = re.IGNORECASE
_LITERAL = re.compile(
    r"^(?:type(?: out)?|dictate|insert)(?:\s+(?:the )?(?:words?|phrase|sentence|text)"
    r"(?=[:,\s]))?(?:\s+(?:literally|verbatim))?(?:\s*[:,]\s*|\s+)(.+)$", _FLAGS)
_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
_REPORT = re.compile(r"\b(?:said|says|say the words|phrase|literally|verbatim|"
                     r"the words|quotation|quote|quoted|reads|wrote|write the words|heard|told)\b", _FLAGS)
# "send it" can terminate a dictated message. If it is the object of a
# statement or question, inserting Enter would submit text the owner is composing.
_SEND_IN_SENTENCE = re.compile(
    r"\b(?:I|you|we|she|he|they|to|not|never|cannot|can|could|would|will|"
    r"should|may|might|must|shall|did|do|does|usually|already|always|"
    r"\w+['’](?:t|ll|d))\s+send it[.!?]*$", _FLAGS)
_WAKE_REQUEST = re.compile(r"^(?:let['’]s|I(?:['’]d| would) like to)\s+", _FLAGS)
_EDIT_WORDS = re.compile(r"\b(?:scratch that|new line|send it)\b", _FLAGS)
_SWITCH = re.compile(r"(?:switch(?: over| back| me back| me)? to|go back to|return to|take me back to|focus|go to) (.+)|"
                     r"bring (.+) to the (?:front|foreground)|give (.+) focus", _FLAGS)
_SEARCH = re.compile(r"(?:search (?:the (?:web|internet) )?for|search the web|google|look up|"
                     r"(?:look|search) online for|find online results for|do a web search for|"
                     r"browse the web for) (.+)", _FLAGS)
_READ = re.compile(r"(?:read(?: back| aloud)?|say|speak) (?:(?:(?:the|this|my|current) )*"
                   r"(?:(?:selected|highlighted)(?: text| words| paragraph| passage)?|selection|highlight)"
                   r"|what I(?: have|'ve)? (?:selected|highlighted))(?: out loud| aloud| to me)?", _FLAGS)
_QUERY_WORDS = re.compile(r"^(?:how|when|where|why|what|which|who|whether|does)\b", _FLAGS)
_TYPE_NOUN = re.compile(r"^type (?:[0-9]+|one|two|three|i|ii|iii|safety|checking|"
                        r"system|inference|error|theory|of)\b", _FLAGS)
_QUESTION = re.compile(r"^(?:how|what|who|why|where|which|whose|when|do I|can I|"
                       r"could I|is (?:this|there|it)|does|have you)\b", _FLAGS)
_DELEGATE = re.compile(r"^(?:watch|monitor|track|keep an eye on|write|build|implement|investigate|fix|"
                       r"prepare|draft|compose|design|make|create|generate|put together|review|"
                       r"research|look into|work on|check on|follow|compile|produce)\b", _FLAGS)


def _features(text):
    words = re.findall(r"[\w']+", text.casefold())[:64]
    features = Counter(words)
    features.update(a + "/" + b for a, b in zip(words, words[1:]))
    if words:
        features["^" + words[0]] = 2
        features["^" + "/".join(words[:2])] = 2
    return features


@lru_cache(maxsize=1)
def _centroids():
    docs = [(label, _features(phrase)) for label, phrases in _TRAIN.items() for phrase in phrases]
    counts = Counter(feature for _, doc in docs for feature in doc)
    idf = {feature: 1 + log((len(docs) + 1) / (df + 1)) for feature, df in counts.items()}
    centers = defaultdict(Counter)
    for label, doc in docs:
        vector = {f: n * idf[f] for f, n in doc.items()}
        norm = sqrt(sum(w * w for w in vector.values()))
        centers[label].update({f: w / norm for f, w in vector.items()})
    for vector in centers.values():
        norm = sqrt(sum(w * w for w in vector.values()))
        for f in vector:
            vector[f] /= norm
    return idf, centers


def _scores(text):
    idf, centers = _centroids()
    vector = {f: n * idf[f] for f, n in _features(text).items() if f in idf}
    norm = sqrt(sum(w * w for w in vector.values())) or 1
    return {label: sum(vector[f] * center[f] for f in vector) / norm
            for label, center in centers.items()}


def _slot(kind, body):
    if kind == "read_selection":
        return "" if _READ.fullmatch(body) else None
    match = {"open_app": OPEN_APP_FRAME, "switch_app": _SWITCH, "search": _SEARCH}[kind].fullmatch(body)
    if not match:
        return None
    target = next(group for group in match.groups() if group is not None).strip()
    if kind == "search":
        if not (0 < len(target) <= 80):
            return None
        if re.match(r"^look up (?:from|at|to)\b", body, _FLAGS):
            return None
        if NARRATIVE_VERB.search(target) and not _QUERY_WORDS.match(target):
            return None
        return target
    target = app_name(target)
    if target.casefold() == "sender" and kind == "switch_app":
        return None
    return target or None


def _unquote(payload):
    for candidate in (payload, payload.rstrip(".!?")):
        if len(candidate) > 1 and _QUOTES.get(candidate[0]) == candidate[-1]:
            return candidate[1:-1].strip()
    return payload


def addressed(text):
    return bool(JARVIS_ADDRESS.match(sanitize(text)))


def route(text, source):
    """An unaddressed hotkey dictates or opens a named app. Addressed speech can ask or act."""
    words = sanitize(text)[:2000]
    def result(kind, target="", content=words):
        return {"kind": kind, "text": content, "target": target}

    addressed = bool(JARVIS_ADDRESS.match(words))
    body = JARVIS_ADDRESS.sub("", words, count=1)
    if source == "wake" or addressed:
        body = _WAKE_REQUEST.sub("", body, count=1)
    polite = bool(POLITE_PREFIX.match(body))
    body = POLITE_PREFIX.sub("", body, count=1).strip()
    fallback = "ask" if source == "wake" or addressed else "dictate"
    literal = _LITERAL.fullmatch(body)
    if literal and not _TYPE_NOUN.match(body):
        return result("dictate", "literal", _unquote(literal[1].strip()))
    if source == "hotkey":
        if _SEND_IN_SENTENCE.search(body) or (body.endswith("?") and _EDIT_WORDS.search(body)):
            return result("dictate", "literal")
        if (_unquote(body) != body or (_EDIT_WORDS.search(body) and
                (_REPORT.search(body) or re.search(r"""["“”‘’]|(?:^|\s)'""", body)))):
            return result("dictate", "literal")

        if not addressed:
            target = shortcut_open_target(words)
            if target:
                scores = _scores(body)
                if scores["open_app"] >= max(0.10, scores["dictate"]):
                    return result("open_app", target)
            return result("dictate")

    # Embedded commands, quoted requests and questions about an action never
    # satisfy an executable imperative frame. Preserve the original transcript.
    if not body or (body.endswith("?") and not polite) or _QUESTION.match(body):
        return result(fallback)
    body = re.sub(r"[,\s]+please[.!?]*$", "", body, flags=_FLAGS).strip().rstrip(".!?").strip()
    if OPEN_APP_FRAME.fullmatch(body) or _SWITCH.fullmatch(body):
        body = re.sub(r" for me$", "", body, flags=_FLAGS)
    scores = _scores(body)
    for kind in ("open_app", "switch_app", "search", "read_selection"):
        target = _slot(kind, body)
        if target is None:
            continue
        query = kind == "search" and _QUERY_WORDS.match(target)
        if scores[kind] >= 0.10 and (query or scores[kind] >= scores["dictate"]):
            return result(kind, target)
    if (fallback == "ask" or polite) and _DELEGATE.match(body) and scores["task"] >= 0.12:
        return result("ask", "task")
    if polite and scores["ask"] >= 0.12:
        return result("ask")
    return result(fallback)
