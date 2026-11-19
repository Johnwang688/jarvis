"""Locked English prompt shared by the Laya benchmark and resident worker."""

REPO = "convaiinnovations/laya"
ENGLISH_REVISION = "e333cb7c823cd1787276228260f56d02fbf5180b"

QUESTION = {
    "route": {
        "type": "choice",
        "instructions": (
            "Classify the speaker's primary intent in this voice transcript. "
            "Ignore filler and courtesy. Base the choice on what they want done."
        ),
        "criteria": {
            "ask": "Give an answer, explanation or comparison in conversation, or help with a task in chat.",
            "navigate": "Visit an explicitly named website in a browser.",
            "open_app": "Launch a named desktop application.",
            "switch_app": "Return to or focus a named open app or window.",
            "search": "Perform a web search for the speaker's query.",
            "read_selection": "Read aloud text already selected on the desktop.",
            "clarify": "The requested outcome is unclear or contradictory.",
        },
    },
}

# Maximum size of one JSON line on the private pipes.
MAX_FRAME = 64 * 1024
