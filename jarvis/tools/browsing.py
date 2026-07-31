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

from typing import Annotated

from ..browser import SESSION, BrowserError
from . import ToolResult, tool


@tool
def browser_goto(
    url: Annotated[str, "Full URL including http:// or https://"],
) -> str:
    """Open a URL in the browser. Blocked for hosts outside the allowlist."""
    try:
        return SESSION.goto(url)
    except BrowserError as exc:
        return f"Error: {exc}"


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
        return f"Error: could not click {ref}: {type(exc).__name__}: {exc}"


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
        return f"Error: could not type into {ref}: {type(exc).__name__}: {exc}"


@tool
def browser_screenshot(
    full_page: Annotated[bool, "Capture the whole scrollable page"] = False,
) -> ToolResult:
    """Take a screenshot of the current page. Requires a vision-capable model."""
    try:
        image, size = SESSION.screenshot_b64(full_page)
    except BrowserError as exc:
        return ToolResult(f"Error: {exc}")
    return ToolResult(f"Screenshot captured ({size:,} bytes).", image_b64=image)
