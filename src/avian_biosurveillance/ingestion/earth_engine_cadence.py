"""Bi-weekly ingestion cadence for Google Earth Engine grid profiling.

Google Earth Engine inputs (vegetation indices, land cover, soil moisture,
long-window climate aggregates) change slowly and are expensive to profile, so
they are ingested on a fixed bi-weekly cadence rather than daily.  Observation
dates are anchored to the **1st and 15th of every month** so coverage checks,
catchup validation, and as-of joins stay deterministic and replayable.

All consumers that previously assumed a daily Earth Engine grain should use the
helpers in this module to enumerate or test anchor dates instead of iterating
every calendar day.
"""

from __future__ import annotations

from datetime import date, timedelta


# Calendar days-of-month that act as bi-weekly ingestion anchors.
ANCHOR_DAYS: tuple[int, ...] = (1, 15)


def is_anchor_date(value: date) -> bool:
    """Return ``True`` when *value* falls on a bi-weekly ingestion anchor."""
    return value.day in ANCHOR_DAYS


def anchor_dates_in_range(start: date, end: date) -> list[date]:
    """Return the ordered anchor dates within the inclusive ``[start, end]`` range.

    Args:
        start: First date of the window (inclusive).
        end: Last date of the window (inclusive).

    Returns:
        Anchor dates (1st/15th) between *start* and *end*, oldest first.  An
        empty list is returned when ``end < start`` or no anchor falls inside
        the window.
    """
    if end < start:
        return []
    anchors: list[date] = []
    current = start
    while current <= end:
        if current.day in ANCHOR_DAYS:
            anchors.append(current)
        current += timedelta(days=1)
    return anchors


def latest_anchor_on_or_before(value: date) -> date:
    """Return the most recent anchor date on or before *value*."""
    if value.day >= 15:
        return value.replace(day=15)
    return value.replace(day=1)


def next_anchor(value: date) -> date:
    """Return the first anchor date strictly after *value*."""
    if value.day < 15:
        return value.replace(day=15)
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)
