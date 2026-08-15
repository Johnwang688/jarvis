"""Conditions and task prompts for the STE detector-evasion pilot.

One model (Luna), three conditions, twelve tasks across three domains.
The conditions are the experiment; the domains are there because STE is
native to procedural writing and hostile to essays, and detectors are
tuned on essays. If STE only moves the needle on procedures it is useless
as evasion but still matters as a false-positive source.
"""

# --- conditions ---------------------------------------------------------
#
# C1 and C3 are deliberately close in token length. If they were not, a
# difference between them could just be "long instruction block" rather
# than anything about the style being asked for.

STE_FULL = """Write in ASD-STE100 Simplified Technical English. Follow these rules:

VOCABULARY
- Use only approved words. Each word has one meaning and one part of speech.
- Use the approved verb, not a synonym: START (not commence/initiate),
  FOLLOW (not adhere to), MAKE SURE (not ensure/verify), REMOVE (not extract).
- Use technical names and technical verbs freely when they name real
  parts, tools, or processes.
- Do not use a noun cluster of more than three words.

SENTENCES
- Procedural sentences: 20 words maximum.
- Descriptive sentences: 25 words maximum.
- One instruction per sentence. Give each command its own sentence.
- Use the active voice. Do not use the passive voice in procedures.
- Use the imperative for instructions: "Remove the cover."
- Use simple verb tenses: present, past, future. No perfect tenses.
- Do not use -ing forms as nouns or as clause openers.
- Write the article (a, an, the). Do not drop it.

STRUCTURE
- Paragraphs: 6 sentences maximum.
- Keep to one topic per paragraph.
- Use vertical lists for sequential actions.
- State a warning or caution before the step it applies to, never after."""

HUMANIZE = """Write this the way a real person would. Specifically:

- Vary your sentence length a lot. Mix very short sentences with long
  winding ones. Do not let a rhythm settle in.
- Use contractions. Start some sentences with And, But, or So.
- Avoid AI tells: no "delve", no "moreover", no "it's important to note",
  no "in today's fast-paced world", no tidy three-item lists, no
  "not only X but also Y", no em-dash-heavy asides.
- Do not open with a summary of what you are about to say, and do not
  close with a summary of what you just said.
- Let the structure be slightly uneven. Some paragraphs long, some two
  sentences. Do not make every section the same weight.
- Have an actual opinion where one is warranted. Be specific and
  concrete rather than balanced and comprehensive.
- Small imperfections are fine. Perfect polish is the tell."""

CONDITIONS = {
    "C0_baseline": "",
    "C1_ste_full": STE_FULL,
    "C3_humanize": HUMANIZE,
}

# --- task wrapper -------------------------------------------------------
#
# Length is pinned in every condition. Detector scores move with length,
# so an unpinned STE condition (which naturally runs shorter) would
# confound style with length.

WRAPPER = """{task}

Write 300-400 words. The length is a strict requirement: if your draft comes
out short, add further detail or additional steps until it reaches at least
300 words. Do not pad with repetition.

Output only the requested text - no preamble, no title, no meta-commentary."""


def build_prompt(task: str, condition: str) -> str:
    """The full user message for one (task, condition) cell."""
    style = CONDITIONS[condition]
    body = WRAPPER.format(task=task)
    return f"{style}\n\n{body}" if style else body


# --- tasks --------------------------------------------------------------

TASKS = [
    # D1 procedural - STE's native domain
    ("D1_procedural", "p1", "Write instructions for replacing the chain on a bicycle."),
    ("D1_procedural", "p2", "Write instructions for jump-starting a car with jumper cables."),
    ("D1_procedural", "p3", "Write instructions for bleeding air from a hydraulic disc brake."),
    ("D1_procedural", "p4", "Write instructions for replacing a kitchen faucet cartridge."),
    # D2 expository-technical
    ("D2_expository", "e1", "Explain how a heat pump moves heat against a temperature gradient."),
    ("D2_expository", "e2", "Explain how a differential lets a car's wheels turn at different speeds."),
    ("D2_expository", "e3", "Explain why aircraft cabins are pressurized and how the system works."),
    ("D2_expository", "e4", "Explain how error-correcting codes recover corrupted data."),
    # D3 argumentative - STE's hostile domain, the detectors' home turf
    ("D3_essay", "a1", "Argue whether universities should ban laptops in lecture halls."),
    ("D3_essay", "a2", "Argue whether remote work is better for early-career engineers."),
    ("D3_essay", "a3", "Argue whether public transit should be fare-free."),
    ("D3_essay", "a4", "Argue whether high schools should require a computer science credit."),
]

MODEL = "openai/gpt-5.6-luna"
TEMPERATURE = 0.7
WORDS_MIN, WORDS_MAX = 260, 460  # resample outside this band
