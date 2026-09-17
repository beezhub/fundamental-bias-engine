"""`stale_refs`, and the indicators it used to skip.

`stale_refs` documents itself as "the complement of `coverage_report`, and the
more actionable of the two: this is the list an operator works through, and the
list a report shows so a reader can see which legs of a score were extrapolated."
It then skipped every indicator holding a ``GLOBAL`` ref outright, so those two
claims were both false and the docstring's "an empty mapping means the registry
is entirely healthy" was false with them.

Three indicators are affected and each is the sole input to a pillar component:
``world_equity_index`` and ``vol_index`` for RISK on a 7-day allowance, and
``commodity_price`` for EXTERNAL on 90 days. The 7 and the 90 are far enough
apart that an off-by-one on one boundary cannot hide behind the other, which is
why the tests below sweep both. ``world_equity_index`` carries the same 7 days
and the same ``last_observed`` as ``vol_index``, so it lands on a boundary those
sweeps already cover and needs no case of its own.

A ``GLOBAL`` ref covers the whole universe by construction, one print
serving all eight currencies, so the gap is never one leg of one score. It is a
pillar component going dark for every currency at once, and it was the one class
of gap that could not reach the list an operator is told to work through.

The report side is the sharper cost, and it is this repository's recurring shape
of defect: a reader looking at a run where RISK scored all eight currencies off a
nine-day-old volatility print would have seen no marker of any kind, and the
composite would have looked exactly as it does on a healthy run.

Two kinds of assertion live here. The boundary cases pin the specific dates from
issue #49. The complement property is checked by sweeping every indicator across
a range of dates, because the defect was not that one indicator was wrong, it was
that a whole branch of the function never ran.

No test here reaches the network. Every assertion reads the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from fbe.datasources.registry import (
    GLOBAL,
    INDICATORS,
    SOURCE_MANUAL,
    VERIFIED_ON,
    IndicatorSpec,
    SeriesRef,
    coverage_report,
    stale_refs,
)
from fbe.types import Frequency, PillarName
from fbe.universe import G10

GLOBAL_REF_INDICATORS = tuple(
    key for key, spec in INDICATORS.items() if GLOBAL in spec.series
)
"""Read from the registry rather than typed, so a third one cannot appear unseen.

Issue #49 named these as ``vix`` and ``commodity_index``. Commit ``ffbe91b``
renamed the keys after it was filed. Deriving the list means this module tracks
the rename rather than asserting against a name that no longer exists.
"""


def test_the_registry_still_holds_its_global_indicators() -> None:
    """Guards every other test here from going quietly blind.

    A sweep over a derived list passes vacuously if the list empties, so the
    premise is asserted rather than assumed. Spelled out rather than counted,
    so a key gained or lost has to be acknowledged here: ``world_equity_index``
    joined when the risk pillar's global equity reading was given its own key,
    separate from the eight per-currency benchmarks under ``equity_index``.
    """
    assert set(GLOBAL_REF_INDICATORS) == {
        "vol_index",
        "commodity_price",
        "world_equity_index",
    }


# --- the boundary, which is criterion 2 ------------------------------------


def test_the_volatility_index_reaches_the_worklist_the_day_it_expires() -> None:
    """Criterion 2, on the 7-day allowance and the 2026-09-08 observation.

    The ref is stamped 2026-09-08 and the allowance is 7 days, so 2026-09-15 is
    the last day it is usable and 2026-09-16 is the first day it is not. Both
    sides are asserted: a fix that reported every ``GLOBAL`` indicator
    unconditionally would satisfy the first assertion and fail the second.
    """
    assert "vol_index" not in stale_refs(date(2026, 9, 15))
    assert "vol_index" in stale_refs(date(2026, 9, 16))


def test_the_commodity_index_reaches_the_worklist_the_day_it_expires() -> None:
    """The same boundary on the other allowance, so the fix is not date-specific.

    90 days from 2026-07-01 puts the last usable day at 2026-09-29. Worth
    asserting separately because 7 and 90 days are far enough apart that an
    off-by-one in the comparison would show on one and not the other.
    """
    assert "commodity_price" not in stale_refs(date(2026, 9, 29))
    assert "commodity_price" in stale_refs(date(2026, 9, 30))


def test_a_global_indicator_is_reported_against_the_whole_universe() -> None:
    """The return shape, which is what makes the entry actionable.

    The mapping is indicator to currencies. A ``GLOBAL`` ref covers all eight by
    construction, per `coverage_report`'s own rule that one print serves the
    universe, so the honest answer when it dies is that all eight lost it rather
    than that some unnamed subset did.

    The order matters too: the per-currency branch builds its tuple by iterating
    ``G10``, and a reader comparing two rows of a report should not see the same
    set of currencies in two orders.
    """
    assert stale_refs(date(2026, 9, 16))["vol_index"] == tuple(G10)


# --- the unfetchable half of criterion 1 -----------------------------------
#
# All three registered GLOBAL refs are fetchable today, so the branch that
# catches an unfetchable one is never exercised by the live registry. Deleting the
# ``fetchable`` check from the fix passes every other test in this module, which
# is how this gap was found. The spec below is synthetic for exactly that reason.


def _global_spec(ref: SeriesRef) -> IndicatorSpec:
    """An indicator whose whole coverage is one ``GLOBAL`` ref."""
    return IndicatorSpec(
        key="synthetic_global",
        pillar=PillarName.RISK,
        unit="index",
        frequency=Frequency.DAILY,
        max_staleness_days=7,
        description="Test fixture. Never registered.",
        series={GLOBAL: ref},
    )


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> Callable[[SeriesRef], None]:
    """Add one synthetic indicator to the registry for the length of a test.

    ``monkeypatch.setitem`` rather than a module-level edit, so a failure here
    cannot leak a fake indicator into every test that runs afterwards.
    """

    def _register(ref: SeriesRef) -> None:
        monkeypatch.setitem(INDICATORS, "synthetic_global", _global_spec(ref))

    return _register


def test_an_unfetchable_global_ref_reaches_the_worklist_even_when_fresh(
    registered: Callable[[SeriesRef], None],
) -> None:
    """Criterion 1's second half: "stale **or not fetchable**".

    A manual ref is fresh and useless at the same time. Nobody can retrieve it
    without an operator typing the number in, which is precisely the thing the
    worklist exists to tell them to do, so freshness alone must not clear it.

    `coverage_report` already scores this at 0.0. Before the fix `stale_refs`
    skipped it for being ``GLOBAL``; a fix that only checked staleness would skip
    it for being fresh.
    """
    registered(
        SeriesRef(
            source=SOURCE_MANUAL,
            series_id="typed-in-by-hand",
            unit="index",
            frequency=Frequency.DAILY,
            last_observed=VERIFIED_ON,
        )
    )

    assert coverage_report(VERIFIED_ON)["synthetic_global"] == 0.0
    assert stale_refs(VERIFIED_ON)["synthetic_global"] == tuple(G10)


def test_an_unverified_global_ref_reaches_the_worklist_even_when_fresh(
    registered: Callable[[SeriesRef], None],
) -> None:
    """The other way a ref is unfetchable: registered but never confirmed.

    ``verified=False`` means the identifier could not be checked against the live
    source, so it may not resolve at all. Treating it as coverage because its
    recorded date is recent would be trusting a number nobody has seen.
    """
    registered(
        SeriesRef(
            source="fred",
            series_id="NEVER_CHECKED",
            unit="index",
            frequency=Frequency.DAILY,
            verified=False,
            last_observed=VERIFIED_ON,
        )
    )

    assert coverage_report(VERIFIED_ON)["synthetic_global"] == 0.0
    assert stale_refs(VERIFIED_ON)["synthetic_global"] == tuple(G10)


def test_a_fresh_fetchable_global_ref_stays_off_the_worklist(
    registered: Callable[[SeriesRef], None],
) -> None:
    """The control, so the two tests above are not passing for a trivial reason.

    Without this, a fix that listed every synthetic indicator would satisfy both.
    """
    registered(
        SeriesRef(
            source="fred",
            series_id="VIXCLS",
            unit="index",
            frequency=Frequency.DAILY,
            last_observed=VERIFIED_ON,
        )
    )

    assert coverage_report(VERIFIED_ON)["synthetic_global"] == 1.0
    assert "synthetic_global" not in stale_refs(VERIFIED_ON)


# --- the complement property, which is criterion 3 -------------------------


def _dates_spanning_both_allowances() -> tuple[date, ...]:
    """Dates from before either expiry to well after both.

    Chosen to straddle both boundaries rather than to be numerous: 2026-09-15 and
    2026-09-29 are the last usable days of the two allowances, so a range from
    `VERIFIED_ON` to 40 days past the later one exercises every combination of
    the two being fresh and stale.
    """
    return tuple(VERIFIED_ON + timedelta(days=offset) for offset in range(0, 111, 5))


@pytest.mark.parametrize("asof", _dates_spanning_both_allowances())
def test_nothing_at_zero_coverage_is_missing_from_the_worklist(asof: date) -> None:
    """Criterion 3. The documented complement, enforced rather than described.

    `stale_refs` calls itself the complement of `coverage_report`. This is that
    sentence as an assertion, swept over every registered indicator so it covers
    the three ``GLOBAL`` ones and the eighteen per-currency ones together, which
    is what criterion 3 asks for.

    Zero coverage is the strongest case and the one the defect hid: an indicator
    covering none of the universe that an operator is never told about.
    """
    coverage = coverage_report(asof)
    worklist = stale_refs(asof)

    dark = {key for key, fraction in coverage.items() if fraction == 0.0}
    missing = dark - set(worklist)

    assert not missing, (
        f"on {asof} these indicators cover no currency and are absent from the "
        f"worklist an operator is told to work through: {sorted(missing)}"
    )


@pytest.mark.parametrize("asof", _dates_spanning_both_allowances())
def test_partial_coverage_also_reaches_the_worklist(asof: date) -> None:
    """The weaker half of the same relationship, which the fix must not break.

    Anything below full coverage has at least one unusable leg, so it belongs on
    the worklist too. Asserted alongside the zero case because a fix that special
    cased ``GLOBAL`` indicators and disturbed the per-currency branch would pass
    the test above and fail this one.
    """
    coverage = coverage_report(asof)
    worklist = stale_refs(asof)

    incomplete = {key for key, fraction in coverage.items() if fraction < 1.0}
    missing = incomplete - set(worklist)

    assert not missing, (
        f"on {asof} these indicators are below full coverage and absent from "
        f"the worklist: {sorted(missing)}"
    )


@pytest.mark.parametrize("asof", _dates_spanning_both_allowances())
def test_a_fully_covered_indicator_stays_off_the_worklist(asof: date) -> None:
    """The other direction, so "complement" means complement both ways.

    Without this, reporting every indicator on every date would satisfy the two
    tests above. The docstring's promise that an empty mapping means a healthy
    registry only holds if full coverage keeps an indicator off the list.
    """
    coverage = coverage_report(asof)
    worklist = stale_refs(asof)

    healthy = {key for key, fraction in coverage.items() if fraction == 1.0}
    wrongly_listed = healthy & set(worklist)

    assert not wrongly_listed, (
        f"on {asof} these indicators cover every currency yet appear on the "
        f"worklist: {sorted(wrongly_listed)}"
    )


# --- the docstring, which is criterion 4 -----------------------------------


def test_the_docstring_says_what_a_global_indicator_returns() -> None:
    """Criterion 4. The behaviour is only discoverable if it is written down.

    A caller reading the signature sees ``Mapping[str, tuple[str, ...]]`` and has
    no way to know whether a universe-wide series reports as all eight currencies
    or as some sentinel. The previous docstring did not say, which is part of why
    the branch could be missing without anyone noticing.
    """
    doc = stale_refs.__doc__ or ""

    assert "GLOBAL" in doc
    assert "eight" in doc or "whole universe" in doc


@pytest.mark.parametrize("key", ["yield_2y_chg_1m", "yield_2y_chg_3m"])
def test_a_derived_indicator_nothing_serves_is_on_the_worklist_for_everyone(
    key: str,
) -> None:
    """Issue #169. The derived yield changes are fetched by no source today.

    `_yield_change_series` copied ``verified`` from the level ref, so the
    worklist named CHF and AUD as the only gaps while FRED and the curve
    sources refused the other six. A ref nothing can retrieve is not verified
    in the sense `SeriesRef.verified` documents, and the worklist must say so
    until ADR 0004 is implemented and a source emits them.
    """
    assert set(stale_refs(VERIFIED_ON)[key]) == set(G10)
    assert not any(ref.fetchable for ref in INDICATORS[key].series.values())
