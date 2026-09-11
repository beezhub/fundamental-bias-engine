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
from statistics import median

from fbe.config import ScoringConfig
from fbe.types import Frequency, Observation, PillarName, PillarScore

__all__ = [
    "BasePillar",
    "MIN_CROSS_SECTION",
    "MIN_COMPONENT_WEIGHT",
    "DEFAULT_PUBLICATION_LAG_DAYS",
]


DEFAULT_PUBLICATION_LAG_DAYS: Mapping[Frequency, int] = {
    Frequency.DAILY: 1,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 45,
    Frequency.QUARTERLY: 120,
    Frequency.IRREGULAR: 45,
}
"""Assumed gap between a period starting and its number being published.

Used only when an observation has no ``released_at``, to decide whether a
historical run could have seen it. The values are measured from ``period``, which
is the first day of the period described, so the monthly figure of 45 days covers
a month elapsing plus the usual two-week statistical lag, and the quarterly
figure of 120 days covers a quarter elapsing plus a month.

They are deliberately generous. An assumed lag that is too long costs a backtest
a little realism at the margin; one that is too short manufactures profit out of
numbers nobody had, and that error flatters rather than penalises, so it survives
review. Where a source can supply a real ``released_at``, it should, and this
table should never be reached.
"""


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
    measured in different units, a policy rate in percent and a yield change in
    basis points, so they cannot be summed raw. Each component is z-scored across
    the cross-section first, the component z-scores are blended with the pillar's
    sub-weights, and the blend is re-standardised so every pillar hands the
    scorer a quantity on the same scale.

    That second standardisation is not cosmetic. Averaging several imperfectly
    correlated z-scores shrinks the variance of the result, and the shrinkage
    grows with the number of components. On the reference run in section 7 of
    ``docs/scoring-spec.md``, the five-component monetary blend has a standard
    deviation of 0.7001 against 0.9711 for the two-component inflation blend, so
    without the pass the monetary pillar speaks about 39% more quietly than
    inflation relative to their declared weights: an effective ratio near 1.44 to
    1 where `ScoringConfig` says 2 to 1. Re-standardising is what keeps the
    configured weights the operative ones.

    The divisor comes from `blend_divisor`, which estimates it over recent runs
    rather than from the run in hand. That distinction matters enough to carry
    its own argument; see that method.

    Two pillars are excluded from the pass. Positioning and risk are already
    constructed on the score band in units that mean something, and
    re-standardising them would destroy that meaning: it would force positioning
    to show a spread across currencies even on a run where nothing is crowded,
    which is the opposite of what that pillar is for. Both override `_normalise`.

    Attributes:
        name: Which of the seven pillars this is.
        requires: Canonical indicator keys the pillar consumes. The runner uses
            these to decide what to fetch, so a key omitted here will not be
            available at compute time even if the source could supply it.
        config: Scoring configuration, read for the clip band, the lookback and
            this pillar's weight.
        blend_sd_history: Cross-sectional standard deviations this pillar's blend
            produced on earlier runs, feeding `blend_divisor`.

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

    def __init__(
        self,
        config: ScoringConfig | None = None,
        blend_sd_history: Sequence[float] | None = None,
    ) -> None:
        """Store the scoring configuration and any blend history this pillar has.

        Args:
            config: Scoring configuration. Defaults to `ScoringConfig()`, which
                carries the built-in weights and thresholds.
            blend_sd_history: Cross-sectional standard deviations this pillar's
                blend produced on previous runs, oldest first, for the
                re-standardisation divisor in `blend_divisor`. The runner loads
                them from the stored reports under ``DataConfig.reports_dir`` and
                passes them in here. ``None`` or a short history means the pillar
                falls back to the current run's own standard deviation.

        History arrives at construction rather than through `compute` because
        `compute`'s signature is fixed by the `Pillar` protocol, and because it
        is configuration of the same kind as the weights: a property of how this
        pillar is set up for this run, not of the observations it is handed.

        """
        self.config = config or ScoringConfig()
        self.blend_sd_history: Sequence[float] = blend_sd_history or ()

    @property
    def weight(self) -> float:
        """Weight this pillar carries in the composite, from config.

        Returns:
            The configured weight, or ``0.0`` if the pillar is not listed in
            the weight map. The fallback is defensive only:
            `fbe.config.Config.validate` rejects a map with a pillar missing,
            so a pillar is switched off by setting its weight to ``0.0``
            explicitly, where a reader of the config will see it.

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
            asof: The date the run represents. Only observations that were
                published on or before this date are used, so a historical run
                reproduces what was knowable at the time. Publication, not
                period: see `_extract` for the rule and for why the distinction
                is the difference between a backtest and a fiction.

        Returns:
            One `PillarScore` per currency, keyed by ISO code.

        Two things must reach ``PillarScore.notes`` on every run, because neither
        is recoverable from the numbers afterwards. First, which path
        `blend_divisor` took, ``"rolling"`` or ``"run_local"``: scores computed
        under the fallback are not on the same scale as scores computed under the
        rolling estimate, and a reader comparing two runs needs to know which they
        are holding. Second, how many inputs were admitted by the assumed
        publication lag rather than a real ``released_at``, which is how much of
        the run rests on `DEFAULT_PUBLICATION_LAG_DAYS` rather than on fact.

        The run's own blend standard deviation should also be returned to the
        caller for storage, since it is the next run's history.

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
            asof: Run date. Observations not yet published as of this date are
                dropped, per the visibility rule below.

        Returns:
            ``{currency: {indicator: observations}}``, each inner sequence
            sorted by ``period`` ascending and reduced to one observation per
            period, the newest vintage that existed at ``asof``. Currencies with
            nothing usable map to an empty inner mapping rather than being
            omitted.

        The visibility rule, which every implementation must apply. An
        observation counts only if it had been published by ``asof``:

            ``released_at`` present: visible when
            ``released_at.date() <= asof``. This is the real answer, and sources
            should supply it wherever they can.

            ``released_at`` absent: visible when
            ``period + DEFAULT_PUBLICATION_LAG_DAYS[frequency] <= asof``. An
            assumption, not a fact, and the run should record in
            ``PillarScore.notes`` how many of its inputs were admitted this way,
            because that count is how much of the result rests on a guess.

        Filtering on ``period`` instead is the mistake this rule exists to
        prevent, and it stays invisible until a backtest is run. US Q1 GDP has a
        period of 1 January and is published on about 25 April. A run dated 15
        April that filters on period keeps it, and scores the middle of April
        using a number that will not exist for another ten days. Every pillar
        here consumes lagged macro, so the error is systematic rather than
        occasional, and it always flatters: the engine appears to anticipate data
        it was in fact reading off the answer sheet.

        The vintage rule, which is the same mistake wearing different clothes.
        Where a source republishes a period, take the highest ``revision`` among
        the observations visible at ``asof``, not the highest ``revision``
        outright, breaking ties on the later ``released_at``. A run dated June
        2020 must see the March 2020 payrolls as first estimated in April 2020,
        not as revised in 2024. `Observation.released_at` and
        `Observation.revision` exist on the contract for precisely this, and a
        consumer that ignores them quietly turns a backtest into a description of
        the past written with hindsight.

        A live run, where ``asof`` is today, is unaffected by any of this:
        everything published is visible and the newest vintage is the only
        vintage. The rule costs nothing now and is the difference between an
        honest and a flattering number later, which is why it belongs in the
        specification rather than in a Phase 6 to-do.

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

        Used where a cross-sectional comparison is meaningless. The positioning
        pillar is the live case: how crowded the yen is against how crowded the
        Australian dollar is says nothing, because the two contracts have
        different natural ranges, but how crowded each is against its own record
        says a great deal, and those two readings are comparable.

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

    def blend_divisor(self, run_sd: float) -> tuple[float, str]:
        """Pick the standard deviation the blend is divided by.

        Args:
            run_sd: The cross-sectional standard deviation of this run's own
                blend. Used as the fallback, and recorded by the caller so it can
                join the history for later runs.

        Returns:
            ``(divisor, path)``, where ``path`` is ``"rolling"`` when the median
            of the last ``ScoringConfig.restandardisation_window_runs`` runs was
            used and ``"run_local"`` when there were fewer than
            ``ScoringConfig.min_restandardisation_runs`` of them and ``run_sd``
            was used instead. The path belongs in ``PillarScore.notes``: a score
            computed under the fallback is not on the same scale as one computed
            under the rolling estimate, so a report that does not say which was
            used is hiding the one fact needed to compare two runs.

        Why the divisor is estimated from history rather than from this run.
        Every component is already forced to unit standard deviation
        cross-sectionally at the previous stage, so the blend's standard
        deviation does not vary with how similar the eight economies happen to
        be that day: that information was normalised away one step earlier. What
        it varies with is the correlation between the pillar's own components.
        When the five monetary sub-indicators agree, the blend's standard
        deviation is near 1.0 and re-standardising barely touches it. When they
        disagree, it falls and a run-local divisor scales the pillar up hard.

        That is exactly backwards. Internal disagreement between a pillar's
        components is evidence the pillar is on shaky ground, and a run-local
        divisor converts it into amplification: the pillar would speak loudest on
        the days its own inputs are least coherent, and nothing in the output
        would reveal it. Estimating the divisor over recent runs breaks that
        feedback. The pillar is corrected for how many parts it is built from,
        which is the artefact the pass exists to remove, and not for how much
        those parts happen to be arguing today, which is information the model
        should keep.

        The median rather than the mean, so one strange run cannot move the scale
        the whole model is measured on. A fixed divisor also means the clip
        interacts with a scaling that does not move day to day, so a currency
        cannot cross the band edge because of something that happened to an
        unrelated pillar.

        The theoretical alternative, dividing by ``sqrt(sum of u squared)``, was
        rejected: for the monetary pillar it gives 0.4583 against the 0.7001
        actually observed, because it assumes components that are visibly
        correlated are independent. It would scale the heaviest pillar in the
        model by 2.18x instead of 1.43x, erring toward over-amplification in the
        one place that costs most.

        """
        usable = [s for s in self.blend_sd_history if s > 1e-9]
        window = usable[-self.config.restandardisation_window_runs :]
        if len(window) >= self.config.min_restandardisation_runs:
            return median(window), "rolling"
        return float(run_sd), "run_local"

    def blend_components(
        self,
        component_z: Mapping[str, Mapping[str, float | None]],
        weights: Mapping[str, float] | None = None,
    ) -> dict[str, float | None]:
        """Combine per-component z-scores into one z-score per currency.

        Args:
            component_z: ``{component: {currency: z}}``, each component already
                z-scored across the cross-section. Components absent from
                ``weights`` are ignored, which is how a pillar carries a
                report-only value through `_transform` for
                `headline_component` without letting it into the arithmetic.
            weights: Sub-weights per component. Defaults to
                `component_weights`.

        Returns:
            ``{currency: z}``, centred on this run's own cross-section and
            divided by the scale from `blend_divisor`.

        The arithmetic, matching section 2.3 of ``docs/scoring-spec.md`` with the
        divisor taken from history rather than from the run:

            ``blend(c)    = sum over available j of u_j' * z_j(c)``

            ``z_pillar(c) = (blend(c) - mean(blend)) / blend_divisor(sd(blend))``

        The mean stays run-local while the scale does not, and the asymmetry is
        deliberate. Centring is what keeps the pillar a statement about this run's
        cross-section, so it must come from this run. Scaling only corrects for
        how many parts the pillar is built from, which is a property of the
        pillar rather than of the day, so it should not be re-derived from the
        day. `blend_divisor` carries the full argument.

        When every currency has full sub-indicator coverage the blend's mean is
        exactly zero by construction, since each component z has mean zero and
        the sub-weights sum to 1. It is only non-zero when the per-currency
        renormalisation below differs across currencies, so subtract it anyway
        rather than relying on the special case. A divisor below ``1e-9`` is
        handled like any other degenerate cross-section: every score becomes
        ``0.0``.

        Renormalisation rule: for each currency the weighted mean runs over the
        components that currency actually has, divided by the weight present
        rather than the weight configured. A currency holding less than
        `MIN_COMPONENT_WEIGHT` of the pillar's sub-weight is returned as ``None``
        instead, because past that point the surviving components are being
        asked to speak for the ones that are absent.

        A component present for only part of the cross-section is z-scored
        across the currencies that have it, and the currencies that do not
        renormalise around its absence. Be aware of what that costs: a z-score
        computed across four currencies sits on a different scale from one across
        eight, so the two groups are being ranked against different yardsticks on
        that component. The growth pillar's PMI gap is the live case and carries
        the discussion; the alternative, dropping the component for everyone,
        throws away the best series in a pillar whenever one country is missing.

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
