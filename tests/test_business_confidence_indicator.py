"""`business_confidence_mfg`, and the separation from `pmi_composite`.

The registry carries two survey-based growth indicators that a reader could
easily take for one thing. They are not one thing, and the difference is in the
unit rather than in the name:

* `pmi_composite` is a diffusion index. Its neutral line is 50.
* `business_confidence_mfg` is a percentage balance, net positive respondents
  minus net negative. Its neutral line is 0.

Score one as the other and every currency in the universe moves in the same
direction at once, so the cross-sectional z-score absorbs the offset and the
ranking still looks orderly. Nothing downstream would report an error. That is
the failure this module exists to make impossible to reach by accident, and it
is why the two are separate keys rather than one key with two sources behind it.

The second thing asserted here is that nothing consumes the new series yet.
Whether GROWTH uses it, and at what sub-weight, is the scoring layer's decision
and was explicitly excluded from the work that added it. `UNCONSUMED_INDICATORS`
is where that state is declared, and the tests in
``tests/test_registry_pillar_agreement.py`` hold both directions of it.

No test here reaches the network. Every assertion reads the registry.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fbe.datasources.registry import (
    INDICATORS,
    SOURCE_MANUAL,
    SOURCE_OECD,
    UNCONSUMED_INDICATORS,
    VERIFIED_ON,
    IndicatorSpec,
)
from fbe.types import Frequency, PillarName
from fbe.universe import G10

KEY = "business_confidence_mfg"
PMI_KEY = "pmi_composite"

MONTHLY_CURRENCIES = frozenset({"USD", "EUR", "GBP", "CHF"})
"""The four whose national business tendency survey runs monthly."""

QUARTERLY_CURRENCIES = frozenset({"JPY", "CAD", "AUD", "NZD"})
"""The four whose survey runs quarterly: the Tankan, and the Canadian,
Australian and New Zealand business outlooks the OECD republishes."""


@pytest.fixture
def spec() -> IndicatorSpec:
    return INDICATORS[KEY]


# ---------------------------------------------------------------------------
# The indicator exists, on its own terms
# ---------------------------------------------------------------------------


def test_the_indicator_is_attributed_to_growth(spec: IndicatorSpec) -> None:
    """The attribution is what `UNCONSUMED_INDICATORS` is checked against.

    Not asserting ``spec.key == KEY`` alongside it: `INDICATORS` is built as a
    comprehension keyed on ``spec.key``, so that holds by construction and
    cannot fail.
    """
    assert spec.pillar is PillarName.GROWTH


def test_every_g10_currency_has_a_leg(spec: IndicatorSpec) -> None:
    """Eight for eight is the whole point: the licensed PMI is zero for eight."""
    assert set(spec.series) == set(G10)


def test_every_leg_is_fetchable_rather_than_manual(spec: IndicatorSpec) -> None:
    """A manual leg here would defeat the reason for adding the series at all."""
    for currency, ref in spec.series.items():
        assert ref.source == SOURCE_OECD, currency
        assert ref.fetchable, currency


# ---------------------------------------------------------------------------
# The unit, which is the failure mode
# ---------------------------------------------------------------------------


def test_the_unit_is_a_percentage_balance_not_an_index(spec: IndicatorSpec) -> None:
    assert spec.unit == "percentage_balance"
    for currency, ref in spec.series.items():
        assert ref.unit == "percentage_balance", currency


def test_the_unit_differs_from_the_pmi_unit(spec: IndicatorSpec) -> None:
    """The assertion that stops the two being blended under one key.

    Written as a comparison rather than as two literals so that renaming either
    unit cannot leave this passing while the two have silently converged.
    """
    assert spec.unit != INDICATORS[PMI_KEY].unit


def test_the_transform_is_level_because_a_balance_is_already_a_net(
    spec: IndicatorSpec,
) -> None:
    """A percentage balance is a level, not something to difference again."""
    for currency, ref in spec.series.items():
        assert ref.transform == "level", currency


# ---------------------------------------------------------------------------
# pmi_composite is untouched
# ---------------------------------------------------------------------------


def test_pmi_composite_is_still_registered_and_still_manual() -> None:
    """Adding a free proxy does not retire the licensed series it proxies for.

    The two measure different things and the proposal that added
    `business_confidence_mfg` said so. If a later change routes the PMI key to
    the OECD, this fails, which is the intended outcome.
    """
    pmi = INDICATORS[PMI_KEY]
    assert pmi.unit == "index"
    assert set(pmi.series) == set(G10)
    for currency, ref in pmi.series.items():
        assert ref.source == SOURCE_MANUAL, currency


# ---------------------------------------------------------------------------
# Frequency is per leg, because the surveys genuinely differ
# ---------------------------------------------------------------------------


def test_each_leg_carries_its_own_true_frequency(spec: IndicatorSpec) -> None:
    """`SeriesRef.frequency` is authoritative and the spec's is only typical.

    Four of these countries survey monthly and four quarterly. A single
    frequency on the spec would be wrong for half the universe, and the period
    parser reads the key rather than the spec for exactly this reason.
    """
    for currency in MONTHLY_CURRENCIES:
        assert spec.series[currency].frequency is Frequency.MONTHLY, currency
    for currency in QUARTERLY_CURRENCIES:
        assert spec.series[currency].frequency is Frequency.QUARTERLY, currency


def test_the_two_frequency_groups_cover_the_universe_exactly() -> None:
    """Guards the test above from going quietly blind if a currency is dropped."""
    assert set(G10) == MONTHLY_CURRENCIES | QUARTERLY_CURRENCIES
    assert not MONTHLY_CURRENCIES & QUARTERLY_CURRENCIES


# ---------------------------------------------------------------------------
# The staleness allowance, derived rather than chosen
# ---------------------------------------------------------------------------


MAX_PUBLICATION_LAG_DAYS = (date(2026, 9, 9) - date(2026, 6, 30)).days
"""Upper bound on how long after a quarter ends the survey publishes, in days.

Bounded by observation, not known exactly. The 2026-Q2 print (quarter ending
2026-06-30) was still the newest one on `VERIFIED_ON`, which puts the lag at no
more than this. One observation cannot narrow it, so the bound is used as though
it were the lag, which errs towards a longer allowance and is the direction that
risks hiding a gap. That is why the other side is asserted too.
"""


def _next_quarter(start: date) -> date:
    """First day of the quarter after the one beginning on ``start``."""
    return date(start.year + (start.month + 3 > 12), (start.month + 2) % 12 + 1, 1)


def _worst_fresh_age(stamp: date) -> int:
    """Age of a first-day-stamped quarterly print on the day before it is replaced.

    A print stays the newest one until its successor publishes, which is one
    further quarter end plus `MAX_PUBLICATION_LAG_DAYS`. Computed on the real
    calendar rather than from nominal 90-day quarters, because that is the
    difference this test exists to catch: quarters run 90 to 92 days, so the
    nominal arithmetic understates the answer by up to three days and leaves a
    current series reading stale for the last days of its cycle.
    """
    successor_ends = _next_quarter(_next_quarter(stamp)) - timedelta(days=1)
    successor_publishes = successor_ends + timedelta(days=MAX_PUBLICATION_LAG_DAYS)
    return (successor_publishes - timedelta(days=1) - stamp).days


QUARTER_STARTS = (
    date(2026, 1, 1),
    date(2026, 4, 1),
    date(2026, 7, 1),
    date(2026, 10, 1),
)
"""The four stamp positions. The worst case is not the same in each, which is
the whole reason this is computed rather than written down once."""


def test_the_allowance_covers_a_punctual_print_in_every_quarter(
    spec: IndicatorSpec,
) -> None:
    """No day on which an entirely current series reads stale.

    This is the assertion the earlier version of this file did not make. It
    pinned only a range, and every value from 161 to 250 satisfied it, so the
    allowance could be edited to any of them and the suite stayed green. The
    number it is supposed to pin is a derivation, so derive it here and compare.
    """
    worst = max(_worst_fresh_age(stamp) for stamp in QUARTER_STARTS)
    assert worst == 253
    assert spec.max_staleness_days >= worst


def test_the_allowance_still_expires_a_leg_that_missed_a_release(
    spec: IndicatorSpec,
) -> None:
    """The other side, which is the side that lets a lie through.

    An allowance wide enough that a series which missed a whole release still
    counts converts a visible gap into an invisible one, and
    `IndicatorSpec.max_staleness_days` names that as the single easiest way to
    make the registry lie.
    """
    worst = max(_worst_fresh_age(stamp) for stamp in QUARTER_STARTS)
    assert spec.max_staleness_days < worst + 90


def test_the_allowance_is_the_value_the_description_derives(
    spec: IndicatorSpec,
) -> None:
    """Pinned outright, in the style of `tests/test_manual.py`'s PMI assertion.

    The two tests above leave a window, and 270 is one of several values in it.
    The registry's description commits to a specific number and to a reason for
    it, and both published coverage tables in ``docs/data-sources.md`` report
    8/8 as a consequence. A literal here is what makes an edit to that number a
    deliberate act rather than a silent one.

    270 is also this table's own published figure for a quarterly series stamped
    on its period's first day, and what ``gdp_yoy`` uses.
    """
    assert spec.max_staleness_days == 270
    assert spec.max_staleness_days == INDICATORS["gdp_yoy"].max_staleness_days


def test_no_leg_is_already_stale_on_the_day_it_was_verified(
    spec: IndicatorSpec,
) -> None:
    """The measured case, as opposed to the derived worst case above.

    The quarterly legs were 161 days old on `VERIFIED_ON` while entirely
    current. An allowance sized from the monthly half would fail all four on day
    one, and both published coverage tables would then report 4/8 for a series
    that is fully covered.
    """
    stale = [
        currency
        for currency, ref in spec.series.items()
        if ref.stale_on(VERIFIED_ON, spec.max_staleness_days)
    ]
    assert not stale
    assert (VERIFIED_ON - date(2026, 4, 1)).days == 161


def test_last_observed_is_stamped_on_the_first_day_of_its_period(
    spec: IndicatorSpec,
) -> None:
    """The convention ruled on issue #27, applied to both cadences."""
    for currency, ref in spec.series.items():
        observed = ref.last_observed
        assert observed is not None, currency
        assert observed.day == 1, currency
        if currency in QUARTERLY_CURRENCIES:
            assert observed.month in (1, 4, 7, 10), currency


def test_the_verified_observations_match_what_the_api_returned(
    spec: IndicatorSpec,
) -> None:
    """Pins the fetch the registry entry was built from.

    These are the newest observations the OECD held for each leg on
    `VERIFIED_ON`. They are here so that a later edit to `last_observed` has to
    be a deliberate re-verification rather than a number nudged to make a
    freshness test pass.
    """
    expected = {currency: date(2026, 8, 1) for currency in MONTHLY_CURRENCIES}
    expected.update({currency: date(2026, 4, 1) for currency in QUARTERLY_CURRENCIES})
    assert {c: ref.last_observed for c, ref in spec.series.items()} == expected


# ---------------------------------------------------------------------------
# Nothing consumes it yet, and that is a declared state
# ---------------------------------------------------------------------------


def test_the_indicator_is_declared_unconsumed() -> None:
    """Whether GROWTH uses this is the scoring layer's call, not the data layer's.

    The proposal that added the series said the sub-weight question "should not
    be taken as approved along with it". Until that decision is taken and
    recorded, the honest state is registered and consumed by nothing, and this
    set is where that is said out loud.
    """
    assert KEY in UNCONSUMED_INDICATORS
