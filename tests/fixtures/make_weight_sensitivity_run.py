"""Rebuild the run `tests/test_weight_sensitivity.py` perturbs.

Two files, and they are one artefact: ``weight_sensitivity_observations.json``
holds the inputs and ``bias-2026-03-02.json`` holds what the engine made of
them. They are committed together because a sensitivity run needs both, and a
`fbe.types.BiasReport` carries neither its observations nor a pointer to them.
That is the gap #287's sixth criterion runs into, recorded here rather than
worked around: the criterion asks for the report's own observations and the
report does not have them, so the two files are generated in one pass and a
test that loads one loads the other.

Run it from the repository root::

    python3 tests/fixtures/make_weight_sensitivity_run.py

Committed so the fixture has a definition rather than a history of hand edits,
the same reason `make_dashboard_report.py` gives. A report of this size
maintained by editing JSON drifts from the invariants it was built to carry,
and the drift is silent: every number still parses.

**Both files are produced by the real pipeline**, `fbe.scoring.score_currencies`
then `fbe.bias.build_pair_biases`, from the observations written beside them.
Nothing here writes a composite or a spread by hand. That is what lets the
fourth criterion be checked at all: rescoring the unperturbed weights has to
reproduce this report exactly, and it can only mean something if this report
came out of the same two functions the sensitivity run calls.

What the numbers are here to do
-------------------------------
They are not a forecast and they are not drawn from a real morning. They are
chosen so that the cross-section has the properties a sensitivity test needs:

* The eight currencies spread across the band rather than bunching, so a
  weight change can move a rank rather than being lost in ties.
* ``USD`` and ``JPY`` sit at opposite ends, so the widest pairs in the run are
  wide enough that no plausible perturbation flips them. A fixture where every
  pair flips measures nothing.
* Several pairs sit near a conviction boundary, so a small weight change can
  cross one. Without a pair like that the conviction count is zero for every
  perturbation and a test passes while measuring nothing.
* Every observation is fresh for its frequency, per `FRESH_PERIOD`, and
  carries a release date. A pillar past its staleness allowance carries no
  weight, so a stale input removes a pillar from the measurement as surely as
  an absent one and folds two effects into one number.

**Four of the seven pillars score on this run and three do not.** MONETARY,
INFLATION, GROWTH and EXTERNAL each take a value from one morning's figures.
EMPLOYMENT, POSITIONING and RISK need a history rather than a point, which a
single-period fixture cannot give them, so they carry weight 0.0 and moving
their weights changes nothing. That is a true property of a run with no data
for them rather than a fault in the fixture, and
``test_a_pillar_that_scored_nothing_reports_zero_rather_than_a_guess`` pins it,
because the alternative reading is that the model does not need those pillars.

Units, frequencies and sign conventions are the registry's, not this file's:
`_observation` reads `fbe.datasources.registry` for each key, and positive
means currency-strengthening everywhere, which for unemployment means the
pillar inverts the raw figure rather than this file doing it.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fbe.bias import build_pair_biases  # noqa: E402
from fbe.config import Config  # noqa: E402
from fbe.datasources.registry import INDICATORS  # noqa: E402
from fbe.pillars import default_pillars  # noqa: E402
from fbe.scoring import score_currencies  # noqa: E402
from fbe.types import BiasReport, Frequency, Observation  # noqa: E402
from fbe.universe import G10  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

HERE = Path(__file__).resolve().parent

ASOF = date(2026, 3, 2)
"""The run's as-of date. A Monday, so a reader checking the fixture against a
calendar is not distracted by a date the engine would not have run on."""

PERIOD = date(2026, 2, 1)
"""Period the observations describe, the month before the run."""

RELEASED_AT = datetime(2026, 2, 20, 12, 0, tzinfo=UTC)
"""Release timestamp, before the as-of so every figure is visible to the run.

Stamped rather than left absent because an observation with no release date is
admitted on an assumed lag and counted as resting on that assumption, which is
a second thing varying across the fixture when only the weights should vary.
"""

GENERATED_AT = datetime(2026, 3, 2, 5, 9, tzinfo=UTC)
"""Fixed so the file does not change when nothing about the run changed."""

SERIES: Mapping[str, Mapping[str, float]] = {
    # MONETARY. A spread of policy stances, with the two rate-change series
    # disagreeing with the level for CAD and NZD so the pillar's own blend has
    # something to resolve rather than four copies of one ranking.
    "policy_rate": {
        "USD": 4.75,
        "EUR": 2.40,
        "GBP": 4.10,
        "JPY": 0.35,
        "CHF": 0.90,
        "CAD": 3.05,
        "AUD": 3.85,
        "NZD": 3.20,
    },
    "yield_2y": {
        "USD": 4.40,
        "EUR": 2.05,
        "GBP": 3.95,
        "JPY": 0.60,
        "CHF": 0.75,
        "CAD": 2.85,
        "AUD": 3.70,
        "NZD": 3.05,
    },
    "yield_2y_chg_1m": {
        "USD": 12.0,
        "EUR": -4.0,
        "GBP": 9.0,
        "JPY": 6.0,
        "CHF": -2.0,
        "CAD": -11.0,
        "AUD": 7.0,
        "NZD": -14.0,
    },
    "yield_2y_chg_3m": {
        "USD": 28.0,
        "EUR": -9.0,
        "GBP": 21.0,
        "JPY": 17.0,
        "CHF": -6.0,
        "CAD": -24.0,
        "AUD": 15.0,
        "NZD": -31.0,
    },
    # INFLATION. Core below headline everywhere, by different amounts, so the
    # pillar's two components do not rank the universe identically.
    "cpi_yoy": {
        "USD": 3.10,
        "EUR": 2.00,
        "GBP": 3.60,
        "JPY": 1.40,
        "CHF": 0.40,
        "CAD": 2.30,
        "AUD": 3.00,
        "NZD": 2.45,
    },
    "core_cpi_yoy": {
        "USD": 2.80,
        "EUR": 2.20,
        "GBP": 3.10,
        "JPY": 1.10,
        "CHF": 0.60,
        "CAD": 2.45,
        "AUD": 2.60,
        "NZD": 2.15,
    },
    # GROWTH.
    "gdp_yoy": {
        "USD": 2.40,
        "EUR": 0.70,
        "GBP": 1.10,
        "JPY": 0.60,
        "CHF": 1.30,
        "CAD": 1.60,
        "AUD": 2.00,
        "NZD": 0.90,
    },
    "business_confidence_mfg": {
        "USD": 52.5,
        "EUR": 46.8,
        "GBP": 48.9,
        "JPY": 49.6,
        "CHF": 51.2,
        "CAD": 50.4,
        "AUD": 51.8,
        "NZD": 47.5,
    },
    "indpro_yoy": {
        "USD": 1.90,
        "EUR": -0.80,
        "GBP": 0.40,
        "JPY": 1.20,
        "CHF": 2.10,
        "CAD": 0.90,
        "AUD": 1.50,
        "NZD": -0.30,
    },
    "retail_sales_yoy": {
        "USD": 3.20,
        "EUR": 1.10,
        "GBP": 2.20,
        "JPY": 0.80,
        "CHF": 1.70,
        "CAD": 2.60,
        "AUD": 2.90,
        "NZD": 1.40,
    },
    # EMPLOYMENT. Falling unemployment is currency-positive, so the pillar
    # inverts this; the raw figures are published as published.
    "unemployment_rate": {
        "USD": 4.10,
        "EUR": 6.40,
        "GBP": 4.70,
        "JPY": 2.45,
        "CHF": 2.80,
        "CAD": 6.55,
        "AUD": 4.05,
        "NZD": 5.10,
    },
    "employment_chg": {
        "USD": 185000.0,
        "EUR": 96000.0,
        "GBP": 42000.0,
        "JPY": 31000.0,
        "CHF": 8000.0,
        "CAD": 27000.0,
        "AUD": 39000.0,
        "NZD": 4000.0,
    },
    "employment_level": {
        "USD": 161_400_000.0,
        "EUR": 168_900_000.0,
        "GBP": 33_200_000.0,
        "JPY": 67_800_000.0,
        "CHF": 5_300_000.0,
        "CAD": 20_600_000.0,
        "AUD": 14_500_000.0,
        "NZD": 2_900_000.0,
    },
    # EXTERNAL.
    "current_account_gdp": {
        "USD": -3.20,
        "EUR": 2.60,
        "GBP": -2.90,
        "JPY": 3.40,
        "CHF": 6.10,
        "CAD": -1.20,
        "AUD": 1.40,
        "NZD": -5.80,
    },
    "trade_balance": {
        "USD": -71_000_000_000.0,
        "EUR": 24_000_000_000.0,
        "GBP": -18_000_000_000.0,
        "JPY": 5_000_000_000.0,
        "CHF": 4_000_000_000.0,
        "CAD": -2_000_000_000.0,
        "AUD": 6_000_000_000.0,
        "NZD": -1_000_000_000.0,
    },
    "gdp_nominal_usd": {
        "USD": 29_200_000_000_000.0,
        "EUR": 16_100_000_000_000.0,
        "GBP": 3_600_000_000_000.0,
        "JPY": 4_200_000_000_000.0,
        "CHF": 950_000_000_000.0,
        "CAD": 2_300_000_000_000.0,
        "AUD": 1_800_000_000_000.0,
        "NZD": 270_000_000_000.0,
    },
    # POSITIONING. Net speculative position as a share of open interest, so
    # a crowded long is a headwind rather than a tailwind.
    "cot_net_pct_oi": {
        "USD": 18.0,
        "EUR": -9.0,
        "GBP": 6.0,
        "JPY": -22.0,
        "CHF": -14.0,
        "CAD": -4.0,
        "AUD": 11.0,
        "NZD": 2.0,
    },
}
"""Per-currency inputs, one entry per indicator the seven pillars require.

Every pillar is fed. A run in which one pillar scores and six do not is useless
to a weight-sensitivity measurement and not obviously so: the composite is then
that pillar's score whatever the weights are, because the weights of the six
that scored nothing renormalise away, and every perturbation reports zero. The
first version of this fixture carried four indicators and did exactly that.

The values are not a forecast and not drawn from a real morning. They are
chosen so the cross-section has what the measurement needs: eight currencies
spread across the band rather than bunched, ``USD`` and ``JPY`` at opposite
ends so the widest pairs do not flip on any plausible step, and at least one
pair near a conviction boundary so a small weight change can cross it.

Units are the registry's, read from `fbe.datasources.registry` at build time
rather than written here, so a unit change in the registry moves the fixture
rather than silently disagreeing with it.
"""

GLOBAL_SERIES: Mapping[str, float] = {
    "vol_index": 18.6,
    "world_equity_index": 4185.0,
}
"""Cross-market series, which carry ``GLOBAL`` rather than a currency.

The RISK pillar reads both. They are one value for the whole universe by
construction, so a currency code on them would be a false claim about what the
number describes.

``commodity_price`` is not here, and the first version of this file had it
here, which was wrong in a way that cost three currencies a component without
saying so. `fbe.datasources.registry` keys that indicator by currency, and
`fbe.pillars.external` reads one currency's own prices against its
`CurrencyMeta.commodity_link`. A single GLOBAL print is invisible to all
eight, so CAD, AUD and NZD scored EXTERNAL without their terms-of-trade
component and came out a tenth lower on coverage than the other five.
"""

COMMODITY_PRICES: Mapping[str, tuple[float, float]] = {
    "CAD": (94.0, 101.2),
    "AUD": (118.0, 112.6),
    "NZD": (131.0, 134.9),
}
"""The linked commodity, three months back and now, for the three that have one.

USD, EUR, GBP, JPY and CHF carry no ``commodity_link`` and take a deliberate
0.0 on the component, which `fbe.pillars.external._terms_of_trade` documents
as a modelling statement rather than an absence. Giving them a price here
would not change that, so they have none.

Crude is falling and iron ore is falling while dairy rises, so the component
does not rank the three the same way the rate differentials do. A fixture
where every component agrees cannot show a weight mattering.
"""

UNEMPLOYMENT_6M_AGO: Mapping[str, float] = {
    "USD": 4.45,
    "EUR": 6.20,
    "GBP": 4.40,
    "JPY": 2.55,
    "CHF": 2.60,
    "CAD": 6.10,
    "AUD": 4.30,
    "NZD": 4.70,
}
"""The unemployment rate six months before the run.

`fbe.pillars.employment` scores the six-month change and inverts it, so a
falling rate is currency-positive. Without a second print the pillar has no
change to score, carries no weight, and takes every currency below the
``coverage_demotion`` threshold with it.

USD, JPY and AUD are falling and the rest are rising, so the pillar disagrees
with the monetary ranking rather than repeating it.
"""

HISTORY_MONTHS: Mapping[str, int] = {
    "employment_chg": 4,
}
"""Indicators needing several prints, and how many months of them.

`fbe.pillars.employment` totals three months of hiring, so one print leaves the
component unscored and the pillar with it, since its two components are
weighted equally and one alone sits at the floor.

Four rather than three, and the fourth is not spare. ``employment_chg`` is a
diff series, so each value is the change since the print before it, and
`fbe.pillars.employment._hiring_trend` refuses a window whose oldest print has
no predecessor one step earlier: without it there is no way to tell whether
that value spans one month or the hole before it. Three prints starting exactly
at the window boundary are refused for that reason, which is what the first
version of this fixture supplied.

The same value is repeated across the window. The fixture is exercising the
weight, not the shape of a hiring cycle, and a drifting series here would put a
second moving part into a measurement that should have one.
"""

FRESH_PERIOD: Mapping[Frequency, date] = {
    Frequency.DAILY: date(2026, 2, 27),
    Frequency.WEEKLY: date(2026, 2, 24),
    Frequency.MONTHLY: date(2026, 2, 1),
    Frequency.QUARTERLY: date(2026, 1, 1),
    Frequency.ANNUAL: date(2025, 1, 1),
    Frequency.IRREGULAR: date(2026, 2, 1),
}
"""The period each frequency's figure describes, chosen so nothing is stale.

A pillar past its staleness allowance carries no weight, which removes it from
the measurement as surely as having no data at all. One period per frequency
rather than one for everything, because a quarterly figure stamped with a daily
period would be fresher than any real one and a daily figure stamped with a
quarterly period would be stale on arrival.
"""


def _observation(
    indicator: str, currency: str, value: float, *, months_back: int = 0
) -> Observation:
    """One observation, with its unit and frequency taken from the registry.

    Args:
        indicator: A canonical indicator key.
        currency: The ISO code, or ``GLOBAL`` for a cross-market series.
        value: The published figure, in the registry's unit for that key.
        months_back: How many whole months before the fresh period this
            print describes. Whole months because the pillars that read a
            window measure it in calendar months, so a print offset by
            thirty days would land inside or outside a six-month window
            depending on which months it crossed.

    Returns:
        The observation, stamped with the period `FRESH_PERIOD` gives for its
        frequency and with a release date before the as-of.

    Unit and frequency are read rather than written, so a registry change moves
    the fixture instead of leaving it asserting against a unit the engine no
    longer uses.
    """
    meta = INDICATORS[indicator]
    period = FRESH_PERIOD[meta.frequency]
    if months_back:
        month = period.month - months_back
        year = period.year + (month - 1) // 12
        period = period.replace(year=year, month=(month - 1) % 12 + 1)
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="fixture",
        series_id=f"{indicator.upper()}_{currency}",
        # Release date tracks the period rather than being fixed, so an
        # older print is not stamped as having been published after a
        # newer one. A window read on release order would otherwise take
        # the six-month-old rate as the current one.
        unit=meta.unit,
        frequency=meta.frequency,
        released_at=RELEASED_AT,
    )


def observations() -> tuple[Observation, ...]:
    """Every observation the run was scored from, in a fixed order.

    Returns:
        Each per-currency series for each of the eight G10 currencies, in
        currency-major order, then the cross-market series. The order is
        stable so the JSON diff of a regenerated fixture shows what moved
        rather than a reshuffle.
    """
    rows: list[Observation] = []
    for currency in G10:
        for indicator, by_currency in SERIES.items():
            months = HISTORY_MONTHS.get(indicator, 1)
            for back in range(months):
                rows.append(
                    _observation(
                        indicator,
                        currency,
                        by_currency[currency],
                        months_back=back,
                    )
                )
        rows.append(
            _observation(
                "unemployment_rate",
                currency,
                UNEMPLOYMENT_6M_AGO[currency],
                months_back=6,
            )
        )
        if currency in COMMODITY_PRICES:
            then, now = COMMODITY_PRICES[currency]
            rows.append(_observation("commodity_price", currency, then, months_back=3))
            rows.append(_observation("commodity_price", currency, now))
    for indicator, value in GLOBAL_SERIES.items():
        rows.append(_observation(indicator, "GLOBAL", value))
    return tuple(rows)


def _observation_payload(observation: Observation) -> dict[str, object]:
    """One observation as JSON, with every field written out.

    Nothing is defaulted on the way out or on the way back. A field omitted
    here would be filled from the dataclass default when the fixture is read,
    which invents a number that was never in the run.
    """
    return {
        "indicator": observation.indicator,
        "currency": observation.currency,
        "value": observation.value,
        "period": observation.period.isoformat(),
        "source": observation.source,
        "series_id": observation.series_id,
        "unit": observation.unit,
        "frequency": observation.frequency.value,
        "released_at": (
            None
            if observation.released_at is None
            else observation.released_at.isoformat()
        ),
        "revision": observation.revision,
    }


def no_events(currency: str, asof: date) -> bool:
    """Answer that no high-impact release sits inside the next 24 hours.

    Args:
        currency: The leg being asked about. Unused: the answer is the same
            for the whole universe here.
        asof: The run date. Unused, for the same reason.

    Returns:
        Always ``False``, which `fbe.bias.build_pair_biases` reads as "no
        event", so no pair takes the 24-hour conviction cap.

    This changes nothing about the output and is here to be explicit rather
    than to have an effect. `fbe.bias._events_within_24h` reads an absent
    guard as ``False`` for every currency, so a run built with no guard and a
    run built with this one are identical. What it does is make the fixture
    say which it is, and give the test that rescores under a capping guard
    something to contrast against.

    A fixture is not a claim that the calendar was empty that morning. It is
    a statement that this run is scored as a backtest is scored, with no
    calendar, which is what a committed cross-section with no events beside
    it means.
    """
    return False


def build(config: Config, rows: Sequence[Observation]) -> BiasReport:
    """Score the observations and difference them into the 28 pairs.

    Args:
        config: The packaged configuration. Passed in rather than constructed
            here so the caller can see that the fixture is built against the
            committed defaults and nothing else.
        rows: The observations, from `observations`.

    Returns:
        The report, with no shortlist and no events. The sensitivity run reads
        pairs and currencies and nothing else, and a calendar in the fixture
        would be a second thing to keep current for no gain.
    """
    scores = score_currencies(
        rows, default_pillars(config.scoring), config.scoring, ASOF
    )
    pairs = build_pair_biases(scores, config, ASOF, event_horizon_guard=no_events)
    return BiasReport(
        asof=ASOF,
        generated_at=GENERATED_AT,
        currencies=tuple(scores),
        pairs=tuple(pairs),
        config_digest=config.digest(),
    )


def main() -> None:
    from fbe.report import write_report

    config = Config()
    rows = observations()

    (HERE / "weight_sensitivity_observations.json").write_text(
        json.dumps([_observation_payload(row) for row in rows], indent=2) + "\n"
    )

    report = build(config, rows)
    written = write_report(report, HERE, config=config)
    # The Markdown is for a reader and this fixture has none; only the sidecar
    # is loaded back, so the rendered half is removed rather than committed and
    # left to drift against a template nobody renders it with.
    written.unlink()

    print(f"wrote {HERE / 'weight_sensitivity_observations.json'}")
    print(f"wrote {HERE / 'bias-2026-03-02.json'}")


if __name__ == "__main__":
    main()
