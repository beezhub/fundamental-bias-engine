"""Tests for RISK, the one pillar that re-signs the same currency by the weather.

The other six hold a fixed view of what makes a currency strong. This one reads
one global regime number and hands each currency its own share of it through
`CurrencyMeta.risk_beta`, so the yen is the best currency in the world on the
worst day of the year and an unremarkable one in a quiet rally. The failures
worth guarding follow from that shape:

- The sign. A negative regime times a negative beta is a positive score, and
  that is the haven bid, the only way this pillar says anything good about
  anyone. Inverting it would sell the yen into a panic.
- Zero meaning two different things. At ``R = 0`` every currency scores exactly
  ``0.0`` and the pillar is **present**: a calm market genuinely offers no
  regime signal, which is a finding, and it is not the same as the feed being
  down. Every other pillar in this package uses a score of 0.0 with ``z`` of
  ``None`` to mean absence, so this is the one place the two must be told
  apart deliberately.
- Cross-sectional normalisation, which would cancel the regime out entirely.
  ``R`` is one number shared by all eight, so dividing by the cross-section's
  own standard deviation leaves a constant ranking of betas that never changes
  from run to run, which is the one thing this pillar must not be.
- A partial regime. If either global series is unusable the answer is not half
  a reading: an unmeasurable regime is unmeasurable for everybody, and all
  eight currencies lose the pillar together.

Every observation is built in the test that uses it, so each regime number is
checkable by hand against values written in this file. Nothing reaches the
network.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from fbe.config import ScoringConfig
from fbe.datasources import registry
from fbe.datasources.registry import (
    GLOBAL,
    INDICATORS,
    MIN_HISTORY_OBSERVATIONS,
    UNCONSUMED_INDICATORS,
    coverage_report,
)
from fbe.pillars.base import BasePillar
from fbe.pillars.risk import (
    DRAWDOWN_SCALE_PCT,
    DRAWDOWN_WINDOW_SESSIONS,
    MIN_DRAWDOWN_WINDOW_SESSIONS,
    RISK_SCALE,
    VOL_SCALE_Z,
    RiskPillar,
    _drawdown_pct,
)
from fbe.types import Frequency, Observation
from fbe.universe import G10, meta

ASOF = date(2026, 9, 15)

EQUITY = "world_equity_index"
VOL = "vol_index"

EXPECTED_REQUIRES = (EQUITY, VOL)
"""Spelled out rather than read off the pillar, so a key silently dropped from
`requires` fails here rather than quietly shrinking what these tests cover."""


@pytest.fixture
def pillar() -> RiskPillar:
    return RiskPillar()


def obs(
    indicator: str,
    value: float,
    period: date,
    *,
    currency: str = GLOBAL,
    released_at: datetime | None = None,
) -> Observation:
    """One global observation, released the day it describes.

    Both series are daily closes, so the release stamp is the same day rather
    than a lag. A test that wants the visibility rule exercised passes its own.
    """
    stamped = (
        released_at
        if released_at is not None
        else datetime(period.year, period.month, period.day, 22, 0, tzinfo=UTC)
    )
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id=indicator,
        unit=INDICATORS[indicator].unit,
        frequency=Frequency.DAILY,
        released_at=stamped,
    )


def sessions(count: int, *, end: date = ASOF) -> list[date]:
    """``count`` consecutive dates ending at ``end``, oldest first."""
    return [end - timedelta(days=offset) for offset in reversed(range(count))]


def equity_series(values: Sequence[float], *, end: date = ASOF) -> list[Observation]:
    """A daily equity series with the given closes, oldest first."""
    return [
        obs(EQUITY, value, period)
        for value, period in zip(values, sessions(len(values), end=end), strict=True)
    ]


def usable_equity(values: Sequence[float], *, end: date = ASOF) -> list[Observation]:
    """`equity_series`, padded at the front to clear the drawdown floor.

    The pad repeats the oldest close. That can never raise the window high,
    since the repeated value is already in the series, and it never touches the
    latest close, so the drawdown a test works out by hand is the one the pillar
    reads. Tests that want the floor itself exercised call `equity_series`
    directly with a short series.
    """
    shortfall = max(0, MIN_DRAWDOWN_WINDOW_SESSIONS - len(values))
    return equity_series([values[0]] * shortfall + list(values), end=end)


def vol_series(values: Sequence[float], *, end: date = ASOF) -> list[Observation]:
    """A daily volatility series with the given closes, oldest first."""
    return [
        obs(VOL, value, period)
        for value, period in zip(values, sessions(len(values), end=end), strict=True)
    ]


DAILY_FLOOR = MIN_HISTORY_OBSERVATIONS[Frequency.DAILY]
"""One year of sessions, the fewest `BasePillar.time_series_z` will score.

Read from the mapping rather than written as 252, so these fixtures follow the
rule rather than today's number.
"""


def steady_vol(latest: float, *, length: int = DAILY_FLOOR + 1) -> list[float]:
    """A volatility history with real dispersion, ending at ``latest``.

    Alternating either side of 16.0 so the standard deviation is non-zero. The
    history length is even, which matters: it puts equally many values on each
    side and so fixes the mean at exactly 16.0, and ``steady_vol(16.0)`` then
    has a z-score of exactly 0.0. An odd history left the mean a fortieth of a
    point below 16.0, which was small enough to look like nothing and large
    enough to move a hand-computed regime in the third decimal.

    The default was 41 until #214 made the history floor frequency-aware.
    ``vol_index`` is daily, so the floor is now a year of sessions and a
    41-session fixture would be refused rather than scored, which would leave
    every test here measuring the floor instead of the pillar. ``DAILY_FLOOR``
    plus one keeps the history even, so the mean stays exactly 16.0 for the
    reason above.
    """
    history = [16.0 + (1.0 if index % 2 else -1.0) for index in range(length - 1)]
    return [*history, latest]


def run(
    pillar: RiskPillar,
    equity: Sequence[float],
    vol: Sequence[float],
    *,
    currencies: Sequence[str] | None = None,
    pad_equity: bool = True,
) -> dict[str, dict[str, float | None]]:
    """Extract and transform one run, returning the components per currency.

    ``equity`` is padded through `usable_equity` unless ``pad_equity`` is false,
    so a test that cares about the drawdown arithmetic writes only the closes
    that arithmetic uses. The tests for the floor pass ``pad_equity=False``.
    """
    wanted = tuple(currencies if currencies is not None else sorted(G10))
    build = usable_equity if pad_equity else equity_series
    observations = [*build(equity), *vol_series(vol)]
    extracted = pillar._extract(observations, wanted, ASOF)
    return {
        currency: dict(components)
        for currency, components in pillar._transform(extracted, ASOF).items()
    }


# --- the registry the ruling on this issue reshaped --------------------------


def test_the_global_equity_key_holds_one_global_ref_and_no_per_currency_ones() -> None:
    """The ruling's first criterion, and the reason it is a new key.

    A key whose refs are `GLOBAL` is a global reading and a key whose refs are
    per-currency is a per-currency reading. One key never holds both, because
    the pillar asking for a global number must not receive one currency's index
    and call the result global.
    """
    spec = INDICATORS[EQUITY]

    assert set(spec.series) == {GLOBAL}


def test_the_per_currency_equity_refs_are_declared_unconsumed() -> None:
    """They are attributed to RISK and nothing reads them now.

    `tests/test_registry_pillar_agreement.py` fails a series attributed to a
    pillar that is in neither that pillar's ``requires`` nor this set, so
    leaving them attributed and unread would break that guard. Declaring them
    is the deliberate statement that set exists for.
    """
    assert "equity_index" in UNCONSUMED_INDICATORS
    assert "equity_index" not in RiskPillar.requires

    # Membership alone is not the criterion. The ruling asks for the reason in
    # the ``description``, exactly as `yield_10y` carries it, because that is
    # the text a reader meets when they look the key up. Reverting the
    # description to the old "feeds the risk pillar" sentence passes a
    # membership check and leaves the registry saying something false.
    description = INDICATORS["equity_index"].description.lower()
    assert "consumed by no pillar" in description
    assert "world_equity_index" in description
    assert "availability rather than as a live input" in description


def test_the_per_currency_equity_coverage_counts_all_eight_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side effect that made the obvious fix unsafe, pinned on the number.

    `coverage_report` short-circuits on a `GLOBAL` ref: a key holding one is
    reported on that ref alone and its per-currency refs stop being counted
    entirely. Adding a world index under `equity_index` would therefore have
    silently changed what the coverage report describes, with nothing raising.

    Asserting the key is in the report cannot see any of that, because the
    report holds a value for every registered key on both branches. Asserting
    the value can. Break one of the eight legs and a report still counting them
    answers 7/8; a report short-circuiting on a `GLOBAL` ref answers 1.0 and the
    broken leg has vanished. That number is the criterion, so it is the
    assertion.
    """
    spec = INDICATORS["equity_index"]
    assert GLOBAL not in spec.series
    assert spec.series.keys() >= set(G10)

    one_leg_down = replace(
        spec,
        series={**spec.series, "NZD": replace(spec.series["NZD"], verified=False)},
    )
    monkeypatch.setitem(registry.INDICATORS, "equity_index", one_leg_down)

    assert coverage_report()["equity_index"] == pytest.approx(7 / 8)


def test_the_world_index_says_a_us_index_is_standing_in_for_the_world() -> None:
    """The ruling's fallback, recorded at the point of use.

    The substitution is real and it is the pillar's largest known weakness, so
    it is stated in the description a reader meets rather than implied by a
    ticker they would have to recognise.
    """
    spec = INDICATORS[EQUITY]
    description = spec.description.lower()

    assert "united states" in description
    assert "world" in description or "global" in description
    assert spec.series[GLOBAL].series_id == "SP500"


# --- what _extract selects ---------------------------------------------------


def test_the_pillar_asks_for_the_two_global_series(pillar: RiskPillar) -> None:
    """Guards every test below, which would narrow silently with `requires`."""
    assert tuple(pillar.requires) == EXPECTED_REQUIRES


def test_the_global_series_reach_every_currency_key(pillar: RiskPillar) -> None:
    """Both series carry ``currency="GLOBAL"`` and belong to all eight.

    The repetition keeps this pillar inside the same per-currency contract as
    the other six, so `compute` needs no special case for it. A routing that
    kept `GLOBAL` under its own key, which is what the shared loop does, would
    leave every currency with nothing.
    """
    observations = [*equity_series([100.0, 99.0]), *vol_series([16.0, 17.0])]

    extracted = pillar._extract(observations, sorted(G10), ASOF)

    assert set(extracted) == set(G10)
    for currency in G10:
        assert set(extracted[currency]) == set(EXPECTED_REQUIRES)
        assert [entry.value for entry in extracted[currency][EQUITY]] == [100.0, 99.0]
        assert [entry.value for entry in extracted[currency][VOL]] == [16.0, 17.0]


def test_a_per_currency_observation_is_not_admitted(pillar: RiskPillar) -> None:
    """This pillar reads a global reading and only a global one.

    A stray observation of the same key carrying a currency code would make one
    currency's regime differ from the rest, which is precisely the thing `R`
    being a single shared number rules out.

    The stray sits on a period no global print occupies, and its value changes
    the answer. An earlier version put it on the newest period, where
    `BasePillar._newest_vintages` dropped it as a duplicate whatever `_extract`
    had done with its currency, so an override that relabelled every
    observation `GLOBAL` passed this test and the rest of the file.
    """
    stray = obs(EQUITY, 200.0, date(2026, 9, 1), currency="JPY")
    observations = [
        *usable_equity([100.0, 99.0]),
        *vol_series(steady_vol(16.0)),
        stray,
    ]

    extracted = pillar._extract(observations, sorted(G10), ASOF)

    admitted = extracted["JPY"][EQUITY]
    assert 200.0 not in [entry.value for entry in admitted]
    assert all(entry.currency == GLOBAL for entry in admitted)

    built = pillar._transform(extracted, ASOF)

    # Admitting the stray would make 200.0 the window high and turn a 1% fall
    # into a 50.5% one, deep enough to saturate the component.
    assert built["JPY"]["drawdown_pct"] == pytest.approx(-1.0)


def test_an_observation_not_yet_published_takes_no_part(pillar: RiskPillar) -> None:
    """The shared visibility rule, which this pillar must not sidestep.

    It overrides `_extract` for the `GLOBAL` routing, which is a licence to
    route, not a licence to skip the rule every other pillar applies.
    """
    unpublished = obs(
        EQUITY,
        1.0,
        date(2026, 9, 14),
        released_at=datetime(2026, 12, 1, 12, tzinfo=UTC),
    )
    observations = [
        *equity_series([100.0, 99.0]),
        *vol_series([16.0, 17.0]),
        unpublished,
    ]

    extracted = pillar._extract(observations, ("USD",), ASOF)

    assert 1.0 not in [entry.value for entry in extracted["USD"][EQUITY]]


def test_the_pillar_reaches_the_shared_helpers(pillar: RiskPillar) -> None:
    """Overriding `_extract` must not mean reimplementing the rule inside it."""
    import fbe.pillars.base as base_module
    import fbe.pillars.risk as risk_module

    assert "_visible" not in vars(RiskPillar)
    assert "_newest_vintages" not in vars(RiskPillar)
    assert not hasattr(risk_module, "_vintage_key")
    assert type(pillar)._visible is base_module.BasePillar._visible


# --- the regime itself -------------------------------------------------------


def test_the_drawdown_is_measured_from_the_trailing_window_high(
    pillar: RiskPillar,
) -> None:
    """A known high, a known close, and a drawdown checkable by hand.

    The high is 120.0 and the latest close is 108.0, so the drawdown is
    ``(108 - 120) / 120 * 100``, which is ``-10.0``: exactly
    `DRAWDOWN_SCALE_PCT`, so the drawdown component saturates at ``-1.0``.
    """
    closes = [100.0, 110.0, 120.0, 115.0, 108.0]

    built = run(pillar, closes, steady_vol(16.0))

    expected_dd = -1.0
    expected_vol = 0.0
    expected_r = min(0.0, 0.5 * expected_dd + 0.5 * expected_vol)
    assert built["USD"]["regime"] == pytest.approx(expected_r)


def test_a_high_outside_the_trailing_window_does_not_count(
    pillar: RiskPillar,
) -> None:
    """The window is what makes this a 52-week drawdown rather than a record one.

    A spike older than `DRAWDOWN_WINDOW_SESSIONS` is deliberately placed so
    that counting it would give a far deeper drawdown than the window's own
    high does.
    """
    inside = [100.0] * DRAWDOWN_WINDOW_SESSIONS
    inside[-1] = 95.0
    closes = [1000.0, *inside]

    built = run(pillar, closes, steady_vol(16.0))

    # Against the in-window high of 100.0 the drawdown is -5.0%, so the
    # component is -0.5. Against the 1000.0 spike it would be -90.5%, saturating
    # at -1.0, and the regime would differ by a quarter of the band.
    assert built["USD"]["regime"] == pytest.approx(0.5 * -0.5 + 0.5 * 0.0)


def test_the_volatility_z_comes_from_the_shared_helper(pillar: RiskPillar) -> None:
    """Not a second implementation, which would drift from the first.

    Computed here by calling `BasePillar.time_series_z` on the same series the
    pillar was handed, so this fails if the pillar standardises differently,
    uses a different window, or z-scores the wrong end of the series.
    """
    vol = steady_vol(20.0)
    config = ScoringConfig()
    expected_z = BasePillar.time_series_z(vol_series(vol), config.lookback_years, ASOF)

    built = run(pillar, [100.0, 100.0], vol)

    assert expected_z is not None
    expected_component = max(-1.0, min(1.0, -expected_z / VOL_SCALE_Z))
    assert built["USD"]["regime"] == pytest.approx(
        min(0.0, 0.5 * 0.0 + 0.5 * expected_component)
    )


def test_a_market_at_its_high_in_calm_scores_exactly_zero(
    pillar: RiskPillar,
) -> None:
    """The upper bound, which is the pillar's central modelling choice.

    Euphoria is not the mirror of panic. A rally with low volatility would
    otherwise make confident high-beta calls at exactly the moment the pillar
    knows least.
    """
    built = run(pillar, [100.0, 105.0, 110.0], steady_vol(10.0))

    assert built["USD"]["regime"] == 0.0
    for currency in G10:
        assert built[currency]["risk_response"] == 0.0


def test_low_volatility_can_offset_a_shallow_drawdown(pillar: RiskPillar) -> None:
    """The two bounds do not sit in the same place, and the docstring says so.

    The drawdown component is bounded above at zero individually; the
    volatility component is not, so unusually low volatility produces a
    positive contribution that offsets a shallow fall before the combined
    regime is bounded.
    """
    shallow = [100.0, 99.0]
    quiet = steady_vol(12.0)
    config = ScoringConfig()
    vol_z = BasePillar.time_series_z(vol_series(quiet), config.lookback_years, ASOF)

    built = run(pillar, shallow, quiet)

    assert vol_z is not None
    assert vol_z < 0.0
    dd = -1.0 / DRAWDOWN_SCALE_PCT
    vol_component = max(-1.0, min(1.0, -vol_z / VOL_SCALE_Z))
    assert vol_component > 0.0
    assert built["USD"]["regime"] == pytest.approx(
        min(0.0, 0.5 * dd + 0.5 * vol_component)
    )


# --- the sign, which is what the pillar exists for ---------------------------


def test_a_risk_off_buys_the_haven_and_sells_the_high_beta(
    pillar: RiskPillar,
) -> None:
    """A negative regime times a negative beta is a positive score.

    This is the haven bid. JPY carries a beta of -0.9 and AUD +0.9, so a deep
    drawdown with elevated volatility must score the yen positive and the
    Australian dollar negative. An inverted pillar sells the yen into a panic.
    """
    crash = [120.0, 118.0, 110.0, 100.0]
    panicked = steady_vol(40.0)

    built = run(pillar, crash, panicked)

    regime = built["JPY"]["regime"]
    assert regime is not None and regime < 0.0
    assert meta("JPY").risk_beta < 0.0
    assert meta("AUD").risk_beta > 0.0
    jpy = built["JPY"]["risk_response"]
    aud = built["AUD"]["risk_response"]
    assert jpy is not None and jpy > 0.0
    assert aud is not None and aud < 0.0


def test_each_currency_gets_the_regime_through_its_own_beta(
    pillar: RiskPillar,
) -> None:
    """Computed here from the betas rather than read back from the pillar."""
    crash = [120.0, 110.0, 100.0]

    built = run(pillar, crash, steady_vol(30.0))

    regime = built["USD"]["regime"]
    assert regime is not None
    for currency in sorted(G10):
        assert built[currency]["risk_response"] == pytest.approx(
            RISK_SCALE * regime * meta(currency).risk_beta
        ), currency
        assert built[currency]["regime"] == pytest.approx(regime), currency


def test_the_regime_is_one_number_for_the_whole_universe(
    pillar: RiskPillar,
) -> None:
    """Eight currencies, one weather report. Anything else is a different model."""
    built = run(pillar, [120.0, 100.0], steady_vol(25.0))

    assert len({built[currency]["regime"] for currency in G10}) == 1


# --- absence, which here is not a score of zero ------------------------------


def test_an_unusable_equity_series_takes_the_pillar_out_for_everybody(
    pillar: RiskPillar,
) -> None:
    """An unmeasurable regime is unmeasurable for everybody.

    Not half a reading built from the volatility side alone: the regime is
    defined as both, and the scorer must drop 0.10 from all eight composites
    together rather than from none of them.
    """
    observations = vol_series(steady_vol(20.0))

    extracted = pillar._extract(observations, sorted(G10), ASOF)
    built = pillar._transform(extracted, ASOF)

    for currency in G10:
        assert built[currency]["risk_response"] is None
        assert built[currency]["regime"] is None


def test_an_unusable_volatility_series_takes_the_pillar_out_for_everybody(
    pillar: RiskPillar,
) -> None:
    """The same, on the other series, tested in its own turn."""
    observations = usable_equity([120.0, 100.0])

    extracted = pillar._extract(observations, sorted(G10), ASOF)
    built = pillar._transform(extracted, ASOF)

    for currency in G10:
        assert built[currency]["risk_response"] is None


def test_a_flat_volatility_history_is_unusable_rather_than_calm(
    pillar: RiskPillar,
) -> None:
    """`time_series_z` returns ``None`` on a zero standard deviation.

    A flat history cannot say whether today is unusual, which is not the same
    as saying today is ordinary. Reading it as a z-score of zero would invent a
    calm reading out of no information.
    """
    built = run(pillar, [120.0, 100.0], [16.0] * 40)

    assert built["USD"]["risk_response"] is None
    assert built["USD"]["regime"] is None


def test_a_calm_regime_is_present_and_not_absent(pillar: RiskPillar) -> None:
    """The distinction this pillar's docstring says the report must not blur.

    At ``R = 0`` every currency scores exactly 0.0 and the pillar still counts
    its full weight. Everywhere else in this package a score of 0.0 with ``z``
    of ``None`` means absence, so this is the one place the two have to be told
    apart deliberately.
    """
    observations = [
        *usable_equity([100.0, 105.0, 110.0]),
        *vol_series(steady_vol(10.0)),
    ]

    scores = pillar.compute(observations, sorted(G10), ASOF)

    for currency in sorted(G10):
        assert scores[currency].z is not None, currency
        assert scores[currency].score == pytest.approx(0.0), currency
        assert scores[currency].weight > 0.0, currency


def test_an_absent_regime_is_marked_absent(pillar: RiskPillar) -> None:
    """And the other half of the same distinction, on the same pillar."""
    scores = pillar.compute(vol_series(steady_vol(20.0)), sorted(G10), ASOF)

    for currency in sorted(G10):
        assert scores[currency].z is None, currency
        assert scores[currency].score == 0.0, currency


# --- normalisation, or the absence of it -------------------------------------


def test_normalise_passes_the_response_through_unchanged(
    pillar: RiskPillar,
) -> None:
    """Standardising here would cancel the regime out entirely."""
    built = run(pillar, [120.0, 100.0], steady_vol(30.0))
    components = {currency: dict(values) for currency, values in built.items()}

    normalised = pillar._normalise(components)

    for currency in sorted(G10):
        assert normalised[currency] == pytest.approx(
            components[currency]["risk_response"]
        ), currency


def test_one_currencys_beta_does_not_move_the_others(
    pillar: RiskPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test a cross-sectional step fails.

    Under cross-sectional standardisation every currency's score depends on
    every other currency's beta, so changing one moves them all. Here the
    regime is shared and the beta is private, so nothing else may move.
    """
    before = run(pillar, [120.0, 100.0], steady_vol(30.0))

    original = meta("AUD")
    monkeypatch.setattr(
        "fbe.pillars.risk.meta",
        lambda code: replace(original, risk_beta=0.2) if code == "AUD" else meta(code),
    )
    after = run(pillar, [120.0, 100.0], steady_vol(30.0))

    assert after["AUD"]["risk_response"] != pytest.approx(
        before["AUD"]["risk_response"]
    )
    for currency in sorted(set(G10) - {"AUD"}):
        assert after[currency]["risk_response"] == pytest.approx(
            before[currency]["risk_response"]
        ), currency


# --- what reaches a score ----------------------------------------------------


def test_the_regime_reaches_raw_so_a_reader_can_check_it(
    pillar: RiskPillar,
) -> None:
    """`raw` is ``R`` itself, not the currency's response.

    Every currency's response is a different number and all eight follow from
    one regime, so the regime is the number a reader checks the pillar with.
    """
    observations = [
        *usable_equity([120.0, 110.0, 100.0]),
        *vol_series(steady_vol(30.0)),
    ]

    scores = pillar.compute(observations, sorted(G10), ASOF)

    regimes = {scores[currency].raw for currency in G10}
    assert len(regimes) == 1
    only = regimes.pop()
    assert only is not None and only < 0.0


def test_the_scale_constants_are_read_and_not_retyped(
    pillar: RiskPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override one and the score has to move, or the constant is decoration."""
    before = run(pillar, [120.0, 100.0], steady_vol(30.0))

    monkeypatch.setattr("fbe.pillars.risk.RISK_SCALE", RISK_SCALE * 2.0)
    after = run(pillar, [120.0, 100.0], steady_vol(30.0))

    assert after["AUD"]["risk_response"] == pytest.approx(
        2.0 * (before["AUD"]["risk_response"] or 0.0)
    )


def test_the_drawdown_scale_is_read_and_not_retyped(
    pillar: RiskPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for the saturation point of the drawdown component."""
    closes = [100.0, 95.0]
    before = run(pillar, closes, steady_vol(16.0))

    monkeypatch.setattr("fbe.pillars.risk.DRAWDOWN_SCALE_PCT", DRAWDOWN_SCALE_PCT / 2)
    after = run(pillar, closes, steady_vol(16.0))

    assert after["USD"]["regime"] != pytest.approx(before["USD"]["regime"])


def test_the_volatility_scale_is_read_and_not_retyped(
    pillar: RiskPillar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third of the three constants the issue names, which had no test.

    The volatility history is chosen so its z-score sits inside ``[-2, 2]``.
    Outside that the component is already clamped and doubling the scale moves
    nothing, which is how a constant passes a wire test while being decoration.
    """
    before = run(pillar, [100.0, 95.0], steady_vol(17.0))

    monkeypatch.setattr("fbe.pillars.risk.VOL_SCALE_Z", VOL_SCALE_Z * 2.0)
    after = run(pillar, [100.0, 95.0], steady_vol(17.0))

    assert before["USD"]["vol_z"] is not None
    assert abs(before["USD"]["vol_z"] or 0.0) < VOL_SCALE_Z
    assert after["USD"]["regime"] != pytest.approx(before["USD"]["regime"])


def test_every_currency_asked_for_comes_back(pillar: RiskPillar) -> None:
    """Including on a run the pillar could not read, which the scorer relies on."""
    scores = pillar.compute(vol_series(steady_vol(20.0)), sorted(G10), ASOF)

    assert set(scores) == set(G10)


# --- what the sweep found these could not see --------------------------------


def test_the_oldest_session_in_the_window_still_counts(pillar: RiskPillar) -> None:
    """The window boundary at a depth where the clamp cannot hide the answer.

    The high sits at exactly the oldest in-window position, so a window one
    session short loses it and measures the drawdown against a lower high. Its
    sibling below holds the same shape and lets the fall saturate, which pins
    the boundary against the component's floor; this one keeps the fall shallow,
    so an off-by-one changes a magnitude rather than trading one clamped value
    for another.

    An earlier version used a 25% fall against an alternative of 10%. Both
    saturate at -1.0, so the slice this test is named for could be moved by a
    session and the assertion still held.
    """
    closes = [100.0, *([92.0] * (DRAWDOWN_WINDOW_SESSIONS - 2)), 92.0]
    assert len(closes) == DRAWDOWN_WINDOW_SESSIONS

    built = run(pillar, closes, steady_vol(16.0))

    # Against the in-window high of 100.0 the fall is -8%, so the component is
    # -0.8 and nothing clamps. A window one session short takes 92.0 as the
    # high, finds the close sitting on it, and reports 0.0.
    full_window_dd = (92.0 - 100.0) / 100.0 * 100.0
    assert full_window_dd == pytest.approx(-8.0)
    dd_component = max(-1.0, min(0.0, full_window_dd / DRAWDOWN_SCALE_PCT))
    assert dd_component == pytest.approx(-0.8)
    assert built["USD"]["regime"] == pytest.approx(0.5 * dd_component + 0.5 * 0.0)


def test_a_window_one_session_short_would_miss_the_high(
    pillar: RiskPillar,
) -> None:
    """The same boundary, at a depth where the component saturates.

    The high is at the oldest in-window session and the close sits exactly on
    the *second* oldest. Counting the full window gives a 12.5% fall, which
    saturates the component at -1.0; one session short takes the close's own
    level as the high and gives 0%. Two different regimes.
    """
    closes = [120.0, *([105.0] * (DRAWDOWN_WINDOW_SESSIONS - 2)), 105.0]
    assert len(closes) == DRAWDOWN_WINDOW_SESSIONS

    built = run(pillar, closes, steady_vol(16.0))

    full_window_dd = (105.0 - 120.0) / 120.0 * 100.0
    assert full_window_dd == pytest.approx(-12.5)
    dd_component = max(-1.0, min(0.0, full_window_dd / DRAWDOWN_SCALE_PCT))
    assert dd_component == pytest.approx(-1.0)
    short_window_dd = 0.0
    assert dd_component != pytest.approx(short_window_dd)
    assert built["USD"]["regime"] == pytest.approx(0.5 * dd_component)


def test_an_index_that_is_not_positive_is_refused(pillar: RiskPillar) -> None:
    """A drawdown computed against a zero or negative high is nonsense.

    An index cannot be zero, so reaching this means the series is not what the
    caller thinks it is. Dividing anyway would produce a number, and a number
    here becomes a regime and then a score on every currency.
    """
    built = run(pillar, [0.0, 0.0, 0.0], steady_vol(20.0))

    assert built["USD"]["regime"] is None
    assert built["USD"]["risk_response"] is None


def test_the_volatility_window_is_anchored_on_the_run_date(
    pillar: RiskPillar,
) -> None:
    """Not on whatever the newest observation happens to be.

    `time_series_z` defaults its ``asof`` to the newest period in the series,
    so on a feed that stopped months ago the window slides back with it and
    z-scores against a different stretch of history than the run asked about.

    The history has to be longer than the lookback for the two to differ at
    all: six years of it, ending two hundred days before the run. Anchored on
    the run date the window starts five years before the run and drops the
    oldest stretch; anchored on the newest observation it starts five years
    before *that* and keeps it. An earlier version of this test used sixty
    observations, which sit inside both windows, so the two answers were
    identical and it could not fail.

    The recent stretch is a year of sessions because the anchored window sees
    only that stretch, and since #214 a daily window under `DAILY_FLOOR` is
    refused rather than scored. At sixty it returned ``None`` for both windows
    and the test failed on the guard below rather than on the anchoring.
    """
    stale_end = ASOF - timedelta(days=200)
    old = [40.0 + (1.0 if index % 2 else -1.0) for index in range(60)]
    recent = [16.0 + (1.0 if index % 2 else -1.0) for index in range(DAILY_FLOOR)]
    # The old block has to land in the band between the two window starts:
    # after ``newest - 5y``, which is where the drifting window begins, and
    # before ``asof - 5y``, which is where the anchored one does. Outside that
    # band both windows agree and the test cannot fail.
    old_end = ASOF - timedelta(days=5 * 365 + 45)
    vol = [
        *vol_series(old, end=old_end),
        *vol_series([*recent, 20.0], end=stale_end),
    ]
    observations = [*usable_equity([120.0, 100.0]), *vol]
    config = ScoringConfig()

    anchored = BasePillar.time_series_z(vol, config.lookback_years, ASOF)
    drifting = BasePillar.time_series_z(vol, config.lookback_years, None)
    assert anchored is not None and drifting is not None
    assert anchored != pytest.approx(drifting)

    extracted = pillar._extract(observations, ("USD",), ASOF)
    built = pillar._transform(extracted, ASOF)

    dd = max(-1.0, min(0.0, (100.0 - 120.0) / 120.0 * 100.0 / DRAWDOWN_SCALE_PCT))
    expected = min(0.0, 0.5 * dd + 0.5 * max(-1.0, min(1.0, -anchored / VOL_SCALE_Z)))
    assert built["USD"]["regime"] == pytest.approx(expected)


def test_the_lookback_window_is_read_from_config() -> None:
    """Override it and the z-score has to move, or the config is decoration.

    The shipped default is 5 years, so a hardcoded 5 passes every test that
    does not override it. The history therefore has to straddle the boundary:
    a stretch two years back sitting around 30, and a recent stretch around 16.
    A one-year window sees only the recent stretch and reads today's 20 as
    unusually high; a five-year window sees both and reads it as ordinary.

    An earlier version of this test used eighty days of history, which fits
    inside both windows, so the two z-scores were identical and the assertion
    could not fail.

    The recent stretch is a year of sessions because the one-year window sees
    only that stretch, and since #214 a daily window under `DAILY_FLOOR` is
    refused rather than scored. `sessions` steps one calendar day at a time, so
    252 of them span 252 days and sit inside a one-year lookback with room.
    """
    recent = [16.0 + (1.0 if index % 2 else -1.0) for index in range(DAILY_FLOOR)]
    old = [30.0 + (1.0 if index % 2 else -1.0) for index in range(40)]
    observations = [
        *usable_equity([120.0, 100.0]),
        *vol_series(old, end=ASOF - timedelta(days=730)),
        *vol_series([*recent, 20.0]),
    ]

    wide = BasePillar.time_series_z(
        [entry for entry in observations if entry.indicator == VOL], 5, ASOF
    )
    narrow = BasePillar.time_series_z(
        [entry for entry in observations if entry.indicator == VOL], 1, ASOF
    )
    assert wide is not None and narrow is not None
    assert wide != pytest.approx(narrow)

    default = RiskPillar()
    narrowed = RiskPillar(ScoringConfig(lookback_years=1))
    assert default.config.lookback_years == 5

    wide_built = default._transform(
        default._extract(observations, ("USD",), ASOF), ASOF
    )
    narrow_built = narrowed._transform(
        narrowed._extract(observations, ("USD",), ASOF), ASOF
    )

    assert wide_built["USD"]["regime"] != pytest.approx(narrow_built["USD"]["regime"])


def test_cross_sectional_standardisation_would_be_visible_here(
    pillar: RiskPillar,
) -> None:
    """The response keeps its own magnitude, not a standardised one.

    Under cross-sectional standardisation the eight responses would come back
    with a standard deviation of 1.0 whatever the regime, so a mild selloff and
    a crash would produce identical output. Here the magnitudes scale with the
    regime, which is the property that makes the pillar mean anything.
    """
    mild = run(pillar, [100.0, 99.0], steady_vol(17.0))
    severe = run(pillar, [120.0, 100.0], steady_vol(30.0))

    mild_aud = mild["AUD"]["risk_response"]
    severe_aud = severe["AUD"]["risk_response"]
    assert mild_aud is not None and severe_aud is not None
    assert abs(severe_aud) > abs(mild_aud)
    # And the ratio is the ratio of the regimes, which standardisation destroys.
    mild_r = mild["AUD"]["regime"]
    severe_r = severe["AUD"]["regime"]
    assert mild_r is not None and severe_r is not None
    assert severe_aud / mild_aud == pytest.approx(severe_r / mild_r)


# --- what the second review round found these could not see ------------------


def test_the_two_bounds_do_not_sit_in_the_same_place() -> None:
    """`regime` is public, and the paragraph about its bounds is owed a test.

    Through the pipeline the drawdown component's individual upper bound is
    unreachable: `_drawdown_pct` measures against a high taken over a window
    that includes the latest close, so it can never hand `regime` a positive
    number. `regime` is a public staticmethod with a published contract, and at
    that door the two bounds are distinguishable. Without this test, removing
    the individual bound changes nothing any test can see.
    """
    # A positive drawdown, which only a direct caller can supply. The individual
    # bound clamps it to 0.0, leaving R at half the volatility leg.
    assert RiskPillar.regime(5.0, 4.0) == pytest.approx(-0.5)
    # Without that bound the drawdown leg would be +0.5 and R would be -0.25.
    assert RiskPillar.regime(5.0, 4.0) != pytest.approx(-0.25)
    # And the combined bound, which is the one that catches a positive average.
    assert RiskPillar.regime(-20.0, -4.0) == pytest.approx(0.0)


def test_a_component_the_pillar_never_declared_fails_loudly(
    pillar: RiskPillar,
) -> None:
    """`_normalise` indexes rather than defaults, and this is why.

    A `.get` with a fallback would answer a number for a mapping built under a
    name the pillar does not declare, and the number it would answer is the one
    that reads as a dead feed for all eight. The comment in `_normalise` argues
    for the indexing; this is the argument as an assertion. `tests/test_emit_sd.py`
    carried exactly this defect, under the name ``risk_r``, for as long as it was
    guarded by a scaffolding skip.
    """
    with pytest.raises(KeyError):
        pillar._normalise({currency: {"risk_r": 0.0} for currency in G10})


def test_a_slice_missing_a_required_series_fails_loudly(pillar: RiskPillar) -> None:
    """The same rule one step upstream, in `_transform`.

    `_extract` seeds a key for every entry in `requires`, so a slice without one
    did not come from `_extract`. Substituting an empty series would report the
    market at its 52-week high with volatility unreadable, which is a dead feed
    dressed as half a calm market.
    """
    with pytest.raises(KeyError):
        pillar._transform({"USD": {VOL: ()}}, ASOF)

    # And the other series, which needs its own case: both are read before
    # either is checked for ``None``, so a slice carrying only the equity key
    # gets past the first line and has to fail on the second.
    with pytest.raises(KeyError):
        pillar._transform({"USD": {EQUITY: ()}}, ASOF)


def test_every_currency_holds_the_same_slice_object(pillar: RiskPillar) -> None:
    """The precondition `_transform` relies on, asserted rather than assumed.

    `_transform` reads the regime from whichever slice comes to hand and gives
    the answer to all eight. That is correct only while `_extract` hands every
    currency the same object. It is not a micro-optimisation to preserve, it is
    a contract: give two currencies different slices and the first one's
    drawdown is applied to the second, silently and with no exception. A test
    that asserts identity is the honest form, because identity is what is
    relied on.
    """
    observations = [*usable_equity([120.0, 100.0]), *vol_series(steady_vol(30.0))]

    extracted = pillar._extract(observations, sorted(G10), ASOF)

    first = extracted["AUD"]
    assert all(extracted[currency] is first for currency in G10)


def test_a_short_equity_history_is_refused_rather_than_read_as_a_high() -> None:
    """One print is its own 52-week high, and 0.0 there is a confident lie.

    `_drawdown_pct` returns ``0.0`` for a market sitting at its high, which is
    the calmest reading this pillar has. A one-observation series produces that
    reading by construction rather than by observation, so the floor refuses it.
    The volatility half has refused short histories since a floor existed under
    `BasePillar.time_series_z`; this is the same idea on the other half, and
    since #214 it is deliberately a different number. That floor is now a year
    of the series' own prints, which for a daily series is `DAILY_FLOOR`. This
    one stays at twelve sessions because it answers a different question: not
    whether a standard deviation means anything, but how much of a 252-session
    window must be present before a fall from its high is worth reporting.
    """
    short = equity_series([100.0] * (MIN_DRAWDOWN_WINDOW_SESSIONS - 1))
    assert _drawdown_pct(short) is None
    assert _drawdown_pct(equity_series([100.0])) is None

    # One more observation and the same shape answers. The floor is the only
    # thing standing between the two, so neither the value nor the arithmetic
    # can be what made the difference.
    just_enough = equity_series(
        [100.0] * (MIN_DRAWDOWN_WINDOW_SESSIONS - 1) + [90.0],
    )
    assert _drawdown_pct(just_enough) == pytest.approx(-10.0)


def test_a_short_equity_history_takes_the_pillar_out_for_everybody(
    pillar: RiskPillar,
) -> None:
    """And the floor reaches the score as an absence, not as a calm reading.

    Without it this run reports every currency at 0.0 with a real ``z`` and the
    full 0.10 of weight, which is the pillar's own published statement that the
    market is at its high and calm. A partial backfill or a wiped cache is the
    likely way to arrive here.
    """
    built = run(pillar, [100.0, 99.0], steady_vol(16.0), pad_equity=False)

    for currency in G10:
        assert built[currency]["risk_response"] is None, currency
        assert built[currency]["regime"] is None, currency
        assert built[currency]["drawdown_pct"] is None, currency


def test_a_maximum_risk_off_scores_the_published_magnitudes(
    pillar: RiskPillar,
) -> None:
    """The three numbers this repository publishes about RISK, as a fixture.

    Every other magnitude assertion in this file recomputes the answer from the
    same constants the pillar read, so all of them would pass with `RISK_SCALE`
    at 7.0. These are absolute: -1.8 and +1.8 appear in `RISK_SCALE`'s own
    docstring, and 1.2728 is the emitted standard deviation at ``R = -1.0``
    published in section 7 of `docs/scoring-spec.md` and in the module docstring
    of `tests/test_emit_sd.py`, which pins only the ``R = 0`` end.

    A 16.7% fall saturates the drawdown component and a volatility print 5.7
    standard deviations high saturates the other, so ``R`` is exactly -1.0 and
    the arithmetic below is the whole of the pillar: ``2.0 * -1.0 * beta``.
    """
    observations = [*usable_equity([120.0, 100.0]), *vol_series(steady_vol(30.0))]

    scores = pillar.compute(observations, sorted(G10), ASOF)

    assert scores["AUD"].raw == pytest.approx(-1.0)
    assert scores["AUD"].score == pytest.approx(-1.8)
    assert scores["JPY"].score == pytest.approx(1.8)
    assert scores["USD"].diagnostics["emit_sd"] == pytest.approx(1.2728, abs=5e-5)


def test_the_note_shows_both_halves_of_the_regime_and_the_beta(
    pillar: RiskPillar,
) -> None:
    """The working, in full, so the sign can be checked by hand.

    This pillar's sign comes from the run rather than from the indicator, so a
    reader who sees the yen positive and the Australian dollar negative cannot
    tell from the scores whether the regime was read correctly or the betas were
    applied backwards. The whole string is asserted rather than each number in
    turn, because two unordered substring checks pass with the operands swapped.

    Worked by hand. The volatility history alternates 10 and 20 for a year of
    sessions and ends on 20, so its mean is 15.0 and its sample standard
    deviation is ``sqrt(6300/251) = 5.00995``, giving a z of
    ``5/5.00995 = +0.99801``. Halved and negated that is -0.49901. The equity
    series falls from 120 to 100, a 16.67% drawdown, which saturates its
    component at -1.0. So ``R`` is
    ``0.5 * -1.0 + 0.5 * -0.49901 = -0.74950``, and AUD's beta is +0.9.

    The history was twelve alternating values until #214 put a year of sessions
    under a daily z-score. The shape is the same and every figure moved: at
    twelve the standard deviation was ``sqrt(300/11) = 5.2223`` and the z was
    +0.9574, which rounded to +0.96 on the note and gave a regime of -0.74. The
    longer history has more values at the same two levels, so the deviation
    settles nearer 5.0 and the z nearer 1.0.
    """
    observations = [
        *usable_equity([120.0, 100.0]),
        *vol_series([10.0, 20.0] * (DAILY_FLOOR // 2)),
    ]

    scores = pillar.compute(observations, sorted(G10), ASOF)

    assert meta("AUD").risk_beta == pytest.approx(0.9)
    assert scores["AUD"].notes == (
        "drawdown -16.7%, vol z +1.00, regime -0.75, beta +0.90"
    )


def test_a_calm_market_does_not_report_a_negative_zero(pillar: RiskPillar) -> None:
    """``-0.00`` beside the yen in a calm market reads as a short it is not taking.

    At ``R = 0`` the response is ``2.0 * 0.0 * beta``, which for a negative beta
    is ``-0.0``. It compares equal to ``0.0``, so every approx assertion in this
    file accepts it, and it renders with its sign in the report and serialises
    into the JSON with its sign as well.
    """
    observations = [
        *usable_equity([100.0, 105.0, 110.0]),
        *vol_series([20.0, 10.0] * (DAILY_FLOOR // 2)),
    ]

    built = pillar._transform(pillar._extract(observations, sorted(G10), ASOF), ASOF)

    assert meta("JPY").risk_beta < 0.0
    assert built["JPY"]["regime"] == pytest.approx(0.0)
    response = built["JPY"]["risk_response"]
    assert response is not None
    assert math.copysign(1.0, response) == 1.0


def test_an_unusable_but_present_series_names_no_indicator(pillar: RiskPillar) -> None:
    """Current behaviour, pinned so the next reader knows it is not an accident.

    `BasePillar.compute` builds its reason from the `requires` keys that came
    back *empty*. A series that arrived and could not be used is not empty, so
    the two commonest ways this pillar goes dark, a flat volatility history and
    a short one, name no indicator at all. RISK is where that costs most: when
    it goes dark it goes dark for all eight at once, and the operator gets eight
    identical lines that say only that it could not score.

    Asserted rather than fixed here. The fix belongs in `fbe.pillars.base` and
    would change the note every pillar emits, which is more than this issue.
    """
    observations = [*usable_equity([120.0, 100.0]), *vol_series([16.0] * 20)]

    scores = pillar.compute(observations, sorted(G10), ASOF)

    assert scores["USD"].z is None
    assert scores["USD"].notes == "risk could not score USD"
    assert "vol_index" not in scores["USD"].notes
