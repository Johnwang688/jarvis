"""Time and date."""

from __future__ import annotations

from datetime import datetime

from . import tool


@tool
def get_datetime() -> str:
    """Get the current local date, time, and weekday."""
    now = datetime.now().astimezone()
    return now.strftime("%A, %d %B %Y at %H:%M:%S %Z")
