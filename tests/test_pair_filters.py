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
    pair: str = "EURUSD",
    base: str = "EUR",
    quote: str = "USD",
    asof: date = ASOF,
) -> PairBias:
    """An unfiltered EURUSD bias, as `build_pair_biases` leaves it.

    ``tradeable`` is True and ``blockers`` empty on the way in, which is the
    shape that function produces: it sets neither, deliberately.
    """
    return PairBias(
        pair=pair,
        base=base,
        quote=quote,
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=asof,
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

    The date is ignored here rather than asserted. An assertion inside a double
    reads as though it pins the date wire and does not: every date in this file
    was `ASOF`, so passing the bias's own ``asof`` instead of the run's
    satisfied it. `test_the_guard_is_asked_about_the_runs_date_not_the_bias_s`
    pins that properly, on a bias stamped with a different date.
    """
    found = found or {}
    unknown = unknown or {}

    def check(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        if currency in unknown:
            return (), unknown[currency]
        return tuple(found.get(currency, ())), None

    return check


PAYLOAD_KINDS = ("event" + UNKNOWN_SUFFIX, "event")
"""The two kinds emitted as ``"<kind>: <reason>"`` rather than verbatim.

Longest first, because ``"event"`` prefixes ``"event:unknown"`` and taking the
shorter one first would file every unknown as a block."""


def kind_of(entry: str) -> str:
    """Return the `BLOCKERS` kind an emitted string belongs to, or fail.

    Exact membership for the six verbatim kinds, and for the other two an exact
    ``"<kind>: "`` prefix with a non-empty reason after it.

    Deliberately stricter than "shares a prefix with some key", which is what
    this helper first did. Under that rule ``"event:unknownish"`` mapped to
    ``event:unknown`` and passed, so a string no renderer knows and no document
    describes could be emitted without a test noticing.
    """
    if entry in BLOCKERS:
        return entry
    for kind in PAYLOAD_KINDS:
        prefix = kind + ": "
        if entry.startswith(prefix) and entry[len(prefix) :].strip():
            return kind
    raise AssertionError(f"{entry!r} is not a kind BLOCKERS declares")


def kinds(result: PairBias) -> list[str]:
    """Every emitted blocker mapped back to its kind, in emission order."""
    return [kind_of(entry) for entry in result.blockers]


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
    # 0.75 rather than 0.90: `Config.validate` requires
    # ``min_coverage <= coverage_demotion`` and the latter is 0.80, so a
    # stricter figure would be a config that could not be loaded. Nothing here
    # validates, so the test would have passed on an impossible setting.
    strict = Config(scoring=ScoringConfig(min_coverage=0.75))
    thin = legs(base=1.0, quote=0.70)

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

    # Equality rather than a subset, and sorted so the emission order is not
    # pinned here: a subset assertion cannot see a reason emitted twice, which
    # would print the same line twice on a dashboard.
    assert sorted(result.blockers) == ["cost", "coverage", "no_edge"]
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


# --- the branches one fixture could not reach -------------------------------


SCENARIOS: tuple[tuple[str, dict[str, object]], ...] = (
    ("clean", {}),
    ("neutral direction", {"view": {"direction": Direction.NEUTRAL}}),
    ("no conviction", {"view": {"conviction": Conviction.NONE}}),
    ("thin coverage", {"coverage": (1.0, 0.20)}),
    ("no coverage", {"coverage": (0.0, 1.0)}),
    ("costly", {"cost": 0.40}),
    ("no cost input", {"cost": None}),
    ("no guard", {"no_guard": True}),
    ("event on base", {"found": {"EUR": ("EUR CPI at 09:00 UTC",)}}),
    ("event on quote", {"found": {"USD": ("USD FOMC at 18:00 UTC",)}}),
    ("unknown base", {"unknown": {"EUR": "calendar fetch failed"}}),
    ("unknown both", {"unknown": {"EUR": "fetch failed", "USD": "fetch failed"}}),
    (
        "everything at once",
        {
            "view": {"direction": Direction.NEUTRAL, "conviction": Conviction.NONE},
            "coverage": (0.0, 0.10),
            "cost": 0.99,
            "found": {"EUR": ("EUR CPI at 09:00 UTC",)},
        },
    ),
)
"""One entry per branch of the function, which one fixture cannot reach.

The tests that use this were each written against a single scenario first, and
each of them missed mutations in the branches that scenario did not enter: a
``no_edge`` path that zeroed the spread, a guard-is-None path that capped the
conviction. A table is the only way to say "in every case" and mean it."""


def apply_scenario(setup: dict[str, object]) -> tuple[PairBias, PairBias]:
    """Run one scenario, returning the bias before and after."""
    view = dict(setup.get("view") or {})
    coverage = setup.get("coverage") or (1.0, 1.0)
    before = bias(**view)  # type: ignore[arg-type]
    guard_arg = (
        None
        if setup.get("no_guard")
        else guard(
            found=setup.get("found"),  # type: ignore[arg-type]
            unknown=setup.get("unknown"),  # type: ignore[arg-type]
        )
    )
    after = apply_filters(
        before,
        legs(*coverage),  # type: ignore[misc]
        CONFIG,
        ASOF,
        calendar_guard=guard_arg,
        cost_ratio=setup.get("cost", 0.01),  # type: ignore[arg-type]
    )
    return before, after


@pytest.mark.parametrize("label,setup", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_the_view_survives_in_every_branch(
    label: str, setup: dict[str, object]
) -> None:
    """Criterion 10 says "in every case", and one case does not establish that.

    Four wrong implementations survived the single-scenario version of this
    test, each editing the view inside a branch that scenario never entered:
    the ``no_edge`` path zeroing the spread, the same path zeroing the
    agreement, and the guard-is-None path zeroing the agreement or capping the
    conviction. Every one of them is the "tidy up the blocked row" defect this
    assertion exists to prevent, hiding in a branch.
    """
    before, after = apply_scenario(setup)

    assert after.direction is before.direction
    assert after.spread == before.spread
    assert after.conviction is before.conviction
    assert after.agreement == before.agreement
    assert after.base_score == before.base_score
    assert after.quote_score == before.quote_score
    assert after.pair == before.pair
    assert after.asof == before.asof


@pytest.mark.parametrize("label,setup", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_every_emitted_string_is_a_declared_kind_in_every_branch(
    label: str, setup: dict[str, object]
) -> None:
    """Criterion 1, over every branch rather than one scenario.

    A string that is not a kind `BLOCKERS` declares is a blocker no document
    describes and no renderer knows to show. `kind_of` refuses anything that is
    not an exact member or an exact ``"<kind>: <reason>"``, which is what makes
    this bite: the looser "shares a prefix" rule accepted
    ``"event:unknownish"``.

    ``tradeable`` is recomputed from the enumeration rather than asserted as a
    literal, so the verdict and the reasons cannot drift apart.
    """
    _, after = apply_scenario(setup)

    emitted = kinds(after)
    assert len(emitted) == len(after.blockers)
    assert after.tradeable is not any(BLOCKERS[kind] for kind in emitted)


# --- order, which a reader depends on ---------------------------------------


def test_the_base_leg_s_events_are_listed_before_the_quote_leg_s() -> None:
    """Documented order, and a reader uses it.

    The guard orders one leg's releases by time and a trader reads the list top
    to bottom to decide how long to wait. Reversing the entries, or sorting
    them by severity so unknown legs come first, both survive a test that only
    asserts membership.

    The base leg blocks and the quote leg is unknown, on purpose: with the
    unknown on the base leg, leg order and severity order coincide and a
    severity sort is indistinguishable from the documented one.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(
            found={"EUR": ("EUR CPI at 09:00 UTC", "EUR ECB at 12:45 UTC")},
            unknown={"USD": "calendar fetch failed"},
        ),
        cost_ratio=0.01,
    )

    assert result.blockers == (
        "event: EUR CPI at 09:00 UTC",
        "event: EUR ECB at 12:45 UTC",
        "event" + UNKNOWN_SUFFIX + ": calendar fetch failed",
    )


def test_both_legs_unknown_are_recorded_separately() -> None:
    """Two legs the guard could not reach is two facts, not one.

    Deduplicating the marker across legs survives every other test here,
    because no other fixture has both legs unknown, and it would tell a reader
    that one currency's calendar was checked when neither was.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(
            unknown={"EUR": "EUR fetch failed", "USD": "USD fetch failed"}
        ),
        cost_ratio=0.01,
    )

    marker = "event" + UNKNOWN_SUFFIX
    assert result.blockers == (
        f"{marker}: EUR fetch failed",
        f"{marker}: USD fetch failed",
    )
    assert result.tradeable is True


# --- the other twenty-seven pairs -------------------------------------------


def test_the_filters_read_the_pair_s_own_legs() -> None:
    """Every other fixture here is EURUSD, and there are 28 pairs.

    Hardcoding either the score lookups or the calendar loop to EUR and USD
    passes every one of them while being wrong on the other 27. That is the
    pair-convention rule applied to the filters rather than to a pair string.
    """
    asked: list[str] = []

    def recording(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        asked.append(currency)
        return (("AUD RBA at 04:30 UTC",), None) if currency == "AUD" else ((), None)

    result = apply_filters(
        bias(pair="AUDJPY", base="AUD", quote="JPY"),
        {"AUD": score("AUD", 1.0), "JPY": score("JPY", 0.10)},
        CONFIG,
        ASOF,
        calendar_guard=recording,
        cost_ratio=0.01,
    )

    assert asked == ["AUD", "JPY"]
    assert "event: AUD RBA at 04:30 UTC" in result.blockers
    assert "coverage" in result.blockers
    assert result.tradeable is False


def test_the_guard_is_asked_about_the_runs_date_not_the_bias_s() -> None:
    """The run's ``asof`` reaches the guard, not the date stamped on the bias.

    Every fixture in this file used one date for the run, the bias and the
    scores, so passing ``bias.asof`` instead satisfied the assertion inside the
    double and looked tested. The two are separated here, which is the only
    thing that tells them apart.
    """
    stale = date(2020, 1, 2)
    asked: list[tuple[str, date]] = []

    def recording(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        asked.append((currency, when))
        return (), None

    apply_filters(
        bias(asof=stale),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=recording,
        cost_ratio=0.01,
    )

    assert asked == [("EUR", ASOF), ("USD", ASOF)]


# --- boundaries and refusals nothing else reaches ---------------------------


def test_a_conviction_of_low_is_still_something_to_act_on() -> None:
    """``no_edge`` is NEUTRAL or NONE, and LOW is neither.

    Widening it to include LOW is a plausible misreading of the spec and
    survives every other test here, because nothing else passes LOW. It is also
    the most destructive of the mutations found: LOW is exactly what
    `conviction_for` produces after the 24-hour event cap, so this would
    silently delete every capped pair and print a quiet board.
    """
    result = apply_filters(
        bias(conviction=Conviction.LOW),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert result.blockers == ()
    assert result.tradeable is True


def test_a_thin_but_real_coverage_is_not_reported_as_no_coverage() -> None:
    """``no_coverage`` means there is no composite, not a bad one.

    Every other fixture uses exactly 0.0 or a comfortably positive figure, so
    widening the test to anything under 5% survives, and a leg holding 4% of
    pillar weight would be reported as having no composite when it has a poor
    one. The two markers call for different responses.
    """
    result = apply_filters(
        bias(),
        legs(base=1.0, quote=0.04),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "coverage" in result.blockers
    assert "no_coverage" not in result.blockers


def test_a_negative_coverage_reads_as_no_data() -> None:
    """The reason the comparison is ``<=`` and not ``==``.

    Coverage is a sum of non-negative weights, so a negative is unreachable
    today and would be a defect upstream. It reads as no data rather than as
    data, which is the safe direction, and the behaviour was held in place by a
    code comment alone.
    """
    result = apply_filters(
        bias(),
        legs(base=-0.30),
        CONFIG,
        ASOF,
        calendar_guard=guard(),
        cost_ratio=0.01,
    )

    assert "no_coverage" in result.blockers
    assert result.tradeable is False


def test_a_guard_that_raises_is_not_turned_into_a_verdict() -> None:
    """A guard blowing up is a defect in the guard, not a quiet day.

    The protocol already has ``unknown_reason`` for "could not check", so an
    exception means something the guard did not anticipate. Catching it and
    returning a clear day, or an unknown marker, manufactures a verdict nobody
    computed; both survive every other test here.
    """

    def exploding(currency: str, when: date) -> tuple[Sequence[str], str | None]:
        raise RuntimeError("calendar backend is down")

    with pytest.raises(RuntimeError, match="calendar backend is down"):
        apply_filters(
            bias(), legs(), CONFIG, ASOF, calendar_guard=exploding, cost_ratio=0.01
        )


# --- what the emitted sequence itself says ----------------------------------


def test_the_blockers_read_in_the_order_the_checks_ran() -> None:
    """Emission order, pinned on a fixture that sorting would reorder.

    `test_the_base_leg_s_events_are_listed_before_the_quote_leg_s` pins leg
    order, but its reasons happen to be in alphabetical order already, so
    sorting the entries before emitting them produces the same tuple and
    passes. This fixture puts the base leg's later release first and names it
    so that alphabetical order and emission order disagree in two places at
    once: within the base leg, and across the two legs.
    """
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(
            found={
                "EUR": ("EUR ZEW at 09:00 UTC", "EUR CPI at 12:45 UTC"),
                "USD": ("USD ADP at 12:15 UTC",),
            }
        ),
        cost_ratio=0.01,
    )

    assert result.blockers == (
        "event: EUR ZEW at 09:00 UTC",
        "event: EUR CPI at 12:45 UTC",
        "event: USD ADP at 12:15 UTC",
    )
    assert result.blockers != tuple(sorted(result.blockers))


def test_one_release_naming_both_legs_is_recorded_on_both() -> None:
    """Two identical strings, because they are two separate facts.

    A release the guard attaches to both currencies, a joint statement or a
    figure that moves the pair from each side, produces the same reason text
    twice. Collapsing the two into one entry, which any deduplication of
    ``blockers`` does, would tell a reader only one leg was affected and leave
    the other looking clear.
    """
    reason = "G20 communique at 14:00 UTC"
    result = apply_filters(
        bias(),
        legs(),
        CONFIG,
        ASOF,
        calendar_guard=guard(found={"EUR": (reason,), "USD": (reason,)}),
        cost_ratio=0.01,
    )

    assert result.blockers == (f"event: {reason}", f"event: {reason}")
    assert result.tradeable is False


def test_the_coverage_refusal_names_the_leg_that_is_unusable() -> None:
    """The message is the whole value of the refusal.

    Every check in this function is symmetric across the legs, so reading the
    two scores in the wrong order changes nothing a verdict test can see. It
    changes this message, which is what someone gets handed when the run stops,
    and a message naming the healthy leg sends them to the wrong data source.
    """
    with pytest.raises(ValueError) as base_leg:
        apply_filters(
            bias(),
            legs(base=float("nan")),
            CONFIG,
            ASOF,
            calendar_guard=guard(),
            cost_ratio=0.01,
        )
    assert "EUR" in str(base_leg.value)
    assert "USD" not in str(base_leg.value)

    with pytest.raises(ValueError) as quote_leg:
        apply_filters(
            bias(),
            legs(quote=float("nan")),
            CONFIG,
            ASOF,
            calendar_guard=guard(),
            cost_ratio=0.01,
        )
    assert "USD" in str(quote_leg.value)
    assert "EUR" not in str(quote_leg.value)
