"""The open-position half of the calendar guard: hold, tighten, or flatten.

Entering into a release and holding through one are different decisions, and
the trap is that they look like the same question. Declining an entry costs a
missed trade. Closing a working position costs the spread twice, abandons a
stop placed on structure, and hands back the trade's remaining expectancy on
an event that might not touch it. A guard that answers both with "blacked
out" bleeds the account through costs alone, which is why
``docs/roadmap.md`` Phase 4 splits them and why one test here asserts the two
functions disagree.

The rule is `TIGHTEN_BUFFER_R` and it has no gap. At or above the buffer the
position can absorb a full stop-distance spike and still be at breakeven, so
it is managed rather than closed. Below it, including in modest profit, there
is not enough cover and it is closed. A trade up 0.5R ten minutes before a
rate decision has less than half a stop of protection, and the plan's news
rule exists for exactly that spike.

Two things this file pins that a reading of the rule alone would miss. The
threshold is read from the module rather than written as ``1.0``, so moving it
moves the tests with it and a hardcoded comparison in the implementation
fails. And the reason is required on every answer except the one where
nothing is in range, because an instruction to close a live position with no
event named is one the journal cannot later explain.

Every event is built in this file. Nothing reaches the network.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import fbe.calendar_guard
from fbe.calendar_guard import (
    TIGHTEN_BUFFER_R,
    CalendarCoverage,
    OpenPositionAction,
    action_for_open_position,
    is_blacked_out,
)
from fbe.config import DataConfig
from fbe.types import CalendarEvent

CONFIG = DataConfig()
"""Defaults: 30 minutes before a release and 60 after."""

WHEN = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
"""A Monday morning, the moment the decision is being made."""

QUIET_TITLE = "Widget Sentiment Index"
"""A title matching no category in `HIGH_IMPACT_KEYWORDS`.

Asserted in ``tests/test_blackout_windows.py``, which owns `is_high_impact`.
Used here only to build an event that produces no window.
"""


def event(
    currency: str = "EUR",
    scheduled_for: datetime | None = None,
    *,
    title: str = "CPI y/y",
    impact: str = "High",
) -> CalendarEvent:
    """Build one calendar event, defaulting to a qualifying one at `WHEN`."""
    return CalendarEvent(
        title=title,
        currency=currency,
        scheduled_for=WHEN if scheduled_for is None else scheduled_for,
        impact=impact,
    )


ABOVE = TIGHTEN_BUFFER_R + 0.5
BELOW = TIGHTEN_BUFFER_R - 0.5
"""Either side of the buffer, derived from it rather than written out.

A literal 1.5 and 0.5 would keep passing if the threshold moved, which is the
config-drift defect the engineering standards name: a number that exists in
two places will disagree, and the disagreement will be silent.
"""


# --- the vocabulary itself ----------------------------------------------------


def test_every_action_is_an_instruction_a_person_carries_out() -> None:
    """A wire-format test, not evidence that the module places no orders.

    These are `StrEnum` values and they reach the report and the journal as
    strings, so renaming one breaks a written record rather than renaming a
    symbol. Nothing else in the suite catches that.

    It cannot fail on a change to `action_for_open_position`, because it never
    calls it. It sits with the enum rather than under criterion 7, where it
    read as an argument that the guard is advisory and was no such thing.
    """
    assert {member.value for member in OpenPositionAction} == {
        "hold",
        "tighten",
        "flatten",
    }


# --- nothing in range --------------------------------------------------------


def test_a_position_with_no_event_in_range_is_held() -> None:
    """The common case, and the one the reason is allowed to be absent on."""
    action, reason = action_for_open_position(
        "EURUSD", WHEN, [event(scheduled_for=WHEN + timedelta(days=1))], CONFIG
    )

    assert action is OpenPositionAction.HOLD
    assert reason is None


def test_an_empty_calendar_holds_rather_than_closing() -> None:
    """A position is not closed because nothing was scheduled.

    This function answers from the windows it can see. Whether the calendar
    could see anything is `coverage_gap`'s question and `is_blacked_out`'s to
    ask, and a guard that flattened on an empty sequence would close every
    position on the first failed fetch.
    """
    action, reason = action_for_open_position("EURUSD", WHEN, [], CONFIG)

    assert action is OpenPositionAction.HOLD
    assert reason is None


def test_an_event_that_does_not_qualify_leaves_the_position_alone() -> None:
    """`is_high_impact` decides, in one place. A second filter here would drift."""
    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event(title=QUIET_TITLE, impact="Low")], CONFIG
    )

    assert action is OpenPositionAction.HOLD


def test_an_event_on_neither_leg_leaves_the_position_alone() -> None:
    """A yen release is not a reason to touch a euro-dollar position."""
    action, _ = action_for_open_position("EURUSD", WHEN, [event("JPY")], CONFIG)

    assert action is OpenPositionAction.HOLD


# --- inside a window, either side of the buffer -------------------------------


def test_a_position_with_enough_buffer_is_tightened() -> None:
    """At or above the buffer the trade survives a full stop-distance spike."""
    action, reason = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=ABOVE
    )

    assert action is OpenPositionAction.TIGHTEN
    assert reason is not None


def test_a_position_without_enough_buffer_is_flattened() -> None:
    action, reason = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=BELOW
    )

    assert action is OpenPositionAction.FLATTEN
    assert reason is not None


def test_the_buffer_itself_is_tightened_and_not_flattened() -> None:
    """The comparison is inclusive, which `TIGHTEN_BUFFER_R`'s docstring states.

    The boundary is the whole of the rule: the docstring says there are two
    branches and no gap between them, so exactly one of them has to own this
    value and the docstring says which.
    """
    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=TIGHTEN_BUFFER_R
    )

    assert action is OpenPositionAction.TIGHTEN


def test_a_position_in_modest_profit_is_still_flattened() -> None:
    """Named separately because it is the counter-intuitive half.

    Up 0.5R with a rate decision ten minutes out is less than half a stop of
    cover, and the spike through a structural stop is the loss the plan's news
    rule exists to prevent. A reader who assumes "in profit means safe" is the
    reason this branch is worth its own test.
    """
    assert 0.0 < 0.5 < TIGHTEN_BUFFER_R

    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=0.5
    )

    assert action is OpenPositionAction.FLATTEN


def test_breakeven_is_the_default_and_is_flattened() -> None:
    """A caller that does not track open profit gets the conservative answer."""
    action, _ = action_for_open_position("EURUSD", WHEN, [event()], CONFIG)

    assert action is OpenPositionAction.FLATTEN


def test_a_losing_position_is_flattened() -> None:
    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=-2.0
    )

    assert action is OpenPositionAction.FLATTEN


def test_the_threshold_is_read_from_the_module_and_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Move the buffer and the answer moves with it.

    Without this, a comparison written as ``>= 1.0`` passes every test above,
    because every fixture is positioned relative to a threshold that happens
    to equal the literal. This is the only test that can tell them apart.
    """
    monkeypatch.setattr("fbe.calendar_guard.TIGHTEN_BUFFER_R", 3.0)

    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=2.0
    )

    assert action is OpenPositionAction.FLATTEN


# --- both legs, and the window's edges ----------------------------------------


def test_an_event_on_the_base_leg_reaches_the_position() -> None:
    """Both halves of a ratio move it. The base leg is the easy one to miss."""
    action, reason = action_for_open_position(
        "EURUSD", WHEN, [event("EUR")], CONFIG, unrealised_r=BELOW
    )

    assert action is OpenPositionAction.FLATTEN
    assert reason is not None
    assert "EUR" in reason


def test_an_event_on_the_quote_leg_reaches_the_position() -> None:
    action, reason = action_for_open_position(
        "EURUSD", WHEN, [event("USD")], CONFIG, unrealised_r=BELOW
    )

    assert action is OpenPositionAction.FLATTEN
    assert reason is not None
    assert "USD" in reason


def test_the_window_opens_before_the_release_and_the_guard_acts_then() -> None:
    """The instruction has to arrive before the release, not on it.

    Thirty minutes ahead by default, which is the pre-positioning drift the
    window's width exists to cover. A guard that only acted at the scheduled
    time would be advising a close while the spike was already happening.
    """
    action, _ = action_for_open_position(
        "EURUSD",
        WHEN,
        [event(scheduled_for=WHEN + timedelta(minutes=20))],
        CONFIG,
        unrealised_r=BELOW,
    )

    assert action is OpenPositionAction.FLATTEN


def test_the_exact_edges_of_the_window_are_inside_it() -> None:
    """Closed at both ends, which is what `blackout_windows` builds.

    A no-trade window is safer closed than open, and the instant the window
    opens is the one a scheduled run is most likely to land on.
    """
    release = WHEN
    opens = release - timedelta(minutes=CONFIG.calendar_blackout_before_min)
    closes = release + timedelta(minutes=CONFIG.calendar_blackout_after_min)

    for moment in (opens, closes):
        action, _ = action_for_open_position(
            "EURUSD", moment, [event(scheduled_for=release)], CONFIG
        )
        assert action is OpenPositionAction.FLATTEN, moment


def test_a_moment_outside_the_window_by_a_minute_is_held() -> None:
    release = WHEN
    opens = release - timedelta(minutes=CONFIG.calendar_blackout_before_min)

    action, reason = action_for_open_position(
        "EURUSD", opens - timedelta(minutes=1), [event(scheduled_for=release)], CONFIG
    )

    assert action is OpenPositionAction.HOLD
    assert reason is None


def test_the_window_widths_come_from_config() -> None:
    """A moment that is outside the default window and inside a widened one."""
    wide = DataConfig(calendar_blackout_before_min=120, calendar_blackout_after_min=60)
    moment = WHEN - timedelta(minutes=90)

    assert action_for_open_position("EURUSD", moment, [event()], CONFIG)[0] is (
        OpenPositionAction.HOLD
    )
    assert action_for_open_position("EURUSD", moment, [event()], wide)[0] is (
        OpenPositionAction.FLATTEN
    )


# --- the reason ---------------------------------------------------------------


@pytest.mark.parametrize(
    "unrealised_r, expected",
    [(BELOW, OpenPositionAction.FLATTEN), (ABOVE, OpenPositionAction.TIGHTEN)],
    ids=["flatten", "tighten"],
)
def test_the_reason_names_the_event_its_currency_and_its_time(
    unrealised_r: float, expected: OpenPositionAction
) -> None:
    """The journal records this string, and later has to explain an early exit.

    "Closed on news" is not a record. "Closed on EUR CPI y/y at 09:00 UTC" is
    one a review can check against what the release actually did.

    Both branches, because they are two separate returns. Asserting the full
    string on one and only the title on the other would leave the second
    pinned by ``reason is not None``, which any string satisfies.
    """
    action, reason = action_for_open_position(
        "EURUSD",
        WHEN,
        [event("EUR", title="CPI y/y")],
        CONFIG,
        unrealised_r=unrealised_r,
    )

    assert action is expected
    assert reason is not None
    assert "EUR" in reason
    assert "CPI y/y" in reason
    assert "09:00" in reason
    assert "UTC" in reason


def test_the_reason_reports_the_release_time_in_utc() -> None:
    """An event carried in another zone is reported in UTC, like everything here.

    A reason reading "at 11:00" for a 09:00 UTC release would send the owner
    to the wrong hour of their own calendar.
    """
    berlin = datetime(2026, 9, 14, 11, 0, tzinfo=timezone(timedelta(hours=2)))

    _, reason = action_for_open_position(
        "EURUSD", WHEN, [event("EUR", scheduled_for=berlin)], CONFIG, unrealised_r=BELOW
    )

    assert reason is not None
    assert "09:00" in reason
    assert "11:00" not in reason


@pytest.mark.parametrize(
    "unrealised_r, expected",
    [(0.0, OpenPositionAction.FLATTEN), (TIGHTEN_BUFFER_R, OpenPositionAction.TIGHTEN)],
    ids=["no-buffer", "buffered"],
)
def test_with_both_legs_in_window_the_base_leg_is_the_one_named(
    unrealised_r: float, expected: OpenPositionAction
) -> None:
    """Which release the journal records when two are in range at once.

    The action cannot differ, since both legs are inside a window and the
    buffer decides alone, so reversing the search order changes nothing a
    caller acts on. It changes the reason, and the reason is the only record
    of why a live position was closed. Asserted on both sides of the buffer so
    the ordering is pinned for whichever instruction comes out.
    """
    action, reason = action_for_open_position(
        "EURUSD",
        WHEN,
        [event("USD", title="Non-Farm Payrolls"), event("EUR", title="CPI y/y")],
        CONFIG,
        unrealised_r=unrealised_r,
    )

    assert action is expected
    assert reason is not None
    assert "EUR CPI y/y" in reason
    assert "Non-Farm Payrolls" not in reason


def test_the_reason_names_the_event_whose_window_contains_the_moment() -> None:
    """With two in range, the one being reported has to be the one acting.

    A merged window cannot say which release it came from, so an implementation
    reading the merged set has nothing to name and would report whichever event
    sorted first.
    """
    _, reason = action_for_open_position(
        "EURUSD",
        WHEN,
        [
            event("EUR", scheduled_for=WHEN - timedelta(hours=6), title="Retail Sales"),
            event("EUR", scheduled_for=WHEN, title="CPI y/y"),
        ],
        CONFIG,
        unrealised_r=BELOW,
    )

    assert reason is not None
    assert "CPI y/y" in reason
    assert "Retail Sales" not in reason


# --- refusals -----------------------------------------------------------------


def test_a_naive_decision_time_is_refused() -> None:
    """Reading 14:30 SAST as 14:30 UTC misses the payrolls window by two hours."""
    naive = datetime(2026, 9, 14, 9, 0)

    with pytest.raises(ValueError, match="naive"):
        action_for_open_position("EURUSD", naive, [event()], CONFIG)


def test_a_naive_event_time_is_refused() -> None:
    naive = CalendarEvent(
        title="CPI y/y",
        currency="EUR",
        scheduled_for=datetime(2026, 9, 14, 9, 0),
        impact="High",
    )

    with pytest.raises(ValueError, match="naive"):
        action_for_open_position("EURUSD", WHEN, [naive], CONFIG)


def test_a_naive_event_is_refused_on_a_currency_the_pair_does_not_hold() -> None:
    """The check is on the feed's shape, not on whether the event is relevant.

    The test above passes even with this function's own check removed, because
    a naive event on a leg of the pair reaches `blackout_windows`, which
    refuses it. This one does not: a yen event on EURUSD never reaches the
    window builder.

    ``tests/test_blackout_windows.py`` carries the same test for
    `is_blacked_out` and records it as a live defect there, where the refusal
    was conditional on the event sitting on a leg, so a naive yen event was
    silently ignored on EURUSD and would have reached a qualifying event on
    the next run. Reproducing the guard without reproducing its test is how
    that comes back.
    """
    naive = CalendarEvent(
        title="Policy Rate",
        currency="JPY",
        scheduled_for=datetime(2026, 9, 14, 9, 0),
        impact="High",
    )

    with pytest.raises(ValueError, match="naive"):
        action_for_open_position("EURUSD", WHEN, [naive], CONFIG)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_an_open_profit_that_is_not_a_number_is_refused(value: float) -> None:
    """The guard on the one input that decides between two live instructions.

    A NaN fails every comparison, so it falls past the buffer test into
    FLATTEN: an instruction to close a position on a real account, reached by
    refusing to answer rather than by deciding. The infinities are in here
    because a check written as ``value != value`` catches only the NaN and
    reads as if it catches all three.

    Asserted twice, once with an event in range and once with nothing in
    range, so the refusal is about the argument rather than about the branch
    it would otherwise have fallen into.
    """
    with pytest.raises(ValueError, match="finite"):
        action_for_open_position("EURUSD", WHEN, [event()], CONFIG, unrealised_r=value)

    with pytest.raises(ValueError, match="finite"):
        action_for_open_position("EURUSD", WHEN, [], CONFIG, unrealised_r=value)


@pytest.mark.parametrize("pair", ["EUR", "EURUSDX"])
def test_a_pair_of_the_wrong_length_is_refused(pair: str) -> None:
    """Matched on the message, as ``tests/test_blackout_windows.py`` does.

    Asserting only `ValueError` would pass with `split_pair` taken out
    altogether, and that is the canonical splitter and the one place the
    quoting convention is enforced.
    """
    with pytest.raises(ValueError, match="six-character"):
        action_for_open_position(pair, WHEN, [event()], CONFIG)


@pytest.mark.parametrize("pair", ["EURXYZ", "XYZUSD"])
def test_a_leg_that_is_not_a_currency_is_refused(pair: str) -> None:
    """One mistyped character splits as cleanly as a real pair.

    It would then match no event and this function would answer HOLD, which is
    an instruction to keep a position through a release nobody checked for.
    """
    with pytest.raises(ValueError, match="not a G10 currency"):
        action_for_open_position(pair, WHEN, [event()], CONFIG)


def test_a_lowercase_pair_is_answered_rather_than_refused() -> None:
    """Hand-typed from the pre-trade check, as `is_blacked_out` already allows."""
    action, _ = action_for_open_position(
        "eurusd", WHEN, [event()], CONFIG, unrealised_r=BELOW
    )

    assert action is OpenPositionAction.FLATTEN


# --- the two questions are not the same question ------------------------------


def covering(*events: CalendarEvent) -> CalendarCoverage:
    """Coverage reaching an hour past `WHEN`, so `coverage_gap` is satisfied."""
    return CalendarCoverage(events=events, covers_through=WHEN + timedelta(hours=1))


def test_the_open_position_answer_differs_from_the_entry_answer() -> None:
    """Criterion 6, and the reason this module has two functions.

    Same pair, same moment, same event. A new entry is blocked, because
    declining it costs a missed trade. A position already on with a full stop
    of buffer is tightened rather than closed, because closing it pays the
    spread twice and abandons a stop placed on structure.

    An implementation that answered the open-position question by calling
    `is_blacked_out` and translating True to FLATTEN would pass every other
    test in this file and fail this one.
    """
    blocked, entry_reason = is_blacked_out("EURUSD", WHEN, covering(event()), CONFIG)
    action, held_reason = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=ABOVE
    )

    assert blocked is True
    assert action is OpenPositionAction.TIGHTEN
    assert entry_reason is not None
    assert held_reason is not None


def test_the_two_agree_when_there_is_no_buffer() -> None:
    """The other half of the same comparison, so the first is not a fluke.

    Without this, an implementation that always returned TIGHTEN inside a
    window would pass the disagreement test. They are meant to differ on one
    case, not on every case.
    """
    blocked, _ = is_blacked_out("EURUSD", WHEN, covering(event()), CONFIG)
    action, _ = action_for_open_position(
        "EURUSD", WHEN, [event()], CONFIG, unrealised_r=BELOW
    )

    assert blocked is True
    assert action is OpenPositionAction.FLATTEN


# --- advisory only ------------------------------------------------------------


def test_the_guard_returns_an_instruction_and_changes_nothing() -> None:
    """Criterion 7, as far as it is checkable. See the test below it.

    Purity is the part that can fail: the call cannot have acted on the
    position by mutating what it was handed, and asking twice gives the same
    answer rather than a different one, which a memoised mutable default
    would break.

    An earlier version also asserted two default `DataConfig` instances are
    equal. They are, by construction, since it is a frozen dataclass with
    deterministic defaults, so that assertion held with the function body
    replaced by ``return HOLD, None``. It read as a check and was not one.
    """
    events = [event()]
    before = list(events)

    first = action_for_open_position("EURUSD", WHEN, events, CONFIG, unrealised_r=BELOW)
    second = action_for_open_position(
        "EURUSD", WHEN, events, CONFIG, unrealised_r=BELOW
    )

    assert first == second
    assert events == before


def test_the_module_imports_nothing_that_could_place_an_order() -> None:
    """The checkable half of criterion 7.

    "Nothing in this module places, sizes or closes anything" cannot be
    asserted head-on, because nothing in this package can place an order at
    all, so such a test passes for the wrong reason and would keep passing
    the day one could. What can be asserted is the import surface: the guard
    reaches `fbe.config`, `fbe.types` and `fbe.universe`, none of which can
    act on an account, and nothing else.

    This is what fails the day a broker client is wired into the guard rather
    than an instruction being returned for the owner to carry out.
    """
    tree = ast.parse(Path(fbe.calendar_guard.__file__).read_text(encoding="utf-8"))

    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(alias.name for alias in node.names)

    assert {name for name in reached if name.startswith("fbe")} == {
        "fbe.config",
        "fbe.types",
        "fbe.universe",
    }
