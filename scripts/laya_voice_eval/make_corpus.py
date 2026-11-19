"""Orchestrator-owned, synthetic text only. Keep held-out labels separate from runner input."""

import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "directory", type=Path, help="write inputs, labels and checksums here"
)
out = parser.parse_args().directory
out.mkdir(parents=True, exist_ok=True)
apps = [
    "Notepad",
    "Spotify",
    "Calculator",
    "Chrome",
    "Paint",
    "Slack",
    "Zoom",
    "Calendar",
    "Word",
    "Excel",
]
windows = [
    "Notepad",
    "Calendar",
    "Paint",
    "Calculator",
    "Slack",
    "Word",
    "Zoom",
    "Excel",
    "Spotify",
]
rows = []


def group(kind, lines):
    for line in lines.strip().splitlines():
        text, _, target = line.partition("|")
        rows.append((kind, target, text))


group(
    "ask",
    """
What is the difference between a solar day and a sidereal day?
Can you explain why a calculator sometimes rounds off the last digit?
Does noaa.gov have historical temperature records?
Why would a browser open the wrong profile?
Is a new tab in Chrome the same thing as a new window?
What does the phrase read the room mean?
When I say open a file, what is the computer actually doing?
I'm trying to understand how nasa.gov and esa.int differ.
Can we talk about why Spotify sounds quieter on headphones?
Who maintains archive.org?
What's the difference between typing a URL and searching for it?
Remind me what the icon shaped like a magnifying glass represents.
Could you describe how a clipboard works?
How do I tell whether a window is already running?
Is Paint a good app for cropping a photo?
I wonder why the sky is darker at high altitude.
""",
)
group(
    "navigate",
    """
Please visit archive.org.|https://archive.org
Put bbc.co.uk up in the browser for me.|https://bbc.co.uk
Head over to nps.gov real quick.|https://nps.gov
Let's go straight to noaa.gov.|https://noaa.gov
Bring up the page at esa.int please.|https://esa.int
Would you take me to wikipedia dot org?|https://wikipedia.org
I need to get to weather.gov in my browser.|https://weather.gov
Could you load github.com in a tab?|https://github.com
Pop open example.org please.|https://example.org
Hop over to gutenberg.org.|https://gutenberg.org
I'd like to visit python.org now.|https://python.org
Navigate the browser to w3.org.|https://w3.org
Pull up the site at loc.gov.|https://loc.gov
Open a tab for blender.org.|https://blender.org
Take me directly to mit.edu.|https://mit.edu
Can we head to usps.com?|https://usps.com
""",
)
group(
    "open_app",
    """
Launch Calculator please.|Calculator
Could you start Paint for me?|Paint
Fire up a fresh copy of Notepad.|Notepad
I want to launch Spotify.|Spotify
Please open the Slack application.|Slack
Would you start up Zoom?|Zoom
Get Excel running for me.|Excel
Can you launch Word on this machine?|Word
Pop open the Calendar app.|Calendar
Start Chrome on my computer.|Chrome
Open up Notepad so I can jot something down.|Notepad
Would you mind launching Calculator?|Calculator
I need you to start the Spotify app.|Spotify
Please launch the Paint program.|Paint
Let's open the application called Word.|Word
Boot up Excel for a new spreadsheet.|Excel
""",
)
group(
    "switch_app",
    """
Focus the Calendar window.|Calendar
Bring my already-open Notepad to the foreground.|Notepad
Take me back to Slack.|Slack
Switch over to the running Calculator.|Calculator
Give Excel focus please.|Excel
Put the existing Word window in front.|Word
Return me to Paint.|Paint
Can I get back into the Zoom window?|Zoom
Select the Spotify window that's already open.|Spotify
Make Calendar the active window.|Calendar
Jump back over to Notepad.|Notepad
Switch from here to Word.|Word
Please foreground the Slack window.|Slack
Go back to the open Excel spreadsheet window.|Excel
I want the existing Zoom window back in front.|Zoom
Activate the Calculator window I was using.|Calculator
""",
)
group(
    "search",
    """
Search the web for lunar eclipse times.|lunar eclipse times
Look up sourdough hydration ratios online.|sourdough hydration ratios
Google train timetables.|train timetables
Do a web search for red panda habitat.|red panda habitat
Search online for blueberry muffin recipes.|blueberry muffin recipes
Could you search for budget microphones?|budget microphones
Find web results for alpine trail maps.|alpine trail maps
Type ocean tide predictions into a web search.|ocean tide predictions
Run a search for miniature painting tips.|miniature painting tips
Look up paper airplane designs.|paper airplane designs
Can you Google jazz chord progressions?|jazz chord progressions
Search the internet for public observatories.|public observatories
Find online results for stained glass patterns.|stained glass patterns
I want to search the web for repair cafes.|repair cafes
Do an internet search for space station sightings.|space station sightings
Search for walking tours of Boston.|walking tours of Boston
""",
)
group(
    "read_selection",
    """
Read my selection aloud.
Please speak the highlighted words.
Could you read out what I've selected?
Say the selected paragraph out loud.
Read back the bit I highlighted.
I'd like to hear the current selection.
Can you voice the text that's highlighted?
Read aloud just the selected sentence.
Please read this selection to me.
Speak the passage I have selected.
Read what's highlighted in this document.
Say my highlighted text for me.
Play back the words I selected.
Read the current selection out loud.
Let me hear this highlighted paragraph.
Tell me aloud exactly what the selection says.
""",
)
group(
    "clarify",
    """
Open either archive.org or w3.org, whichever one I meant.
Bring up one of the two, Paint or Notepad; I'm not sure which.
Take me to that website I have in mind.
Switch me to that window. I forget its name.
Open the app whose name I can't remember.
Go to one of these sites, python.org or blender.org.
Get the other application running. I haven't said which one.
Navigate to the address I'm thinking of.
Open Spotify or Zoom, I haven't decided which.
I want one of those websites open. Which one did I mean?
Bring the window I mean to the front. I haven't picked one.
Launch that unnamed tool for me.
Visit the site whose address I forgot.
Either show me w3.org or open archive.org; please ask me which.
Switch to either Calendar or Slack. Let me choose first.
Open the, uh... wait, which app was it?
""",
)
# Only the independent evaluator reads these labels. Input IDs do not encode a route.
rng = random.Random(719708)
rng.shuffle(rows)


def build(source, split):
    data, labels = [], []
    for n, (kind, target, text) in enumerate(source):
        mode = "assistant" if n % 2 == 0 else "wake"
        id_ = hashlib.sha256(f"{split}/{text}".encode()).hexdigest()[:16]
        data.append(
            {"id": id_, "mode": mode, "text": text, "apps": apps, "windows": windows}
        )
        labels.append({"id": id_, "kind": kind, "target": target})
    return data, labels


def write(name, records):
    path = out / name
    raw = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records
    ).encode()
    if path.exists() and path.read_bytes() != raw:
        raise RuntimeError(f"frozen corpus differs: {path}")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


assert Counter(row[0] for row in rows) == {
    k: 16
    for k in (
        "ask",
        "navigate",
        "open_app",
        "switch_app",
        "search",
        "read_selection",
        "clarify",
    )
}
data, labels = build(rows, "holdout")
assert len(set(r["id"] for r in data)) == len(data)
manifest = {
    f: write(f, v)
    for f, v in [("heldout-input.jsonl", data), ("heldout-labels.jsonl", labels)]
}
public = []
for words, target in [
    ("Open boo uh youtube.com", "https://youtube.com"),
    ("Yo, open youtube.Com for me.", "https://youtube.com"),
    ("Open boo uh youtube.comYo, open youtube.Com for me.", "https://youtube.com"),
    ("Could you take me straight to nasa.gov?", "https://nasa.gov"),
    ("Could you show me the official website at nasa.gov?", "https://nasa.gov"),
]:
    public.append(("navigate", target, words))
public += [
    ("ask", "", "Talk me through the difference between nasa.gov and noaa.gov."),
    ("ask", "", "Could you tell me the difference between nasa.gov and noaa.gov?"),
    ("clarify", "", "Show me either nasa.gov or noaa.gov."),
    ("open_app", "Chrome", "Open Chrome"),
    (
        "literal",
        "open nasa.gov and send it",
        "Jarvis, type the words open nasa.gov and send it",
    ),
]
data, labels = build(public, "contract")
base = list(zip(data, labels))
for row, label in base:
    row2 = dict(
        row,
        mode="wake" if row["mode"] == "assistant" else "assistant",
        id=row["id"] + "b",
    )
    data.append(row2)
    labels.append(dict(label, id=row2["id"]))
for n, words in enumerate(
    (
        "open youtube.com",
        "Start with nasa.gov and noaa.gov as references in the intro.",
        "the second one",
    )
):
    row = dict(data[0], id="dictation" + str(n), mode="dictation", text=words)
    data.append(row)
    labels.append({"id": row["id"], "kind": "dictate", "target": words})
manifest.update(
    {
        f: write(f, v)
        for f, v in [("contract-input.jsonl", data), ("contract-labels.jsonl", labels)]
    }
)
(out / "corpus-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("112 balanced held-out cases,", len(data), "public control cases", manifest)
