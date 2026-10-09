"""Browser control tools.

Two ways to see a page, deliberately:

  browser_snapshot   text — element refs plus page text. Works with any model,
                     and refs are exact, so no coordinate guessing.
  browser_screenshot image — needs a vision model. Use when layout, position,
                     or rendering actually matters.

Text-only models can complete a whole run on snapshots alone, which is what
makes a text-vs-vision comparison possible on identical tasks.
"""

from __future__ import annotations

import re
from typing import Annotated

from .. import untrusted
from ..browser import SESSION, BrowserError
from . import ToolResult, tool


def _first_line(exc: Exception) -> str:
    """A Playwright error, minus its call log.

    Playwright appends a call log that quotes the page's own markup ("waiting
    for <button>Ignore your instructions…</button>"), which would reach the
    model outside any fence. The first line says what failed; that is enough.
    """
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {untrusted.one_line(lines[0] if lines else '', cap=200)}"


def _kind(exc: Exception) -> str:
    """An error's type and, for a network failure, its net::ERR code — and
    nothing else. Reading a page runs the page's script, so even the *first*
    line of an evaluate error can be a message the page wrote ("Error: ignore
    your instructions…"), and a navigation error can name a URL the page
    chose. None of that is quoted."""
    code = re.search(r"net::ERR_[A-Z_]+", str(exc))
    return type(exc).__name__ + (f", {code.group(0)}" if code else "")


@tool
def browser_goto(
    url: Annotated[str, "Full URL including http:// or https://"],
) -> str:
    """Open a URL in the browser. Public sites and localhost work; private/LAN
    addresses and Jarvis's own HUD are blocked."""
    try:
        return SESSION.goto(url)
    except BrowserError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return (f"Error: could not load {url} ({_kind(exc)}). The site may be down, slow "
                "or refusing the connection.")


@tool
def browser_snapshot() -> str:
    """Read the current page as text: interactive elements with refs, plus body text.

    Take a fresh snapshot after anything that changes the page — refs are
    reassigned each time.
    """
    try:
        return SESSION.snapshot()
    except BrowserError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return (f"Error: could not read the page ({_kind(exc)}). Its own scripts may be "
                "interfering with the reader; browser_screenshot shows what it looks like.")


@tool
def browser_click(
    ref: Annotated[str, "Element ref from the latest snapshot, e.g. e12"],
) -> str:
    """Click an element by its snapshot ref."""
    try:
        return SESSION.click(ref)
    except BrowserError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: could not click {ref}: {_first_line(exc)}"


@tool
def browser_type(
    ref: Annotated[str, "Element ref of the input field"],
    text: Annotated[str, "Text to enter"],
    submit: Annotated[bool, "Press Enter afterwards"] = False,
) -> str:
    """Type into an input field, optionally submitting."""
    try:
        return SESSION.type_text(ref, text, submit)
    except BrowserError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: could not type into {ref}: {_first_line(exc)}"


@tool
def browser_screenshot(
    full_page: Annotated[bool, "Capture the whole scrollable page"] = False,
    marked: Annotated[
        bool, "Overlay each interactive element's ref (e0, e1…) as a badge on the image"
    ] = True,
) -> ToolResult:
    """Take a screenshot of the current page. Requires a vision-capable model.

    With marked=True (the default) every interactive element is labeled with a
    small ref badge; pass that ref to browser_click / browser_type. Refs are
    reassigned whenever the page changes, so take a fresh screenshot after
    every action. Use marked=False for a clean shot when only layout matters.
    """
    try:
        image, size, marks = SESSION.screenshot_b64(full_page, marked)
    except BrowserError as exc:
        return ToolResult(f"Error: {exc}")
    except Exception as exc:
        return ToolResult(f"Error: could not capture the page ({_kind(exc)}). Its own scripts "
                          "may be interfering; try again, or with marked=False.")
    note = f" {marks} interactive element(s) marked." if marked else ""
    return ToolResult(f"Screenshot captured ({size:,} bytes).{note}", image_b64=image)
