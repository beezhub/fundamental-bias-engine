"""Inflation pillar: distance from target, in headline and core terms.

Weight 0.15. The pillar never scores the level of inflation. A 3.0% print is 1.0
point above target in the United States, the euro area, the United Kingdom,
Japan, Canada and New Zealand; 0.5 points above target in Australia, whose target
is the midpoint of a 2-3% band; and 2.0 points above target in Switzerland, where
the Swiss National Bank aims below 2%. Those are three different policy problems
and therefore three different currency implications. What travels across a
cross-section is the gap between inflation and the number that currency's central
bank has committed to, which is what `CurrencyMeta.inflation_target` holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName
from fbe.universe import meta

__all__ = ["InflationPillar"]


class InflationPillar(BasePillar):
    """Score how far headline and core inflation sit from target.

    Components and sub-weights:
        ``cpi_gap`` (0.40): ``cpi_yoy - inflation_target`` in percentage points.
        ``core_gap`` (0.60): ``core_cpi_yoy - inflation_target``, in percentage
            points.

    Core carries the larger share because it is the series central banks act on.
    Headline is retained because it drives household inflation expectations and,
    through them, the political pressure on the bank, which is a real channel
    even where the committee says it is looking through the number.

    Sign rule: above target is currency-positive, below target is
    currency-negative. Neither component is inverted. Inflation reaches an
    exchange rate almost entirely through the policy channel: an overshoot
    obliges the central bank to hold policy tight or tighten further, and the
    market prices that in the front end.

    The credibility caveat, and where it is handled. Above-target inflation is
    currency-positive only while the central bank is expected to respond to it.
    Where a bank is credibly and deliberately tolerating an overshoot, the same
    print is currency-negative: inflation erodes the real return on the currency
    while the nominal rate does not rise to compensate, and the yen through the
    yield curve control years is the reference case. This pillar does not model
    that switch. The ``real_policy_rate`` term inside the monetary pillar catches
    part of it, since a tolerated overshoot shows up as a falling real rate, and
    it is a partial fix rather than a complete one. Making it explicit is an open
    question in section 10 of ``docs/scoring-spec.md``, and it is deliberately
    left there rather than solved locally: a credibility gate built from the
    front end would be reading a market price that also moves for global reasons,
    and getting that wrong would flip the sign of a 0.15-weight pillar on a
    signal that is not purely domestic.

    The pillar's weight is not the model's inflation response. That
    ``real_policy_rate`` term puts a coefficient of minus one on ``cpi_yoy``, the
    same series ``cpi_gap`` loads on positively, so the two partly cancel. On the
    fixture in section 7 of ``docs/scoring-spec.md`` the cancellation is 49.7%
    when headline moves alone and 18.7% when headline and core move together.
    Section 3.2 publishes the loadings, `scoring.series_loading` computes them,
    and ADR 0003 records why the opposing term was kept rather than removed.

    Known failure modes: the sign assumption inverts in a stagflation, where
    inflation is high, growth is collapsing, and the market prices cuts anyway.
    The pillar reads the inflation as currency-positive at exactly the moment the
    currency is being sold. The growth pillar partially offsets this by moving
    the other way, which is one reason the two carry equal weight. Separately,
    year-on-year rates carry base effects: the gap moves when a large print from
    twelve months ago drops out of the window and nothing has happened this
    month. And the targets in `CurrencyMeta` are single numbers where several of
    these banks publish bands, so the AUD gap is measured against a 2.5% midpoint
    the RBA does not literally target.
    """

    name = PillarName.INFLATION
    requires: Sequence[str] = ("cpi_yoy", "core_cpi_yoy")
    headline_component = "cpi_gap"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "cpi_gap": ("cpi_yoy",),
        "core_gap": ("core_cpi_yoy",),
    }
    """Each gap ages with the print it is built from.

    The target it is differenced against is a constant in `CurrencyMeta` and
    cannot go stale, so the age of the component is the age of the CPI release.
    Both series are quarterly for AUD and NZD, which is the case that exposed
    the ramp.
    """

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.2 of
            ``docs/scoring-spec.md``.

        """
        return {"cpi_gap": 0.40, "core_gap": 0.60}

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Pull headline and core CPI per currency.

        Args:
            observations: Full observation set for the run.
            currencies: Universe to score.
            asof: Run date; later periods are dropped.

        Returns:
            ``{currency: {indicator: observations}}`` for the two indicators in
            `requires`, sorted by period ascending.

        """
        wanted = set(self.requires)
        per_currency: dict[str, dict[str, list[Observation]]] = {
            currency: {indicator: [] for indicator in self.requires}
            for currency in currencies
        }
        for observation in observations:
            if observation.indicator not in wanted:
                continue
            series = per_currency.get(observation.currency)
            if series is None or observation.period > asof:
                continue
            # The shared rule, not a copy of it. `_visible` and
            # `_newest_vintages` are `BasePillar`'s, so #121's fix to the
            # unstamped-revision hole reaches this pillar without anyone
            # remembering it exists.
            if not self._visible(observation, asof):
                continue
            series[observation.indicator].append(observation)
        return {
            currency: {
                indicator: self._newest_vintages(found)
                for indicator, found in series.items()
            }
            for currency, series in per_currency.items()
        }

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Difference the newest prints against each currency's own target.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}`` in percentage points, positive
            above target. Both values are plain differences against the target,
            with nothing else applied to them, which is what lets ``cpi_gap``
            serve as `headline_component` and reach a report as
            ``PillarScore.raw``. A reader seeing ``+0.9`` there is seeing
            inflation nine tenths of a point above the bank's own target, and
            ``notes`` should carry the level and the target alongside it so the
            number can be checked.

        Each currency's target comes from ``universe.meta(currency)``, so a
        currency outside the scored universe raises rather than silently
        defaulting to 2%. A currency missing ``core_cpi_yoy`` holds only 0.40 of
        the sub-weight, which is under `MIN_COMPONENT_WEIGHT`, so it is scored as
        missing: headline alone is too noisy to carry this pillar.

        """
        built: dict[str, dict[str, float | None]] = {}
        for currency, series in extracted.items():
            # Indexed rather than fetched with a default. Six of the eight
            # targets are 2.0, so a default would be right often enough never
            # to be noticed and wrong for exactly the two currencies that make
            # this pillar worth building.
            target = meta(currency).inflation_target
            built[currency] = {
                component: _gap(series.get(indicator, ()), target)
                for component, (indicator,) in self.component_indicators.items()
            }
        return built

    def _notes(
        self,
        currency: str,
        components: Mapping[str, float | None],
        asof: date,
    ) -> str:
        """Say which print and which target produced this currency's gap.

        Args:
            currency: The currency being scored.
            components: Its component values from `_transform`.
            asof: Run date, unused here: the note describes the newest print
                rather than the run.

        Returns:
            The headline gap's two operands, or the empty string when there is
            no headline gap to explain.

        `raw` carries ``cpi_gap``, which is a difference, and a difference
        cannot be checked from itself. ``+0.5`` is consistent with a 3.0% print
        against Australia's 2.5% target and with 2.5% against 2.0%, and those
        are different economies. Reconstructing the level from the gap needs
        the target, which means opening `CurrencyMeta`, so the note carries
        both and the reader does not have to.

        """
        gap = components.get(self.headline_component)
        if gap is None:
            return ""
        target = meta(currency).inflation_target
        return (
            f"{self.name.value} {currency}: headline {gap + target:.1f}% "
            f"against a {target:.1f}% target"
        )


def _gap(found: Sequence[Observation], target: float) -> float | None:
    """Difference the newest print against the target, in percentage points.

    Args:
        found: One currency's visible observations of one CPI series, oldest
            period first, as `_extract` leaves them.
        target: That currency's own `CurrencyMeta.inflation_target`, in percent.

    Returns:
        ``newest - target`` in percentage points, positive above target, or
        ``None`` when the currency has no visible print. Never ``0.0`` for an
        absence: a zero gap is a currency sitting exactly on its target, which
        is a finding rather than a gap in the data, and the two would be
        indistinguishable.

    The last element is the newest because `_extract` sorts by period
    ascending and keeps one observation per period.

    """
    if not found:
        return None
    return found[-1].value - target
