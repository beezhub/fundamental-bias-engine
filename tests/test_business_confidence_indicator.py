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
def spec():
    return INDICATORS[KEY]


# ---------------------------------------------------------------------------
# The indicator exists, on its own terms
# ---------------------------------------------------------------------------


def test_the_indicator_is_registered(spec) -> None:
    assert spec.key == KEY
    assert spec.pillar is PillarName.GROWTH


def test_every_g10_currency_has_a_leg(spec) -> None:
    """Eight for eight is the whole point: the licensed PMI is zero for eight."""
    assert set(spec.series) == set(G10)


def test_every_leg_is_fetchable_rather_than_manual(spec) -> None:
    """A manual leg here would defeat the reason for adding the series at all."""
    for currency, ref in spec.series.items():
        assert ref.source == SOURCE_OECD, currency
        assert ref.fetchable, currency


# ---------------------------------------------------------------------------
# The unit, which is the failure mode
# ---------------------------------------------------------------------------


def test_the_unit_is_a_percentage_balance_not_an_index(spec) -> None:
    assert spec.unit == "percentage_balance"
    for currency, ref in spec.series.items():
        assert ref.unit == "percentage_balance", currency


def test_the_unit_differs_from_the_pmi_unit(spec) -> None:
    """The assertion that stops the two being blended under one key.

    Written as a comparison rather than as two literals so that renaming either
    unit cannot leave this passing while the two have silently converged.
    """
    assert spec.unit != INDICATORS[PMI_KEY].unit


def test_no_leg_claims_the_diffusion_index_unit(spec) -> None:
    """A single repointed ref is enough to misscore the whole cross-section."""
    assert not [c for c, ref in spec.series.items() if ref.unit == "index"]


def test_the_transform_is_level_because_a_balance_is_already_a_net(spec) -> None:
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


def test_the_two_indicators_are_separate_entries() -> None:
    assert KEY in INDICATORS
    assert PMI_KEY in INDICATORS
    assert INDICATORS[KEY] is not INDICATORS[PMI_KEY]


# ---------------------------------------------------------------------------
# Frequency is per leg, because the surveys genuinely differ
# ---------------------------------------------------------------------------


def test_each_leg_carries_its_own_true_frequency(spec) -> None:
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


def test_no_leg_is_already_stale_on_the_day_it_was_verified(spec) -> None:
    """An allowance below the quarterly cadence would report live data as dead.

    The quarterly legs are first-day stamped, so on `VERIFIED_ON` the newest
    print is around 160 days old while being entirely current. An allowance set
    from the monthly half would fail every quarterly currency on day one.
    """
    stale = [
        currency
        for currency, ref in spec.series.items()
        if ref.stale_on(VERIFIED_ON, spec.max_staleness_days)
    ]
    assert not stale


def test_a_leg_frozen_for_one_extra_period_does_go_stale(spec) -> None:
    """The other side of the allowance, which is the side that lets a lie through.

    An allowance wide enough that a series which missed a whole release still
    counts converts a visible gap into an invisible one. One further quarter
    past the quarterly legs' own cadence must expire them.
    """
    later = VERIFIED_ON + timedelta(days=90)
    fresh = [
        currency
        for currency in QUARTERLY_CURRENCIES
        if not spec.series[currency].stale_on(later, spec.max_staleness_days)
    ]
    assert not fresh


def test_last_observed_is_stamped_on_the_first_day_of_its_period(spec) -> None:
    """The convention ruled on issue #27, applied to both cadences."""
    for currency, ref in spec.series.items():
        observed = ref.last_observed
        assert observed is not None, currency
        assert observed.day == 1, currency
        if currency in QUARTERLY_CURRENCIES:
            assert observed.month in (1, 4, 7, 10), currency


def test_the_verified_observations_match_what_the_api_returned(spec) -> None:
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
