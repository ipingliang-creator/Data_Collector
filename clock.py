"""Time helpers anchored to America/New_York (the desk's local zone).

All "today" / "now" decisions in this codebase — report filenames,
date-window math, IV-cache stamps — should use these helpers. Using
naive `date.today()` or `datetime.now(timezone.utc)` produces wrong
filenames whenever the host's wall clock disagrees with ET (e.g.,
CI runners are UTC, so a 20:17 ET run on 2026-05-10 wrote a
`2026-05-11.md` report).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current timezone-aware datetime in America/New_York."""
    return datetime.now(ET)


def today_et() -> date:
    """Today's date in America/New_York."""
    return now_et().date()
