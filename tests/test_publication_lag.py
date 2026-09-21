"""The publication lag is a fact about a leg, not about a cadence.

The engine assumed every monthly series publishes 45 days after its month
starts and every quarterly one 120 days after its quarter starts. FRED's mirror
of the OECD tables runs two to three months behind that, and the registry said
so in a note while the visibility rule kept reading the cadence table. A
historical run then admitted a June trade balance from mid-July that FRED did
not carry until September, which flatters a backtest, and the staleness ramp
that #126 builds on the same number would have discounted those legs for their
source's routine delay. #222 moves the lag onto the leg.

The walk at the bottom is the measurement that holds: on `VERIFIED_ON`, every
verified leg's newest print must be no older than its resolved lag plus one
cycle. A leg that cannot meet that with an honest lag is dead, not late.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from fbe.datasources import registry
from fbe.datasources.registry import (
    CYCLE_DAYS,
    DEFAULT_PUBLICATION_LAG_DAYS,
    VERIFIED_ON,
    SeriesRef,
    publication_lag,
)
from fbe.pillars.base import BasePillar
from fbe.types import Frequency, Observation

# --- the tables -------------------------------------------------------------


def test_every_frequency_has_a_lag_and_a_cycle() -> None:
    """A frequency missing from either table is a KeyError on the first
    observation that carries it, in the middle of a run."""
    assert set(DEFAULT_PUBLICATION_LAG_DAYS) == set(Frequency)
    assert set(CYCLE_DAYS) == set(Frequency)


def test_the_cycle_is_the_longest_gap_between_punctual_periods() -> None:
    """Ruled on #126: daily is four because a Friday close is the newest print
    until Tuesday over a long weekend, and one would zero every yield on a
    Monday."""
    assert CYCLE_DAYS[Frequency.DAILY] == 4
    assert CYCLE_DAYS[Frequency.WEEKLY] == 7
    assert CYCLE_DAYS[Frequency.MONTHLY] == 31
    assert CYCLE_DAYS[Frequency.QUARTERLY] == 92
    assert CYCLE_DAYS[Frequency.ANNUAL] == 366
    assert CYCLE_DAYS[Frequency.IRREGULAR] == 31


def test_the_lag_table_lives_in_the_registry_and_nowhere_else() -> None:
    """The registry is the module that knows the release calendar, and it
    cannot import from ``pillars/``, so the table had to move rather than be
    re-exported. A second copy in ``pillars.base`` would drift."""
    import fbe.pillars.base as pillars_base

    assert not hasattr(pillars_base, "DEFAULT_PUBLICATION_LAG_DAYS")
    assert "DEFAULT_PUBLICATION_LAG_DAYS" not in pillars_base.__all__


# --- the resolver -----------------------------------------------------------


def _ref(frequency: Frequency, lag: int | None = None) -> SeriesRef:
    return SeriesRef(
        source="fred",
        series_id="TEST",
        unit="percent",
        frequency=frequency,
        publication_lag_days=lag,
    )


def test_publication_lag_reads_the_override_when_the_leg_carries_one() -> None:
    assert publication_lag(_ref(Frequency.MONTHLY, lag=100), Frequency.MONTHLY) == 100


def test_publication_lag_falls_back_to_the_frequency_table() -> None:
    assert (
        publication_lag(_ref(Frequency.MONTHLY), Frequency.MONTHLY)
        == (DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY])
    )


def test_publication_lag_with_no_ref_is_the_table() -> None:
    """A key or currency the registry does not carry still has a frequency on
    the observation, and that is the only fact available."""
    assert (
        publication_lag(None, Frequency.QUARTERLY)
        == (DEFAULT_PUBLICATION_LAG_DAYS[Frequency.QUARTERLY])
    )


def test_publication_lag_reads_the_observation_frequency_not_the_ref() -> None:
    """The observation's frequency is what the source stamped; a ref with no
    override defers to it. Tested with the two disagreeing so the wrong read
    fails."""
    assert (
        publication_lag(_ref(Frequency.MONTHLY), Frequency.QUARTERLY)
        == (DEFAULT_PUBLICATION_LAG_DAYS[Frequency.QUARTERLY])
    )


def test_the_override_default_is_none_and_documented() -> None:
    ref = _ref(Frequency.MONTHLY)
    assert ref.publication_lag_days is None
    docstring = SeriesRef.__doc__ or ""
    assert "publication_lag_days" in docstring
    assert "period" in docstring


def test_a_non_positive_override_is_refused() -> None:
    """A lag of zero admits a figure on the first day of the span it
    describes, which is the look-ahead the rule exists to prevent."""
    with pytest.raises(ValueError, match="publication_lag_days"):
        publication_lag(_ref(Frequency.MONTHLY, lag=0), Frequency.MONTHLY)


# --- the visibility rule reads it -------------------------------------------


def _observation(indicator: str, currency: str, period: date) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        period=period,
        value=1.0,
        unit="percent",
        source="fred",
        series_id="TEST",
        frequency=Frequency.MONTHLY,
    )


def test_visible_admits_an_unstamped_observation_on_the_leg_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leg carrying an override longer than its frequency's default is hidden
    at the table's lag and visible at the override's. Asserted on a fake ref
    so the test does not depend on which real leg is slow this month."""
    period = date(2026, 6, 1)
    table_lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]
    override = table_lag + 55
    slow = _ref(Frequency.MONTHLY, lag=override)
    monkeypatch.setattr(
        registry,
        "series_for",
        lambda indicator, currency: slow if currency == "AUD" else None,
    )
    observation = _observation("trade_balance", "AUD", period)

    assert not BasePillar._visible(observation, period + timedelta(days=table_lag))
    assert not BasePillar._visible(observation, period + timedelta(days=override - 1))
    assert BasePillar._visible(observation, period + timedelta(days=override))


def test_visible_uses_the_table_for_an_indicator_the_registry_lacks() -> None:
    """The manual source permits a key the registry has not routed, and
    `series_for` raises on one; the rule must fall back rather than propagate."""
    period = date(2026, 6, 1)
    observation = _observation("not_a_registered_key", "USD", period)
    lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]

    assert not BasePillar._visible(observation, period + timedelta(days=lag - 1))
    assert BasePillar._visible(observation, period + timedelta(days=lag))


def test_visible_uses_the_table_for_a_currency_the_registry_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = date(2026, 6, 1)
    monkeypatch.setattr(registry, "series_for", lambda indicator, currency: None)
    observation = _observation("trade_balance", "ZZZ", period)
    lag = DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY]

    assert not BasePillar._visible(observation, period + timedelta(days=lag - 1))
    assert BasePillar._visible(observation, period + timedelta(days=lag))


def test_visible_still_prefers_a_real_release_stamp() -> None:
    """A stamped observation is a fact and the lag is never consulted."""
    period = date(2026, 6, 1)
    stamped = replace(
        _observation("trade_balance", "AUD", period),
        released_at=datetime(2026, 6, 20, tzinfo=UTC),
    )

    assert BasePillar._visible(stamped, date(2026, 6, 20))
    assert not BasePillar._visible(stamped, date(2026, 6, 19))


# --- the registry walk ------------------------------------------------------


def _verified_refs() -> list[tuple[str, str, SeriesRef]]:
    return [
        (key, currency, ref)
        for key, spec in registry.INDICATORS.items()
        for currency, ref in spec.series.items()
        if ref.verified and ref.last_observed is not None
    ]


def test_every_verified_leg_was_inside_its_lag_plus_one_cycle_when_verified() -> None:
    """On `VERIFIED_ON` a punctual leg's newest print is never older than its
    lag plus one cycle. A leg older than that either carries an understated
    lag, which a backtest exploits, or is dead and should not be verified.
    Either way the fix is in the registry, and this names the leg."""
    late = [
        (
            f"{key} {currency}",
            (VERIFIED_ON - ref.last_observed).days,
            publication_lag(ref, ref.frequency) + CYCLE_DAYS[ref.frequency],
        )
        for key, currency, ref in _verified_refs()
        if ref.last_observed is not None
        and (VERIFIED_ON - ref.last_observed).days
        > publication_lag(ref, ref.frequency) + CYCLE_DAYS[ref.frequency]
    ]
    assert late == [], "\n".join(
        f"{leg}: age {age} > {bound}" for leg, age, bound in late
    )


def test_every_override_is_longer_than_its_table_default() -> None:
    """An override exists to say a source is slower than its cadence. One
    shorter than the table would admit figures earlier than the cautious
    default, which is the direction that flatters, so it needs a stamp, not an
    override."""
    faster = [
        f"{key} {currency}"
        for key, currency, ref in _verified_refs()
        if ref.publication_lag_days is not None
        and ref.publication_lag_days < DEFAULT_PUBLICATION_LAG_DAYS[ref.frequency]
    ]
    assert faster == []


def test_every_override_says_how_it_was_measured() -> None:
    """The measurement and its date go in the note, so the next verification
    can tell a number that was measured from one that was typed to make the
    walk pass."""
    unexplained = [
        f"{key} {currency}"
        for key, currency, ref in _verified_refs()
        if ref.publication_lag_days is not None and "lag" not in ref.note.lower()
    ]
    assert unexplained == []
