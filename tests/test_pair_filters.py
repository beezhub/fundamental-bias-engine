"""Tests for `fbe.bias.apply_filters`, the hard filters.

This function decides whether a pair can be traded, and records why not. The
failures worth guarding are the ones that would leave a trader acting on a pair
the engine had already doubted, or reading silence as an all-clear:

- A blocker that short-circuits, so a pair blocked three ways reports one and
  the trader fixes the wrong thing.
- A check that did not run looking like a check that passed. An offline run has
  no calendar and no cost input, and both absences have names for that reason.
- A guard that tried and failed looking like a guard that never ran. The two
  call for opposite responses: check the calendar by hand now, versus nothing
  is wrong.
- An event on the quote leg going unnoticed. A pair has two legs and an FOMC
  evening blocks every dollar pair, not only the one being watched.
- The view being edited to match the verdict. ``direction``, ``spread`` and
  ``conviction`` are what the model thought, and a report that neutralised a
  blocked pair would lose the only thing a later review can learn from.

Every fixture builds its `PairBias` and `CurrencyScore` values directly, so the
coverage figures, the conviction and the direction can disagree with each other
on purpose.

Nothing reaches the network, and no calendar is constructed: the guard is an
injected callable and every test supplies a double.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from fbe.bias import (
    BLOCKERS,
    UNCHECKED_SUFFIX,
    UNKNOWN_SUFFIX,
    apply_filters,
)
from fbe.config import Config, ScoringConfig
from fbe.types import Conviction, CurrencyScore, Direction, PairBias

ASOF = date(2026, 9, 9)

CONFIG = Config()
"""The shipped defaults: ``min_coverage`` 0.60, ``max_cost_ratio`` 0.05."""


def score(code: str, coverage: float) -> CurrencyScore:
    """One leg's score. Only ``coverage`` is read by this function."""
    return CurrencyScore(
        currency=code,
        composite=1.0,
        pillars={},
        asof=ASOF,
        rank=1,
        dispersion=0.5,
        coverage=coverage,
    )


def legs(base: float = 1.0, quote: float = 1.0) -> dict[str, CurrencyScore]:
    """Both legs of EURUSD at the given coverages."""
    return {"EUR": score("EUR", base), "USD": score("USD", quote)}


def bias(
    *,
    direction: Direction = Direction.LONG,
    conviction: Conviction = Conviction.HIGH,
    spread: float = 1.60,
    agreement: float = 0.82,
) -> PairBias:
    """An unfiltered EURUSD bias, as `build_pair_biases` leaves it.

    ``tradeable`` is True and ``blockers`` empty on the way in, which is the
    shape that function produces: it sets neither, deliberately.
    """
    return PairBias(
        pair="EURUSD",
        base="EUR",
        quote="USD",
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=ASOF,
        base_score=0.80,
        quote_score=-0.80,
        agreement=agreement,
    )


def guard(
    found: Mapping[str, Sequence[str]] | None = None,
    unknown: Mapping[str, str] | None = None,
):
    """A `CalendarGuard` double returning what each currency was given.

    Args:
        found: ``{currency: blockers}`` for legs with a release in the window.
        unknown: ``{currency: reason}`` for legs the guard could not check.

    The protocol requires a guard reporting an unknown reason to return an
    empty ``blockers``, so the two mappings are kept apart rather than one
    overriding the other, and a currency in neither is a checked, quiet day.
    """
    found = found or {}
    unknown = unknown or {}

    def check(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        assert when == ASOF
        if currency in unknown:
            return (), unknown[currency]
        return tuple(found.get(currency, ())), None

    return check


def kinds(result: PairBias) -> list[str]:
    """Map each emitted blocker back to the `BLOCKERS` kind it belongs to.

    Two kinds carry a payload rather than being emitted verbatim: ``event``
    arrives as ``"event: <reason>"`` and ``event:unknown`` as
    ``"event:unknown: <reason>"``. Longest prefix wins, because ``"event"`` is
    itself a prefix of ``"event:unknown"`` and matching the short one first
    would file every unknown as a block.
    """
    mapped: list[str] = []
    for entry in result.blockers:
        candidates = [key for key in BLOCKERS if entry == key or entry.startswith(key)]
        assert candidates, f"{entry!r} matches no kind in BLOCKERS"
        mapped.append(max(candidates, key=len))
    return mapped


# --- the vocabulary ---------------------------------------------------------


def test_every_blocker_emitted_is_a_kind_the_enumeration_declares() -> None:
    """The enumeration is the contract and this function may not widen it.

    `BLOCKERS` is asserted against section 6 of ``docs/scoring-spec.md`` by
    `tests/test_blockers.py`, so a string emitted here that is not one of its
    kinds would be a blocker no document describes and no renderer knows to
    show. The expected set is derived from the mapping rather than retyped, so
    adding a kind there does not silently make this test stale.

    Every branch is tripped at once, which is also the only way to see that the
    list is a union rather than whichever branch ran last.
    """
    result = apply_filters(
        bias(direction=Direction.NEUTRAL, conviction=Conviction.NONE),
        legs(base=0.0, quote=0.30),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": ("EUR CPI at 09:00 UTC",)}),
        cost_ratio=0.40,
    )

    assert set(kinds(result)) <= set(BLOCKERS)
    assert len(result.blockers) == len(kinds(result))


# --- no_edge ----------------------------------------------------------------


def test_a_neutral_direction_is_nothing_to_act_on() -> None:
    """Half of `no_edge`, on its own.

    Conviction is HIGH here, so a test that only ever set both conditions
    together could not tell which one fired.
    """
    result = apply_filters(
        bias(direction=Direction.NEUTRAL, conviction=Conviction.HIGH),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "no_edge" in result.blockers
    assert result.tradeable is False


def test_no_conviction_is_nothing_to_act_on() -> None:
    """The other half, with a direction the model would otherwise back."""
    result = apply_filters(
        bias(direction=Direction.LONG, conviction=Conviction.NONE),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "no_edge" in result.blockers
    assert result.tradeable is False


def test_a_backed_pair_carries_no_edge_blocker() -> None:
    """The converse, so the two above are not passing because everything blocks."""
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.01
    )

    assert "no_edge" not in result.blockers
    assert result.tradeable is True


# --- coverage ---------------------------------------------------------------


def test_the_worse_covered_leg_decides_the_coverage_blocker() -> None:
    """A pair is only as well measured as its worse-measured side.

    The quote leg is below the threshold and the base leg is comfortably above
    it, so an implementation reading the base leg sees 0.95 and one averaging
    the two sees 0.77, and both let this through.
    """
    result = apply_filters(
        bias(),
        legs(base=0.95, quote=0.60 - 0.01),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "coverage" in result.blockers
    assert result.tradeable is False


def test_coverage_exactly_at_the_threshold_is_not_blocked() -> None:
    """The comparison is ``<``, so the threshold itself is acceptable."""
    result = apply_filters(
        bias(),
        legs(base=1.0, quote=CONFIG.scoring.min_coverage),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "coverage" not in result.blockers


def test_the_coverage_threshold_is_read_from_config() -> None:
    """Not a hardcoded 0.60.

    The same pair passes under the default and blocks under a stricter config,
    so an implementation ignoring the setting fails on the second call.
    """
    strict = Config(scoring=ScoringConfig(min_coverage=0.90))
    thin = legs(base=1.0, quote=0.80)

    assert (
        "coverage"
        not in apply_filters(
            bias(), thin, CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.01
        ).blockers
    )
    assert (
        "coverage"
        in apply_filters(
            bias(), thin, strict, ASOF, calendar_guard=guard(), cost_ratio=0.01
        ).blockers
    )


def test_a_leg_with_no_coverage_at_all_says_so_separately() -> None:
    """``no_coverage`` is its own fact: there is no composite, not a thin one.

    It fires alongside ``coverage``, because zero is also below the threshold
    and both statements are true. They are not the same statement: one says the
    number rests on too little, the other says there is no number.
    """
    result = apply_filters(
        bias(),
        legs(base=1.0, quote=0.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "no_coverage" in result.blockers
    assert "coverage" in result.blockers
    assert result.tradeable is False


def test_no_coverage_is_checked_on_both_legs() -> None:
    """The base leg, so the test above is not passing on a quote-only check."""
    result = apply_filters(
        bias(),
        legs(base=0.0, quote=1.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "no_coverage" in result.blockers


def test_full_coverage_carries_neither_coverage_blocker() -> None:
    """The converse for both."""
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.01
    )

    assert "coverage" not in result.blockers
    assert "no_coverage" not in result.blockers


# --- cost -------------------------------------------------------------------


def test_a_dealing_cost_above_the_ceiling_blocks() -> None:
    """The broker taking more than a twentieth of the plausible move.

    A cost test rather than a view: the bias stands and the pair is still
    scored, it is just not worth the ticket.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=CONFIG.scoring.max_cost_ratio + 0.001,
    )

    assert "cost" in result.blockers
    assert result.tradeable is False


def test_a_dealing_cost_exactly_at_the_ceiling_does_not_block() -> None:
    """The comparison is ``>``, so the ceiling itself is acceptable."""
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=CONFIG.scoring.max_cost_ratio,
    )

    assert "cost" not in result.blockers
    assert result.tradeable is True


def test_the_cost_ceiling_is_read_from_config() -> None:
    """Not a hardcoded 0.05."""
    tolerant = Config(scoring=ScoringConfig(max_cost_ratio=0.20))

    assert (
        "cost"
        in apply_filters(
            bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.10
        ).blockers
    )
    assert (
        "cost"
        not in apply_filters(
            bias(), legs(), tolerant, ASOF, calendar_guard=guard(), cost_ratio=0.10
        ).blockers
    )


def test_no_cost_input_is_recorded_and_does_not_block() -> None:
    """An offline run has no cost input and still produces biases.

    The marker is what stops its silence reading as an all-clear. Dropping the
    filter entirely would leave this pair tradeable with nothing said, which is
    the same output a genuinely cheap pair produces.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=None
    )

    assert "cost" + UNCHECKED_SUFFIX in result.blockers
    assert "cost" not in result.blockers
    assert result.tradeable is True


# --- the calendar -----------------------------------------------------------


def test_no_guard_is_recorded_and_does_not_block() -> None:
    """Silence and an all-clear must not look the same.

    A backtest and an offline run both pass no guard. The pair stays tradeable,
    because refusing to produce biases would throw away the fundamentals, which
    are the slow half of the model and still valid.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=None, cost_ratio=0.01
    )

    assert "event" + UNCHECKED_SUFFIX in result.blockers
    assert result.tradeable is True


def test_a_quiet_calendar_records_nothing_at_all() -> None:
    """A guard that ran and found nothing is the one case with no marker.

    Which is what makes the two markers mean something: this is the only state
    that prints an unqualified clear.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.01
    )

    assert not any(entry.startswith("event") for entry in result.blockers)
    assert result.tradeable is True


def test_an_event_on_the_base_leg_blocks() -> None:
    """Inside the blackout window there is no order, full stop.

    The guard's reason is carried through rather than replaced by a flag, so a
    reader sees which release and when instead of a bare marker.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": ("EUR CPI at 09:00 UTC",)}),
        cost_ratio=0.01,
    )

    assert "event: EUR CPI at 09:00 UTC" in result.blockers
    assert result.tradeable is False


def test_an_event_on_the_quote_leg_blocks() -> None:
    """A pair has two legs.

    An FOMC evening blocks every dollar pair, not only the one the trader
    happened to be watching, and an implementation checking the base alone
    passes every other test in this file.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"USD": ("USD FOMC at 18:00 UTC",)}),
        cost_ratio=0.01,
    )

    assert "event: USD FOMC at 18:00 UTC" in result.blockers
    assert result.tradeable is False


def test_every_event_on_both_legs_is_recorded() -> None:
    """Accumulating rather than stopping at the first.

    Two releases on one evening is two reasons to stand aside, and a trader
    deciding whether to wait needs both.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(
            found={
                "EUR": ("EUR CPI at 09:00 UTC", "EUR ECB at 12:45 UTC"),
                "USD": ("USD FOMC at 18:00 UTC",),
            }
        ),
        cost_ratio=0.01,
    )

    events = [entry for entry in result.blockers if entry.startswith("event")]
    assert len(events) == 3


def test_a_guard_that_could_not_check_is_not_a_guard_that_found_nothing() -> None:
    """The distinction ADR 0002 rule 4 exists for.

    A failed calendar fetch and a quiet day are both an empty blocker list, and
    the trader's response differs: check by hand immediately, versus nothing is
    wrong. It does not block, because refusing every pair on one bad fetch
    would cost a full trading day over an outage the next run may clear.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(unknown={"EUR": "calendar fetch failed"}),
        cost_ratio=0.01,
    )

    assert "event" + UNKNOWN_SUFFIX + ": calendar fetch failed" in result.blockers
    assert result.tradeable is True


def test_an_unknown_on_the_quote_leg_is_recorded_too() -> None:
    """Both legs again, for the marker rather than for the block."""
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(unknown={"USD": "cached week ends before asof"}),
        cost_ratio=0.01,
    )

    assert (
        "event" + UNKNOWN_SUFFIX + ": cached week ends before asof" in result.blockers
    )
    assert result.tradeable is True


def test_an_unknown_leg_and_a_blocked_leg_report_both() -> None:
    """One leg checked and blocked, the other unreachable.

    The pair blocks, on the leg that answered. The other leg's marker still has
    to survive, or a reader would think the calendar was fully consulted.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(
            found={"USD": ("USD FOMC at 18:00 UTC",)},
            unknown={"EUR": "calendar fetch failed"},
        ),
        cost_ratio=0.01,
    )

    assert "event: USD FOMC at 18:00 UTC" in result.blockers
    assert "event" + UNKNOWN_SUFFIX + ": calendar fetch failed" in result.blockers
    assert result.tradeable is False


def test_the_guard_is_asked_about_both_legs_on_the_runs_own_date() -> None:
    """The date reaching the guard is the run's, not today's.

    A guard asked about the wrong day answers about the wrong day, and on a
    historical run every answer would be wrong in the same direction.
    """
    asked: list[tuple[str, date]] = []

    def recording(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        asked.append((currency, when))
        return (), None

    apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=recording, cost_ratio=0.01
    )

    assert asked == [("EUR", ASOF), ("USD", ASOF)]


# --- accumulation and the untouched view ------------------------------------


def test_three_conditions_produce_three_blockers() -> None:
    """Blockers accumulate rather than short-circuiting.

    A pair blocked for three reasons is a different thing from a pair blocked
    for one, and a trader fixing the first of three fixes nothing.
    """
    result = apply_filters(
        bias(direction=Direction.NEUTRAL, conviction=Conviction.NONE),
        legs(base=1.0, quote=0.20),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.30,
    )

    assert {"no_edge", "coverage", "cost"} <= set(result.blockers)
    assert result.tradeable is False


def test_the_model_s_view_survives_every_verdict() -> None:
    """What the engine thought is not edited to match whether it can be traded.

    A report that quietly neutralised a blocked pair would lose the record of
    what the model actually said, which is the only thing a later review can
    learn from. Asserted field by field on the most blocked case, since that is
    where a renderer would be tempted to tidy up.
    """
    before = bias(direction=Direction.SHORT, conviction=Conviction.MEDIUM)

    after = apply_filters(
        before,
        legs(base=0.0, quote=0.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": ("EUR CPI at 09:00 UTC",)}),
        cost_ratio=0.99,
    )

    assert after.direction is before.direction
    assert after.spread == before.spread
    assert after.conviction is before.conviction
    assert after.agreement == before.agreement
    assert after.pair == before.pair
    assert after.base == before.base
    assert after.quote == before.quote
    assert after.base_score == before.base_score
    assert after.quote_score == before.quote_score
    assert after.asof == before.asof


def test_the_input_is_not_mutated() -> None:
    """Frozen in, new object out.

    `PairBias` is frozen, so an implementation trying to edit in place would
    raise rather than corrupt. What this guards is the quieter version: reusing
    the input's blocker tuple and appending to a shared list.
    """
    before = bias()

    after = apply_filters(
        before,
        legs(base=0.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=None,
    )

    assert after is not before
    assert before.blockers == ()
    assert before.tradeable is True
    assert after.blockers != ()


def test_a_clean_pair_comes_back_tradeable_with_nothing_said() -> None:
    """The whole point of the markers is that this state is distinguishable.

    Everything checked, everything passed: no blockers at all and tradeable
    True. If this printed the same as an offline run, none of the rest would
    matter.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.01
    )

    assert result.blockers == ()
    assert result.tradeable is True


def test_only_the_blocking_kinds_set_tradeable_false() -> None:
    """``tradeable`` follows `BLOCKERS`, not the presence of any marker.

    Both non-blocking markers at once, which is exactly an offline run: no cost
    input and no guard. A pair carrying two blockers and still tradeable looks
    wrong until you read what they say, and that is the design.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=None, cost_ratio=None
    )

    assert set(result.blockers) == {
        "cost" + UNCHECKED_SUFFIX,
        "event" + UNCHECKED_SUFFIX,
    }
    assert all(BLOCKERS[kind] is False for kind in kinds(result))
    assert result.tradeable is True


def test_a_missing_leg_raises_rather_than_passing_the_pair() -> None:
    """A currency absent from ``scores`` is an absence of data.

    Treating it as coverage zero, or skipping the check, would file it as an
    absence of opportunity. `build_pair_biases` already raises on the same
    shape, and this function reads the same mapping.
    """
    with pytest.raises(KeyError, match="USD"):
        apply_filters(
            bias(),
            {"EUR": score("EUR", 1.0)},
            CONFIG,
            ASOF,
            calendar_guard=guard(),
            cost_ratio=0.01,
        )


def test_filtering_twice_says_the_same_thing_as_filtering_once() -> None:
    """``blockers`` is set, not appended to.

    The docstring says this function sets ``tradeable`` and ``blockers``, and
    the difference matters because every input it sees today arrives with an
    empty tuple from `build_pair_biases`. An implementation that carried the
    input's blockers through passes every other test in this file, and the
    first time anything re-filters a bias, a report shows each reason twice and
    a reader counts two events where there was one.

    Found by a mutation that survived the rest of the suite.
    """
    once = apply_filters(
        bias(),
        legs(base=0.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": ("EUR CPI at 09:00 UTC",)}),
        cost_ratio=None,
    )
    twice = apply_filters(
        once,
        legs(base=0.0),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": ("EUR CPI at 09:00 UTC",)}),
        cost_ratio=None,
    )

    assert twice.blockers == once.blockers
    assert twice.tradeable is once.tradeable


# --- inputs no comparison here can judge ------------------------------------


def test_a_guard_that_answers_both_ways_at_once_is_refused() -> None:
    """A guard cannot say it could not check and also name what it found.

    `CalendarGuard` promises the two fields are never in tension about whether
    a currency is clear, and this is the check that hard-blocks a pair. The
    first implementation trusted that promise and dropped the blockers, so an
    evening where the guard positively identified an FOMC release inside the
    window came back tradeable, carrying a note saying the calendar was
    uncertain. A row that says "not sure" reads as one that was handled.

    The guard is this codebase's own, so a breach is a defect to fix rather
    than a condition to tolerate, and it raises here where the message can name
    the leg and what was discarded.
    """

    def contradictory(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        if currency == "USD":
            return ("USD FOMC at 18:00 UTC",), "cached week ends before asof"
        return (), None

    with pytest.raises(ValueError, match="USD FOMC at 18:00 UTC"):
        apply_filters(
            bias(),
            legs(),
            CONFIG,
            ASOF,
            calendar_guard=contradictory,
            cost_ratio=0.01,
        )


@pytest.mark.parametrize("unusable", [float("nan"), float("inf"), -5.0])
def test_a_cost_ratio_no_threshold_can_judge_is_refused(unusable: float) -> None:
    """Every filter here is a ``<`` or a ``>``, and NaN loses all of them.

    An unusable cost ratio passed every comparison and the pair came back with
    ``tradeable`` True and an empty ``blockers``, which is the one state this
    function reserves for "every check ran and every check passed". A NaN cost
    ratio printed exactly like a pair verified as cheap.

    ``None`` and only ``None`` says a value was not supplied. A negative is
    included because a dealing cost cannot be one, and whoever writes the
    producer has both spellings available.
    """
    with pytest.raises(ValueError, match="cost_ratio"):
        apply_filters(
            bias(),
            legs(),
            CONFIG,
            ASOF,
            calendar_guard=guard(),
            cost_ratio=unusable,
        )


def test_a_coverage_no_threshold_can_judge_is_refused() -> None:
    """The same hole on the other input, and it is reachable.

    `Config.validate` rejects a negative weight with ``weight < 0.0``, which is
    False for NaN, so a NaN weight passes validation and reaches `coverage` as
    a sum. Fixing the cost ratio and leaving this would close half a hole.
    """
    with pytest.raises(ValueError, match="coverage"):
        apply_filters(
            bias(),
            legs(base=float("nan")),
            CONFIG,
            ASOF,
            calendar_guard=guard(),
            cost_ratio=0.01,
        )


def test_a_cost_ratio_of_zero_is_a_reading_and_not_an_absence() -> None:
    """Zero is free, not unsupplied, and the two must not collapse.

    ``if not cost_ratio`` would read 0.0 as missing and print
    ``cost:unchecked`` on a pair whose cost was measured and found to be
    nothing.
    """
    result = apply_filters(
        bias(), legs(), CONFIG, ASOF, calendar_guard=guard(), cost_ratio=0.0
    )

    assert result.blockers == ()
    assert result.tradeable is True
