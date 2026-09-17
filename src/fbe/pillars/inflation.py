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

    One more that only bites later, recorded before the backtest rather than
    after it. `CurrencyMeta.inflation_target` is a current-vintage constant and
    the targets are not constants over history: Japan adopted 2% in 2013, and
    several of these banks have re-specified their bands since. A Phase 6 run
    dated before a change would difference that currency's CPI against a target
    that did not exist yet. That is the look-ahead class in the place nobody
    checks for it, because it is a configuration constant rather than a data
    series, and closing it means giving the target a history rather than
    changing anything here.
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
                component: _gap(series[indicator], target)
                for component, (indicator,) in self.component_indicators.items()
            }
        return built

    def _notes(
        self,
        currency: str,
        components: Mapping[str, float | None],
        extracted: Mapping[str, Sequence[Observation]],
    ) -> str:
        """Say which print, from which period, against which target.

        Args:
            currency: The currency being scored.
            components: Its component values from `_transform`.
            extracted: Its slice of `_extract`'s output, read for the period of
                the print being described.

        Returns:
            The gap's two operands and the period they came from, or a line
            naming core where there is no headline print. Never raises: a note
            is cosmetic and `scoring.score_currencies` would mark the whole
            universe unscored if this threw.

        `raw` carries ``cpi_gap``, which is a difference, and a difference
        cannot be checked from itself. ``+0.5`` is consistent with a 3.0% print
        against Australia's 2.5% target and with 2.5% against 2.0%, and those
        are different economies.

        The period is named because both series are quarterly for AUD and NZD.
        "headline 3.0% against a 2.5% target" reads identically whether the
        print landed last month or five months ago, and the second is the case
        a reader needs to notice.

        A currency scored on core alone still gets a line. ``core_gap`` carries
        0.60 of the sub-weight and clears `MIN_COMPONENT_WEIGHT` by itself, so
        such a currency reaches a report with a real score and an empty ``raw``,
        which is exactly where an empty working is least affordable.

        """
        target = meta(currency).inflation_target
        for component, label in (("cpi_gap", "headline"), ("core_gap", "core")):
            gap = components.get(component)
            if gap is None:
                continue
            (indicator,) = self.component_indicators[component]
            printed = extracted.get(indicator, ())
            period = f" for {printed[-1].period.isoformat()}" if printed else ""
            carried = (
                "" if component == self.headline_component else ", no headline print"
            )
            return (
                f"{self.name.value} {currency}: {label} {gap + target:.1f}%"
                f"{period} against a {target:.1f}% target{carried}"
            )
        return ""


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
