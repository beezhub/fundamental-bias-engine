"""Shared machinery for the seven fundamental pillars.

A pillar answers one question about every currency in the universe at once,
and it answers it in relative terms. `BasePillar` fixes the shape of that
answer so the scorer can treat all seven identically: extract the observations
the pillar needs, transform them into one or more headline numbers per
currency, normalise those numbers across the eight currencies, then clip the
result onto the engine's ``-3..+3`` band.

The sign convention is global and never varies: a positive score means the
pillar reads the currency as fundamentally strong relative to the rest of the
universe. Pillars whose natural reading runs the other way, such as a falling
unemployment rate, invert inside `_transform` and say so in their docstring.

Nothing here does I/O. Pillars receive observations that a data source has
already fetched, cached and keyed to the canonical indicator names.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date

from fbe.config import ScoringConfig
from fbe.types import Observation, PillarName, PillarScore

__all__ = ["BasePillar", "MIN_CROSS_SECTION", "MIN_COMPONENT_WEIGHT"]


MIN_CROSS_SECTION: int = 3
"""Fewest usable currencies a cross-sectional z-score will accept.

With two values the z-score is always ``+/-0.707`` whatever the gap between
them, which encodes rank but discards magnitude and would hand the pillar a
confident-looking score built on nothing. Below this count the pillar declines
to score the whole cross-section rather than scoring part of it.
"""

MIN_COMPONENT_WEIGHT: float = 0.5
"""Fraction of a pillar's sub-weight that must be present for a currency.

Multi-component pillars renormalise over the components they actually have.
Below half the sub-weight the renormalisation is doing more work than the data,
so the currency is scored as missing instead.
"""


class BasePillar(ABC):
    """Base class for every pillar, implementing the `Pillar` protocol.

    Subclasses supply three things: the class attributes `name` and `requires`,
    an `_extract` hook that pulls the observations they care about, and a
    `_transform` hook that turns those observations into headline numbers. The
    normalisation and scoring steps are shared and should rarely be overridden.

    Two-stage normalisation is used throughout. Components inside a pillar are
    measured in different units, a policy rate in percent and a trade balance in
    billions of local currency, so they cannot be summed raw. Each component is
    z-scored across the cross-section first, the component z-scores are blended
    with the pillar's sub-weights, and the blend is re-standardised so every
    pillar hands the scorer a quantity on the same scale. Without that second
    standardisation a five-component pillar would arrive systematically quieter
    than a one-component pillar and would lose weight it was never meant to lose.

    Attributes:
        name: Which of the seven pillars this is.
        requires: Canonical indicator keys the pillar consumes. The runner uses
            these to decide what to fetch, so a key omitted here will not be
            available at compute time even if the source could supply it.
        config: Scoring configuration, read for the clip band, the lookback and
            this pillar's weight.

    """

    name: PillarName
    requires: Sequence[str] = ()

    headline_component: str = ""
    """Which component's natural-unit value is reported as ``PillarScore.raw``.

    The score band is unitless and unreadable on its own. Every pillar nominates
    the one component a human would quote when asked to justify the call, and
    `compute` copies that component's pre-normalisation value into ``raw`` so
    the report can show it. It has no effect on the arithmetic.

    The named component does not have to appear in `component_weights`. A pillar
    whose blended components are all derived quantities may emit an extra
    report-only component from `_transform` purely to fill this field, which is
    what the positioning and risk pillars do.
    """

    def __init__(self, config: ScoringConfig | None = None) -> None:
        """Store the scoring configuration this pillar will read.

        Args:
            config: Scoring configuration. Defaults to `ScoringConfig()`, which
                carries the built-in weights and thresholds.

        """
        self.config = config or ScoringConfig()

    @property
    def weight(self) -> float:
        """Weight this pillar carries in the composite, from config.

        Returns:
            The configured weight, or ``0.0`` if the pillar is not listed in
            the weight map, which is how a pillar is switched off without
            removing it from the run.

        """
        return float(self.config.weights.get(self.name, 0.0))

    # ------------------------------------------------------------------
    # Template method
    # ------------------------------------------------------------------

    def compute(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, PillarScore]:
        """Score every currency in the universe for this pillar.

        Runs the fixed pipeline: `_extract`, `_transform`, `_normalise`,
        `clip_and_scale`. Every currency in ``currencies`` appears in the
        result, including those with no usable data, which receive the neutral
        `missing_score`. The scorer relies on that: it detects thin coverage by
        looking for ``z is None``, not by looking for absent keys.

        Args:
            observations: Every observation fetched for this run, for all
                currencies and all indicators. Pillars filter this themselves;
                the full set is passed because normalisation is cross-sectional
                and a pillar cannot judge one currency without seeing the rest.
            currencies: The universe to score, normally `universe.G10`.
            asof: The date the run represents. Observations with a ``period``
                after this date are ignored, so a historical run reproduces
                what was knowable at the time.

        Returns:
            One `PillarScore` per currency, keyed by ISO code.

        """
        raise NotImplementedError

    @abstractmethod
    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        """Select and index the observations this pillar needs.

        Args:
            observations: The full observation set for the run.
            currencies: The universe to score.
            asof: Run date. Observations dated after it are dropped.

        Returns:
            ``{currency: {indicator: observations}}``, each inner sequence
            sorted by ``period`` ascending and de-duplicated to the highest
            ``revision`` per period. Currencies with nothing usable map to an
            empty inner mapping rather than being omitted.

        """

    @abstractmethod
    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        """Turn raw observations into the pillar's component values.

        This is where each pillar's economics live: differencing a rate against
        an inflation target, taking a three-month change, inverting a sign.
        Values stay in their natural units at this stage; comparability is the
        normaliser's job.

        Args:
            extracted: Output of `_extract`.
            asof: Run date.

        Returns:
            ``{currency: {component: value}}``. A component the currency has no
            data for maps to ``None``, never to ``0.0``: zero is a reading and
            missing is not, and conflating them would drag a currency toward the
            middle of the cross-section on the strength of an outage.

        """

    def _normalise(
        self,
        components: Mapping[str, Mapping[str, float | None]],
    ) -> Mapping[str, float | None]:
        """Blend the pillar's components into one z-score per currency.

        Default behaviour: z-score each component across the cross-section, blend
        with `component_weights`, then re-standardise the blend. Single-component
        pillars get plain `cross_sectional_z`.

        Args:
            components: Output of `_transform`.

        Returns:
            ``{currency: z}``, with ``None`` where the currency could not be
            scored.

        """
        raise NotImplementedError

    @property
    def component_weights(self) -> Mapping[str, float]:
        """Sub-weights the pillar's components carry inside the blend.

        Returns:
            ``{component: weight}``, summing to 1.0 across the components the
            pillar defines. Overridden by every multi-component pillar.

        """
        return {}

    # ------------------------------------------------------------------
    # Shared statistics
    # ------------------------------------------------------------------

    @staticmethod
    def cross_sectional_z(
        values: Mapping[str, float | None],
    ) -> dict[str, float | None]:
        """Z-score a set of values across the currencies in this run.

        This is the core transform of the model. The question is never whether
        a 4.5% policy rate is high, it is whether 4.5% is high compared with the
        other seven currencies on the same day, because that is what an FX pair
        actually prices.

        The standard deviation is computed with ``ddof=0``. The eight currencies
        are the entire scored universe, not a sample drawn from a larger one, so
        the population form is the correct estimator and the sample form would
        inflate the spread for no reason.

        Args:
            values: ``{currency: value}``, with ``None`` for currencies that
                have no reading. Missing currencies take no part in the mean or
                the standard deviation.

        Returns:
            ``{currency: z}`` over the same keys as ``values``. Currencies that
            came in as ``None`` stay ``None``.

        Behaviour in the awkward cases:
            Fewer than `MIN_CROSS_SECTION` usable values: every currency comes
            back ``None``, including the ones that had data. A z-score against a
            one or two point cross-section is arithmetic, not information, and
            the honest answer is that the pillar could not run.

            Zero standard deviation, meaning every usable currency reported the
            same value: every usable currency gets ``0.0``. That is a real
            finding, the pillar sees no difference between them, and it should
            not be reported as missing data.

        """
        raise NotImplementedError

    @staticmethod
    def time_series_z(
        series: Sequence[Observation],
        lookback_years: int,
        asof: date | None = None,
    ) -> float | None:
        """Z-score the latest value of a series against its own history.

        Used where a cross-sectional comparison is meaningless. A Japanese trade
        balance in billions of yen and a Canadian one in billions of dollars
        cannot be ranked against each other, but each can be ranked against its
        own five-year record, and those two ranks are comparable.

        The standard deviation uses ``ddof=1`` here, the opposite of
        `cross_sectional_z`: the observed history is a sample of the process, not
        the whole of it.

        Args:
            series: Observations for one indicator and one currency, sorted by
                ``period`` ascending.
            lookback_years: Window length. Observations older than
                ``asof - lookback_years`` are excluded.
            asof: Run date, defaulting to the newest period in ``series``.
                Observations after it are excluded so historical runs stay
                honest.

        Returns:
            The z-score of the newest in-window value, or ``None`` when the
            window holds fewer than twelve observations or its standard
            deviation is zero. Twelve is the floor because most of these series
            are monthly and a shorter window cannot distinguish a level shift
            from a seasonal one.

        """
        raise NotImplementedError

    @staticmethod
    def momentum(series: Sequence[Observation], periods: int) -> float | None:
        """Return the change in a series over the last N periods.

        The workhorse transform of the engine. In FX the level of a variable is
        largely in the price already, having been forecast and traded for months;
        what moves a pair is the revision, the surprise, the direction of travel.
        A 5% policy rate that is on its way down is a weak currency and a 2% rate
        on its way up is a strong one.

        Args:
            series: Observations for one indicator and one currency, sorted by
                ``period`` ascending, one observation per period.
            periods: How many observations back to difference against. Periods
                are series-native, so ``periods=3`` on a monthly series is a
                three-month change and on a quarterly one is nine months. Each
                pillar states the horizon it intends in its own docstring.

        Returns:
            ``series[-1].value - series[-1 - periods].value`` in the series' own
            unit, not annualised and not rebased to a percentage. ``None`` when
            the series holds fewer than ``periods + 1`` observations, which is
            the common case for a newly onboarded currency.

        """
        raise NotImplementedError

    @staticmethod
    def clip_and_scale(z: float | None, clip: float) -> float:
        """Map a z-score onto the engine's bounded score band.

        Clipping is what stops a single dislocated pillar from carrying a
        currency. A z of 6, which in this model usually means a data error or a
        one-off policy shock rather than six standard deviations of fundamental
        strength, is worth exactly the same as a z of 3.

        Args:
            z: The z-score, or ``None`` when the pillar could not score the
                currency.
            clip: Band half-width, from ``ScoringConfig.score_clip`` (3.0).

        Returns:
            ``z`` bounded to ``[-clip, +clip]``, or ``0.0`` when ``z`` is
            ``None``. The zero is deliberate and is documented on
            `missing_score`: it is a neutral placeholder, and the coverage
            figure alongside it is what tells the reader the pillar was absent.

        """
        if z is None:
            return 0.0
        return max(-clip, min(clip, float(z)))

    def blend_components(
        self,
        component_z: Mapping[str, Mapping[str, float | None]],
        weights: Mapping[str, float] | None = None,
    ) -> dict[str, float | None]:
        """Combine per-component z-scores into one z-score per currency.

        Args:
            component_z: ``{component: {currency: z}}``, each component already
                z-scored across the cross-section.
            weights: Sub-weights per component. Defaults to
                `component_weights`.

        Returns:
            ``{currency: z}``, re-standardised across the cross-section so the
            blend has unit dispersion again.

        Renormalisation rule: for each currency the weighted mean runs over the
        components that currency actually has, divided by the weight present
        rather than the weight configured. A currency holding less than
        `MIN_COMPONENT_WEIGHT` of the pillar's sub-weight is returned as ``None``
        instead, because past that point the surviving components are being
        asked to speak for the ones that are absent.

        A component present for only part of the cross-section is dropped for
        everyone, not just for the currencies missing it. A z-score computed
        across four currencies is on a different scale from one computed across
        eight, and blending the two silently rescales the pillar. The growth
        pillar's PMI gap is the live case.

        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Freshness and absence
    # ------------------------------------------------------------------

    @staticmethod
    def staleness_days(observations: Sequence[Observation], asof: date) -> int:
        """Return the age in days of the freshest observation in a set.

        Age is measured from ``period``, the period the data describes, not from
        ``released_at``. A GDP print published yesterday for the quarter that
        ended four months ago is four months old as far as the model is
        concerned, whatever the release timestamp says.

        Args:
            observations: The observations a pillar consumed for one currency.
            asof: Run date.

        Returns:
            ``(asof - newest period).days``, floored at ``0`` for periods dated
            after ``asof``, which happens with forward-dated survey data.
            Returns ``ScoringConfig.max_staleness_days + 1`` for an empty set,
            so an absent pillar sorts as stale rather than as fresh.

        """
        raise NotImplementedError

    def missing_score(
        self,
        currency: str,
        asof: date,
        notes: str = "",
        staleness_days: int | None = None,
    ) -> PillarScore:
        """Build the score a pillar returns when it cannot score a currency.

        The choice here matters more than it looks. The alternatives were to
        guess a value, to carry the last known reading forward, or to drop the
        currency from the run. All three lie to the aggregator. Instead the
        pillar returns a genuinely neutral score of ``0.0`` with ``raw`` and
        ``z`` both ``None``, and the scorer detects the absence through those
        ``None`` markers, excludes the pillar's weight from the composite, and
        reports the shortfall as reduced ``coverage`` on the `CurrencyScore`.

        The consequence downstream is a smaller position, not a wrong one: low
        coverage caps conviction in `bias.conviction_for`, so a currency scored
        on half its pillars cannot reach the shortlist at high conviction.

        Args:
            currency: ISO code the score belongs to.
            asof: Run date.
            notes: Short human-readable reason, shown in the report's working.
                Say which indicator was missing, not just that data was thin.
            staleness_days: Override for the freshness field. Defaults to
                ``ScoringConfig.max_staleness_days + 1``, marking the pillar as
                past its useful life.

        Returns:
            A neutral `PillarScore` carrying this pillar's configured weight.

        """
        raise NotImplementedError
