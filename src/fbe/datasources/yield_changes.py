"""Derive the two-year yield changes from the level, per ADR 0004.

No source publishes a pre-differenced government bond yield change for any G10
issuer, so ``yield_2y_chg_1m`` and ``yield_2y_chg_3m`` are computed here, once,
from the ``yield_2y`` sessions the sources already return. One implementation in
the collector rather than one per source is what ADR 0004 asks for: the window
is then the same for a Treasury yield from FRED, a Bund curve point from the ECB
and a CHF yield typed by hand.

The window, ADR 0004 Decision 1:

- the later endpoint is a session the source holds;
- the earlier endpoint is the last session on or before the same calendar day
  one or three months back, or the last day of that month when the day does not
  exist in it;
- when that session is more than `registry.YIELD_CHANGE_TOLERANCE_DAYS` before
  its target, nothing is emitted, because a window longer than the key names is
  a wrong number with the right label;
- the value is ``(later - earlier) * 100``, in basis points. Positive means the
  yield rose, which is currency-positive, as everywhere in the model.

One observation is emitted per session, not only for the newest, so a run
scored at an earlier ``asof`` sees the change that was knowable then rather than
today's.

This module knows the registry and the observation type and nothing else. It
does no I/O.
"""

from __future__ import annotations

import calendar
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from fbe.datasources.registry import (
    INDICATORS,
    YIELD_CHANGE_LEVEL,
    YIELD_CHANGE_MONTHS,
    YIELD_CHANGE_TOLERANCE_DAYS,
)
from fbe.types import Observation

__all__ = ["derive_yield_changes", "months_back"]

BASIS_POINTS_PER_PERCENTAGE_POINT = 100.0
"""A percent level differenced is in percentage points; the keys are in basis
points, the unit their `IndicatorSpec` declares and their sub-weights were
reasoned in."""


def months_back(day: date, months: int) -> date:
    """Return the same calendar day ``months`` earlier, clamped to month end.

    Args:
        day: The later session's date.
        months: How far back, ``1`` or ``3`` for the two keys today.

    Returns:
        The target date for the earlier endpoint. 31 May less three months is
        28 February, or 29 in a leap year, because ADR 0004 takes the last day
        of the target month when the day does not exist in it. Rolling forward
        into March instead would shorten a three-month window by up to three
        days on exactly the dates where nobody would think to check.

    """
    index = day.year * 12 + (day.month - 1) - months
    year, month_zero = divmod(index, 12)
    month = month_zero + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def derive_yield_changes(
    observations: Iterable[Observation],
) -> tuple[Observation, ...]:
    """Return every change the ``yield_2y`` sessions given can support.

    Args:
        observations: Any observations. Only ``yield_2y`` is read, and each
            currency's sessions are differenced against that currency alone.

    Returns:
        One observation per key in `registry.YIELD_CHANGE_MONTHS`, per currency,
        per session whose earlier endpoint exists within the tolerance. Each
        carries the later session's ``period``, ``released_at``, ``source``,
        ``series_id`` and ``frequency``, because the change is a fact about that
        session and cannot be known before it was. Empty when no currency has
        enough history: absence, never an approximation.

    Two observations of one session for one currency are not expected, since
    the collector reconciles to one per period before this runs. If they did
    arrive, the later in input order would be used, which is the collector's
    own override rule.

    """
    sessions: dict[str, dict[date, Observation]] = defaultdict(dict)
    for row in observations:
        if row.indicator == YIELD_CHANGE_LEVEL:
            sessions[row.currency][row.period] = row

    derived: list[Observation] = []
    for currency, by_day in sorted(sessions.items()):
        days = sorted(by_day)
        for key, months in YIELD_CHANGE_MONTHS.items():
            unit = INDICATORS[key].unit
            for later_day in days:
                target = months_back(later_day, months)
                position = bisect_right(days, target) - 1
                if position < 0:
                    continue
                earlier_day = days[position]
                if (target - earlier_day).days > YIELD_CHANGE_TOLERANCE_DAYS:
                    continue
                later = by_day[later_day]
                earlier = by_day[earlier_day]
                derived.append(
                    Observation(
                        indicator=key,
                        currency=currency,
                        value=(later.value - earlier.value)
                        * BASIS_POINTS_PER_PERCENTAGE_POINT,
                        period=later_day,
                        source=later.source,
                        series_id=later.series_id,
                        unit=unit,
                        frequency=later.frequency,
                        released_at=later.released_at,
                        revision=later.revision,
                    )
                )
    return tuple(derived)
