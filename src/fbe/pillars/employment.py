"""Employment pillar: labour market direction, not labour market level.

Weight 0.10. Employment matters to FX mainly as an input to the policy
reaction function, which is why it is weighted below growth and well below the
rates pillar it ultimately feeds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import fsum

from fbe.pillars.base import BasePillar
from fbe.types import Observation, PillarName

__all__ = [
    "EmploymentPillar",
    "UNEMPLOYMENT_WINDOW_MONTHS",
    "HIRING_WINDOW_MONTHS",
    "MONTHS_PER_YEAR",
]


UNEMPLOYMENT_WINDOW_MONTHS: int = 6
"""Months spanned by the unemployment change, measured from the newest print.

Six rather than one because the unemployment rate is a slow, heavily smoothed
survey and a single month of change is mostly sampling noise. Section 3.4 of
``docs/scoring-spec.md`` is the source of the number.

Months, not observations. CHF and NZD publish quarterly, so six observations
would reach eighteen months back for them and six for everyone else, and the
cross-section would then compare a year and a half of one country's labour
market with half a year of another's. The window is anchored on the newest
visible print rather than on ``asof``, so a currency whose release is late
measures six months of its own data instead of a longer span ending today.
"""

HIRING_WINDOW_MONTHS: int = 3
"""Months of hiring summed before annualising, measured the same way.

Three because a single employment print is noisy and revision-prone, and a
quarter is the shortest span over which the direction is worth reading. It is a
whole number of months for the same reason the window above is: a quarterly
publisher contributes exactly one print to it and a monthly publisher exactly
three, so both describe the same span of time.
"""

MONTHS_PER_YEAR: int = 12
"""Months in a year, used to annualise the hiring window.

Named rather than written as ``12`` at the point of use because it appears
beside `HIRING_WINDOW_MONTHS` in the same expression, and a bare ``12`` next to
a bare ``3`` invites a reader to wonder which of them is the window.
"""


class EmploymentPillar(BasePillar):
    """Score the direction of unemployment and the momentum of hiring.

    Components and sub-weights:
        ``unemployment_6m`` (0.50): the six-month change in the unemployment
            rate in percentage points, multiplied by ``-1``. Six months rather
            than one because the unemployment rate is a slow, heavily smoothed
            series and a single month of change is mostly survey noise.
        ``employment_trend`` (0.50): the three-month total change in
            ``employment_chg``, annualised and expressed as a percent of the
            employment level. A total rather than an average because a
            quarterly publisher contributes one print to the window and a
            monthly one contributes three, and the two must describe the same
            span; the annualisation then divides by the window either way.

    Why no level component. Unemployment levels across the G10 measure labour
    market institutions rather than the cycle. Japan sits near 2.5% and the euro
    area near 6.5% in good years and bad, so ranking the eight on the level
    ranks their hiring and firing law, which does not trade. The cycle lives
    entirely in the change, so only the change is scored.

    Why hiring is expressed as an annualised percent. The underlying series is
    published in raw counts and the counts are not comparable: a US payrolls
    print is in the hundreds of thousands and a New Zealand quarterly employment
    change is in the thousands. Dividing by the employment level and annualising
    turns both into "the labour force grew at this rate", which is directly
    comparable across the cross-section and needs no per-currency rebasing before
    the z-score. Doing it in the units rather than through a second normalisation
    step also keeps the number readable in a report: 1.3% annualised hiring means
    something to a person, and a time-series z-score of 1.3 does not.

    Sign rule: positive means the labour market is tightening relative to the
    others, which is currency-positive through the expected policy path. The
    inversion on ``unemployment_6m`` is the only sign flip in the pillar: a
    falling unemployment rate is a rising score.

    Known failure modes: employment lags the cycle. By the time the unemployment
    rate has turned, the rate market has usually finished repricing, so the
    pillar tends to confirm what the monetary pillar already said rather than
    adding information, which is why it carries only 0.10.

    Then the participation trap. Unemployment can fall because people stop
    looking for work, which this pillar reads as strength when it is the
    opposite. ``employment_trend`` is the partial guard, since a
    participation-driven fall shows no hiring behind it, and the two components
    disagreeing is itself informative: it lands as elevated ``dispersion`` on the
    `CurrencyScore` and cuts conviction downstream.

    Last, frequency, and the two components do not share it. The unemployment
    rate is quarterly for CHF and NZD and monthly for the other six. Employment
    is quarterly for EUR, GBP, CHF and NZD and monthly for USD, JPY, CAD and
    AUD. GBP is therefore mixed, reading a monthly rate against a quarterly
    hiring series, and it is the only currency where the two components age on
    different cadences.

    A fixed number of periods would therefore mean a different span of time for
    almost every currency, and a different span for the two halves of the same
    currency. Both horizons below are stated and measured in calendar months for
    that reason, so six months is six months everywhere and the cross-section
    compares like with like.
    """

    name = PillarName.EMPLOYMENT
    requires: Sequence[str] = (
        "unemployment_rate",
        "employment_chg",
        "employment_level",
    )
    headline_component = "unemployment_chg_6m"

    component_indicators: Mapping[str, tuple[str, ...]] = {
        "unemployment_6m": ("unemployment_rate",),
        "employment_trend": ("employment_chg", "employment_level"),
    }
    """The two components do not share a cadence, and neither do all currencies
    within one. The rate is quarterly for CHF and NZD; employment is quarterly
    for EUR, GBP, CHF and NZD. GBP reads a monthly rate against a quarterly
    hiring series and is the only mixed one.

    Every currency is aged against the same allowance regardless, so a quarterly
    publisher simply sits further along the ramp than a monthly one. That is the
    honest reading of a series that publishes four times a year rather than a
    penalty, and `employment_level` carries `employment_chg`'s allowance for the
    same reason: the same release carries both.
    """

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Return the sub-weights used to blend this pillar's components.

        Returns:
            ``{component: weight}`` summing to 1.0, matching section 3.4 of
            ``docs/scoring-spec.md``. The two components are weighted equally
            because neither guards against the other's failure mode on its own.

        """
        return {"unemployment_6m": 0.50, "employment_trend": 0.50}

    # `_extract` is `BasePillar`'s, deliberately not overridden, and the stub's
    # docstring here asked for something else. It specified a monthly grid with
    # quarterly publishers carried forward, so that a fixed number of periods
    # would mean a fixed number of months. The sixth acceptance criterion on
    # #157 asks for the same guarantee by the opposite route: leave the series
    # on its own cadence and measure the window in months. Both give the same
    # component value; they differ in what reaches the reader.
    #
    # The carry-forward loses. `compute` puts everything `_extract` returns into
    # `PillarScore.inputs` and measures `staleness_days` over it, so inventing
    # two observations per quarter would put rows in the audit trail that no
    # statistics office published and would make a quarterly currency look
    # fresher than it is. ADR 0002 is about representing what is not known
    # rather than filling it in. The windows below therefore work in months and
    # this pillar reads three per-currency series in exactly the shape the base
    # implementation serves. The decision is recorded on #157 rather than only
    # here, because it is a departure from the stub.

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Build the inverted unemployment change and the hiring trend.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}`` over three keys, two of which
            carry sub-weight:

                ``unemployment_6m``: the six-month change in percentage points
                with the sign already flipped, so a positive value means
                unemployment fell. Weighted.

                ``employment_trend``: an annualised percent of the employment
                level. Weighted.

                ``unemployment_chg_6m``: the same six-month change in percentage
                points with its natural published sign, so a positive value means
                unemployment rose. Report-only, no sub-weight, and it exists
                solely to fill `headline_component`.

        The sign flip on unemployment is the single flip in this pillar and is
        applied here, once, never again downstream.

        Why the unflipped copy exists. ``PillarScore.raw`` is contracted to be
        the pillar's headline number in its natural unit, and a report shows it
        so a reader can check the reasoning. Putting the flipped value there
        prints ``+0.3`` beside a currency whose unemployment rate rose 0.3 points,
        which reads as good news about a bad number. The flip is a modelling
        step and belongs to the score; ``raw`` should show what the statistics
        office published. This is the same device the positioning and risk
        pillars use to report a readable quantity alongside a derived one.

        With the components weighted equally at 0.50, either one missing leaves
        exactly `MIN_COMPONENT_WEIGHT` present, and the floor is "at or below",
        so a currency with only one of the two series is scored as missing rather
        than on that series alone. There is therefore no partial state for this
        pillar: a currency has both components or it has none of the pillar, and
        the shortfall reaches the reader as reduced coverage on the
        `CurrencyScore` instead of as a full-weight score built on half the
        evidence. That is the point of the equal weighting. Neither component
        guards against the other's failure mode, so half of this pillar is not
        better than none of it.

        Neither window is measured from ``asof``. Both are anchored on the
        currency's own newest visible print, so a country whose release is two
        months late reports six months of its own labour market rather than
        eight months ending today. Age is not this method's concern: a late
        print arrives here as the reading it is and
        `BasePillar.component_freshness` discounts it, which is the distinction
        `blend_components` relies on when it measures `MIN_COMPONENT_WEIGHT` on
        the sub-weight present rather than on the sub-weight still fresh.

        """
        return {
            currency: self._components(series) for currency, series in extracted.items()
        }

    def _components(
        self,
        series: Mapping[str, Sequence[Observation]],
    ) -> dict[str, float | None]:
        """Build one currency's three values from its three series.

        Args:
            series: That currency's observations, keyed by indicator, oldest
                period first, as `_extract` leaves them.

        Returns:
            The three keys `_transform` documents. Every one of them is ``None``
            rather than ``0.0`` when it cannot be measured, because zero is a
            reading in both units: an unemployment rate that did not move over
            six months, and a workforce that neither grew nor shrank.

        Indexed rather than fetched with a default. `_extract` seeds a key for
        every entry in `requires`, so a missing one means the mapping was built
        some other way, and an empty series in its place would report an outage
        as a labour market that stood perfectly still.

        """
        published = _change_over_months(
            series["unemployment_rate"], UNEMPLOYMENT_WINDOW_MONTHS
        )
        return {
            # The single sign flip in this pillar, applied once, here.
            "unemployment_6m": None if published is None else -published,
            "employment_trend": _hiring_trend(
                series["employment_chg"], series["employment_level"]
            ),
            "unemployment_chg_6m": published,
        }


def _months_between(earlier: date, later: date) -> int:
    """Whole calendar months from ``earlier`` to ``later``, ignoring the day.

    Args:
        earlier: The older period stamp.
        later: The newer period stamp.

    Returns:
        The count of month boundaries crossed, so 2026-03-01 to 2026-09-01 is
        ``6``. Negative when the arguments are the wrong way round.

    Calendar months rather than days, because a six-month window counted in days
    would be 181 days across one part of the year and 184 across another, and
    the boundary would then land inside a monthly series in some quarters and
    outside it in others.

    """
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def _change_over_months(found: Sequence[Observation], months: int) -> float | None:
    """Return the change in a series over a whole number of calendar months.

    Args:
        found: One currency's visible observations of one series, oldest period
            first, as `_extract` leaves them.
        months: Width of the window, anchored on the newest observation.

    Returns:
        ``newest - earlier`` in the series' own published unit, which for
        ``unemployment_rate`` is percentage points, so a rate falling from 5.0
        to 4.4 gives ``-0.6``. The sign is the published one and no inversion
        happens here.

        ``None`` when the series is empty, or when it holds no observation
        exactly ``months`` before the newest one.

        Never ``0.0`` for an absence. Zero is the reading for a rate that did
        not move, which is a finding about a flat labour market, so an outage
        filed under the same value would put that currency in the middle of the
        cross-section on the strength of having no data.

    Why the match is exact rather than nearest. A series with a hole where the
    window opens would otherwise be measured against whatever print happened to
    survive, over a span the caller did not ask for and cannot see. Every series
    this pillar reads is stamped on the first of its period, so an exact match
    is the normal case and its absence means the series is not what the caller
    thinks it is.

    """
    if not found:
        return None
    newest = found[-1]
    for entry in reversed(found):
        if _months_between(entry.period, newest.period) == months:
            return newest.value - entry.value
    return None


def _hiring_trend(
    flow: Sequence[Observation],
    stock: Sequence[Observation],
) -> float | None:
    """Return hiring over the trailing window as an annualised percent of employment.

    Args:
        flow: One currency's visible ``employment_chg`` observations, oldest
            first. Each is a change in persons over that series' own period.
        stock: The same currency's ``employment_level`` observations. Only the
            newest is read.

    Returns:
        ``total * MONTHS_PER_YEAR / HIRING_WINDOW_MONTHS / level * 100``, a
        percent per year, so a workforce of 150,000 thousand gaining 120
        thousand a month scores ``0.96``. Positive means the workforce is
        growing; there is no sign flip on this component, because a shrinking
        workforce is currency-negative directly.

        ``None`` when either series is empty, when the newest level is not
        positive, or when the flow does not cover the whole window. Never
        ``0.0`` for any of those: zero is the reading for a workforce that
        neither grew nor shrank.

    Why the units cancel. The count and the level come from the same published
    series, so whether the source reports thousands of persons or persons, the
    ratio is the same number. That is what makes the US ref, which arrives in
    thousands under a key whose canonical unit is persons, harmless here and
    only here. See `fbe.datasources.registry.EMPLOYMENT_LEVEL`.

    Why the window is checked rather than summed over whatever arrived. A
    monthly publisher contributes three prints and a quarterly one contributes
    one, and both describe three months. Summing two monthly prints because the
    third is missing would report two thirds of the hiring that happened, as a
    confident number, and the shortfall is always in the same direction so it
    would not read as noise. The expected count is derived from the series' own
    step rather than from the registry, because the cadence is a property of
    what arrived.

    Counting the prints is not sufficient, and this is the half that is easy to
    miss. ``employment_chg`` is a ``diff`` series, so each value is the change
    since the *previous* observation and its span is whatever gap precedes it.
    A series with periods 2026-01, 2026-05, 2026-06 and 2026-07 offers three
    prints inside a three-month window and a step of one month, so a count alone
    passes, but the 2026-05 value is a four-month change. At 50,000 hired a
    month the window would total 300,000 instead of 150,000 and report double
    the true hiring, in range and in whichever direction the labour market moved
    during the gap. So the periods are required to sit on the series' own grid,
    and the print immediately before the window must exist one step earlier,
    which is what proves the oldest in-window print spans one step rather than
    the hole before it. A series that begins exactly at the window has no such
    predecessor and is refused, because there is no way to tell what its first
    change measures.

    Why a non-positive level is refused rather than divided by. A workforce
    cannot be zero or negative, so reaching one means the series is not an
    employment level, and a percentage computed against it would be a confident
    number built on nonsense.

    """
    if not flow or not stock:
        return None

    level = stock[-1].value
    if level <= 0.0:
        return None

    step = _step_months(flow)
    if step is None or HIRING_WINDOW_MONTHS % step != 0:
        return None

    newest = flow[-1].period
    in_window = [
        entry
        for entry in flow
        if 0 <= _months_between(entry.period, newest) < HIRING_WINDOW_MONTHS
    ]
    if len(in_window) != HIRING_WINDOW_MONTHS // step:
        return None
    if _months_between(in_window[0].period, newest) != HIRING_WINDOW_MONTHS - step:
        return None
    if any(
        _months_between(earlier.period, later.period) != step
        for earlier, later in zip(in_window, in_window[1:], strict=False)
    ):
        return None
    if not any(
        _months_between(entry.period, in_window[0].period) == step for entry in flow
    ):
        return None

    total = fsum(entry.value for entry in in_window)
    return total * MONTHS_PER_YEAR / HIRING_WINDOW_MONTHS / level * 100.0


def _step_months(found: Sequence[Observation]) -> int | None:
    """Return the series' own publication step in whole months.

    Args:
        found: One currency's observations of one series, oldest first.

    Returns:
        Months between the two newest observations: ``1`` for a monthly
        publisher and ``3`` for a quarterly one. ``None`` when the series holds
        fewer than two observations, or when the two newest share a period,
        because neither case says what the cadence is.

    Measured from the series rather than read from
    `fbe.datasources.registry.IndicatorSpec.frequency` because the same
    indicator arrives monthly for some currencies and quarterly for others, and
    the per-currency answer is the one the window needs. `SeriesRef.frequency`
    holds it, but a pillar reaching into the registry for a per-currency source
    detail is the direction `CLAUDE.md` puts the boundary against.

    """
    if len(found) < 2:
        return None
    step = _months_between(found[-2].period, found[-1].period)
    return step if step > 0 else None
