"""A global event is named on every pair and blocks none of them.

Issue #227, ruled option 3 on 2026-09-22. The feed publishes some releases
under "All", which `fbe.datasources.calendar` maps to ``GLOBAL``, and the guard
matched events by leg, so a G20 or BRICS summit was dropped silently and the
plan's item 10, avoid trading around geopolitical events, could never fire.
Item 10 stays a human judgement (`docs/data-sources.md`), so the engine names
the event as a non-blocking ``event:global`` marker rather than standing aside.

Everything here reads the committed capture's BRICS summit row, so the case is
checked against what the feed sent rather than a hand-built event.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fbe.bias import BLOCKERS, CalendarGuard, apply_filters, blocking, kind_of
from fbe.calendar_guard import (
    CalendarCoverage,
    global_events,
    is_blacked_out,
    is_high_impact,
)
from fbe.config import Config, DataConfig
from fbe.dashboard.build import render_dashboard
from fbe.datasources.calendar import CalendarSource
from fbe.datasources.registry import GLOBAL
from fbe.report import render_report
from fbe.types import (
    CalendarEvent,
    Conviction,
    CurrencyScore,
    Direction,
    PairBias,
)
from fbe.universe import ALL_PAIRS, G10, split_pair

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "tests" / "fixtures" / "forexfactory_calendar_thisweek.json"
CONFIG = DataConfig()

GLOBAL_RULE = (
    "A global event never blocks a pair. It is named on every pair as "
    "`event:global`, and whether to stand aside for it is the owner's judgement."
)
"""Stated in `fbe.calendar_guard` and in ``docs/risk-and-execution.md`` in these
words, and asserted in both, so the two cannot drift."""


def _summit() -> CalendarEvent:
    """The capture's BRICS row, parsed by the real source rather than rebuilt."""
    rows = json.loads(CAPTURE.read_text(encoding="utf-8"))
    (row,) = [row for row in rows if row["title"] == "BRICS Summit"]
    parsed = CalendarSource(CONFIG)._event(row)
    assert parsed is not None
    return parsed


def _coverage(*events: CalendarEvent) -> CalendarCoverage:
    moment = events[0].scheduled_for
    return CalendarCoverage(
        events=events, covers_through=moment + timedelta(days=2), fetch_ok=True
    )


def _guard(events: tuple[CalendarEvent, ...]) -> CalendarGuard:
    """A per-currency, per-day guard over the capture, the shape `apply_filters`
    takes. It asks the same functions a live guard would: `global_events` for
    ``GLOBAL`` and `is_blacked_out` semantics for a leg, here no leg event."""
    coverage = _coverage(*events)
    summit = events[0]

    def guard(currency: str, on: date) -> tuple[tuple[str, ...], str | None]:
        if currency == GLOBAL:
            return tuple(global_events(summit.scheduled_for, coverage, CONFIG)), None
        return (), None

    return guard


def _bias(pair: str) -> PairBias:
    base, quote = split_pair(pair)
    return PairBias(
        pair=pair,
        base=base,
        quote=quote,
        spread=1.4,
        direction=Direction.LONG,
        conviction=Conviction.MEDIUM,
        asof=date(2026, 9, 13),
        base_score=0.8,
        quote_score=-0.6,
        agreement=0.8,
    )


def _scores() -> dict[str, CurrencyScore]:
    return {
        code: CurrencyScore(
            currency=code,
            composite=0.0,
            pillars={},
            dispersion=0.2,
            coverage=1.0,
            asof=date(2026, 9, 13),
        )
        for code in G10
    }


def _filtered() -> list[PairBias]:
    summit = _summit()
    guard = _guard((summit,))
    return [
        apply_filters(
            _bias(pair),
            _scores(),
            Config(),
            summit.scheduled_for.date(),
            calendar_guard=guard,
            cost_ratio=0.0,
        )
        for pair in ALL_PAIRS
    ]


def test_the_capture_row_is_global_and_rated_high_impact() -> None:
    """The keyword half rates it high, which is what made the category dead."""
    summit = _summit()

    assert summit.currency == GLOBAL
    assert is_high_impact(summit) is True


def test_the_guard_names_a_global_event_inside_its_window() -> None:
    summit = _summit()

    named = global_events(summit.scheduled_for, _coverage(summit), CONFIG)

    assert len(named) == 1
    assert "BRICS Summit" in named[0]


def test_outside_its_window_a_global_event_is_not_named() -> None:
    summit = _summit()
    later = summit.scheduled_for + timedelta(hours=3)

    assert global_events(later, _coverage(summit), CONFIG) == ()


def test_a_leg_event_is_not_a_global_event() -> None:
    """Only ``GLOBAL`` rows. A EUR release is the leg check's, not this one's."""
    summit = _summit()
    euro = CalendarEvent(
        currency="EUR",
        title="CPI y/y",
        scheduled_for=summit.scheduled_for,
        impact="High",
    )

    assert global_events(summit.scheduled_for, _coverage(euro), CONFIG) == ()


@pytest.mark.parametrize("pair", ALL_PAIRS)
def test_the_entry_question_is_never_blocked_by_it(pair: str) -> None:
    summit = _summit()

    assert is_blacked_out(pair, summit.scheduled_for, _coverage(summit), CONFIG) == (
        False,
        None,
    )


def test_every_pair_carries_the_marker_and_none_is_made_untradeable() -> None:
    """Asserted both ways on all 28 pairs, from the capture's row."""
    filtered = _filtered()

    assert len(filtered) == len(ALL_PAIRS)
    for pair in filtered:
        markers = [m for m in pair.blockers if kind_of(m) == "event:global"]
        assert len(markers) == 1, pair.pair
        assert "BRICS Summit" in markers[0]
        assert pair.tradeable, pair.pair
        assert not blocking(pair.blockers), pair.pair


def test_the_marker_is_a_non_blocking_kind() -> None:
    assert BLOCKERS["event:global"] is False
    assert kind_of("event:global: BRICS Summit at 2026-09-13 08:15 UTC") == (
        "event:global"
    )


def test_the_report_shows_it_on_a_tradeable_row() -> None:
    from fbe.types import BiasReport

    pairs = tuple(_filtered())
    rendered = render_report(
        BiasReport(
            asof=date(2026, 9, 13),
            generated_at=_generated(),
            currencies=(),
            pairs=pairs,
            config_digest="abc123",
        )
    )

    rows = [line for line in rendered.splitlines() if line.startswith("| EURUSD ")]
    assert rows and "event:global" in rows[0] and "BRICS Summit" in rows[0]
    assert "yes" in rows[0]


def test_the_dashboard_names_the_event_in_its_run_conditions() -> None:
    """Carried by every pair, so printed once as a run condition, per #192, and
    with the event named rather than a bare kind the owner has to look up."""
    from fbe.types import BiasReport

    pairs = tuple(_filtered())
    page = render_dashboard(
        BiasReport(
            asof=date(2026, 9, 13),
            generated_at=_generated(),
            currencies=(),
            pairs=pairs,
            config_digest="abc123",
        )
    )
    (conditions,) = re.findall(
        r'<p class="meta run-conditions">(.*?)</p>', page, flags=re.S
    )

    assert "event:global" in conditions
    assert "BRICS Summit" in conditions
    assert "28 of 28" in conditions


def _generated() -> datetime:
    from datetime import UTC

    return datetime(2026, 9, 13, 6, 0, tzinfo=UTC)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_rule_is_stated_in_the_guard_and_the_spec_in_the_same_words() -> None:
    assert GLOBAL_RULE in _flat(REPO / "src" / "fbe" / "calendar_guard.py")
    assert GLOBAL_RULE in _flat(REPO / "docs" / "risk-and-execution.md")


def test_the_spec_says_item_ten_is_the_owners_judgement() -> None:
    """In the words `docs/data-sources.md` already uses."""
    words = "Item 10 stays a human judgement."

    assert words in _flat(REPO / "docs" / "data-sources.md")
    assert words in _flat(REPO / "docs" / "risk-and-execution.md")
