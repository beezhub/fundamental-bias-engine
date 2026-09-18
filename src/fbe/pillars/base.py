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
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import fsum, sqrt
from statistics import median, pstdev

from fbe.config import ScoringConfig
from fbe.datasources.registry import INDICATORS
from fbe.scoring import freshness
from fbe.types import Frequency, Observation, PillarName, PillarScore

__all__ = [
    "BasePillar",
    "MIN_CROSS_SECTION",
    "MIN_TIME_SERIES_WINDOW",
    "MIN_COMPONENT_WEIGHT",
    "DEFAULT_PUBLICATION_LAG_DAYS",
    "staleness_allowance",
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


MIN_TIME_SERIES_WINDOW: int = 12
"""Fewest in-window observations a time-series z-score will accept.

Most of these series are monthly, so twelve is one year: the shortest window
that can tell a level shift from a seasonal one. Below it the standard deviation
is dominated by whichever part of the year the window happens to cover, and the
z-score it produces describes the calendar rather than the currency.

`BasePillar.time_series_z` returns ``None`` below this count rather than scoring
a short window, for the same reason `MIN_CROSS_SECTION` refuses a thin
cross-section: a confident number from too little data is worse than no number.
"""


MIN_CROSS_SECTION: int = 3
"""Fewest usable currencies a cross-sectional z-score will accept.

With two values the z-score is always ``+/-1.0`` whatever the gap between them,
which encodes rank but discards magnitude and would hand the pillar a
confident-looking score built on nothing. Below this count the pillar declines
to score the whole cross-section rather than scoring part of it.

``+/-1.0`` follows from ``ddof=0``, which `BasePillar.cross_sectional_z`
mandates because the eight currencies are the entire scored universe rather than
a sample drawn from a larger one. Two points sit one population standard
deviation either side of their own mean by construction, whether they are 1 and
3 or 1 and 30. The sample form would give ``+/-0.707``, and that figure has no
place in this module: a reader checking this reason by hand computes 1.0 and
would otherwise conclude that either this docstring or the ``ddof=0`` rule is
wrong. ``tests/test_cross_sectional_z.py`` holds the two together.
"""

MIN_COMPONENT_WEIGHT: float = 0.5
"""Fraction of a pillar's sub-weight that must be present for a currency.

Multi-component pillars renormalise over the components they actually have. At
or below half the sub-weight the renormalisation is doing more work than the
data, so the currency is scored as missing instead.

Half is not enough, and the boundary is part of the rule rather than an
accident of it. EMPLOYMENT is the case that decides this: its two components
carry 0.50 each, so a currency missing either one holds exactly 0.50. Under a
strict "less than" the floor could never fire for the one pillar whose own
docstring says neither component guards against the other's failure mode alone,
and a currency scored on the unemployment rate with no hiring series would
carry the pillar's full weight with nothing marking it. A safeguard that cannot
fire for the pillar that needs it most is worse than no safeguard, because a
reader checking the rule concludes the case is covered.

The comparison is uniform across pillars. GROWTH reaches the boundary too, on
any one 0.30 component plus one 0.20 component, and is scored as absent there on
the same reasoning: the reason for the floor is arithmetic, that the surviving
components are being asked to speak for the absent ones, not a judgement about
any one pillar's economics. Exempting a pillar would be a conditional wearing a
disguise.
"""


def staleness_allowance(indicator: str, config: ScoringConfig) -> int:
    """Return how old this indicator may be, in days, and still carry weight.

    Args:
        indicator: Canonical indicator key, as it appears on
            ``Observation.indicator`` and in a pillar's ``requires``.
        config: Scoring configuration, read only for the fallback.

    Returns:
        ``IndicatorSpec.max_staleness_days`` when the registry knows the key,
        otherwise ``ScoringConfig.max_staleness_days``.

    The registry wins because it is the only module that knows the release
    calendar, and it says so in its own docstring: a 2-year yield stale by a
    week means the feed broke, while a quarterly balance-of-payments figure is
    routinely five months old on the day it is most current. The global figure
    is the default for a key the registry has no entry for, not a ceiling over
    the registry's values.

    The fallback is worth reading twice. An unregistered key silently gets the
    45-day default, which is the behaviour this function exists to remove, so a
    pillar asking for a key the registry does not carry under that name is a
    quiet under-weighting rather than an error.
    ``tests/test_staleness_ramp.py`` pins the current set of such keys so that
    it shrinks deliberately rather than growing by accident.

    Importing the registry here is the allowed direction: the registry knows
    nothing about pillars beyond which pillar consumes each indicator, and the
    scorer stays free of it because the pillar passes the resulting factor on.

    """
    spec = INDICATORS.get(indicator)
    if spec is None:
        return config.max_staleness_days
    return spec.max_staleness_days


_EARLIEST = datetime.min.replace(tzinfo=UTC)
"""Sort floor for an observation with no ``released_at``.

Only ever reached as the second element of a vintage key, after a check on
whether the stamp exists at all, so it orders unstamped observations below
stamped ones rather than standing in for a real release time.
"""


def _years_earlier(when: date, years: int) -> date:
    """Return the same calendar day ``years`` before ``when``.

    Args:
        when: The date to count back from.
        years: Whole years, from `ScoringConfig.lookback_years`.

    Returns:
        The same day and month, ``years`` earlier. 29 February steps back to
        28 February where the earlier year is not a leap year, which shortens
        the window by a day rather than lengthening it: a window a day short
        drops one observation, and one a day long admits an observation from
        outside the lookback the config asked for.

    Raises:
        ValueError: If ``years`` is negative, which would put the start of the
            window after its end and return an empty history from a series that
            has one.

    """
    if years < 0:
        raise ValueError(f"a lookback cannot be negative, got {years} years")
    try:
        return when.replace(year=when.year - years)
    except ValueError:
        # 29 February in a year whose counterpart is not a leap year.
        return when.replace(year=when.year - years, month=2, day=28)


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

    component_indicators: Mapping[str, tuple[str, ...]] = {}
    """Which indicators each component is aged against, for the freshness ramp.

    ``{component: (indicator, ...)}`` over exactly the keys in
    `component_weights`. The keys on the right are the ones `_extract` returns,
    so a component built by differencing two series names both of them and a
    component taken straight from one series names that one.

    A component is as fresh as its stalest input, not its freshest. The monetary
    pillar's real policy rate is a daily policy rate less a CPI print that may be
    five months old, and reading it as one day old would carry the stale half
    into the blend at full weight.

    Every component must appear. A component with no entry is silently exempt
    from the discount, which is the failure this table exists to prevent, and
    ``tests/test_staleness_ramp.py`` fails the pillar that leaves one out.
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
        self.last_blend_sd: float | None = None
        """This run's own blend standard deviation, set by `blend_components`.

        ``None`` when the most recent `compute` blended nothing, which `compute`
        resets before it starts so a previous run's figure cannot be read as
        this one's. The runner reads it
        after `compute` and appends it to the stored history, so it becomes the
        next run's `blend_sd_history`. It rides on the instance rather than on
        the return value because `compute`'s signature is fixed by the
        `fbe.types.Pillar` protocol, which is the same reason the history
        arrives at construction.
        """
        self.last_blend_divisor_path: str = ""
        """Which path `blend_divisor` took on this run, or ``""`` for none.

        Set by `blend_components` and copied onto every `PillarScore` that
        `compute` builds. A single-component pillar never blends, so it keeps
        the empty string, which `PillarScore.blend_divisor_path` documents as
        meaning the pillar recorded no path.
        """

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

        Three things are recorded on every score, because none is recoverable
        from the numbers afterwards, and each has a typed home rather than a
        line of prose. Which path `blend_divisor` took goes to
        ``PillarScore.blend_divisor_path``: scores computed under the fallback
        are not on the same scale as scores computed under the rolling estimate,
        and a reader comparing two runs needs to know which they are holding.
        How many inputs were admitted by the assumed publication lag rather than
        a real ``released_at`` goes to
        ``PillarScore.diagnostics["assumed_lag_inputs"]``, which is how much of
        the run rests on `DEFAULT_PUBLICATION_LAG_DAYS` rather than on fact. The
        third is `pillar_freshness`, which goes to
        ``PillarScore.freshness_factor`` and is the only one of the three the
        aggregator acts on: it is the fraction of the configured weight this
        pillar's inputs justify, and `scoring.score_currencies` multiplies by it.

        ``notes`` carries none of them. It is human prose for the report's working
        and nothing may parse it for a decision, which is what
        `fbe.types.PillarScore.notes` now says. Issue #51 records the ruling.

        The per-component freshness factors from `component_freshness` belong in
        ``PillarScore.diagnostics`` under ``freshness.<component>``, one key per
        component the currency had data for. They are a per-run measurement about
        the inputs rather than about the score, so nothing in `scoring.py` reads
        them: the aggregator sees their effect only through the weight, once,
        via the `pillar_freshness` result recorded on
        ``PillarScore.freshness_factor``. Without them a reader cannot tell a
        pillar carrying one stale component from a pillar that is uniformly late,
        and the two call for different action.

        The run's own blend standard deviation should also be returned to the
        caller for storage, since it is the next run's history.

        """
        # Cleared before the run, not after it. Both are set by
        # `blend_components`, and a `_normalise` override that does not blend
        # would otherwise leave the previous call's path and standard deviation
        # on this call's scores, which is a stale fact wearing a current one's
        # clothes.
        self.last_blend_sd = None
        self.last_blend_divisor_path = ""

        extracted = self._extract(observations, currencies, asof)
        components = self._transform(extracted, asof)
        normalised = self._normalise(components)

        # One number for the whole run, not one per currency. `_diagnostics` is
        # called per currency and cannot see the cross-section, and a spread
        # computed from a single currency has no meaning.
        emit_sd = self._emit_sd(normalised, currencies)

        scores: dict[str, PillarScore] = {}
        for currency in currencies:
            per_currency = extracted.get(currency, {})
            # Everything `_extract` returned for this currency. Indexing by
            # `requires` instead would drop any series a pillar extracts under
            # another key, and those observations still aged the score and still
            # count toward the assumed-lag total.
            inputs = tuple(
                observation
                for series in per_currency.values()
                for observation in series
            )
            diagnostics = self._diagnostics(per_currency, inputs, asof)
            diagnostics["emit_sd"] = emit_sd
            z = normalised.get(currency)

            if z is None:
                absent = [
                    indicator
                    for indicator in self.requires
                    if not per_currency.get(indicator)
                ]
                reason = (
                    f"{self.name.value} could not score {currency}: no usable "
                    f"{', '.join(absent)}"
                    if absent
                    else f"{self.name.value} could not score {currency}"
                )
                scores[currency] = replace(
                    self.missing_score(currency, asof, notes=reason),
                    diagnostics=diagnostics,
                    blend_divisor_path=self.last_blend_divisor_path,
                )
                continue

            scores[currency] = PillarScore(
                pillar=self.name,
                currency=currency,
                raw=(
                    components.get(currency, {}).get(self.headline_component)
                    if self.headline_component
                    else None
                ),
                notes=self._notes(currency, components.get(currency, {}), per_currency),
                z=z,
                score=self.clip_and_scale(z, self.config.score_clip),
                weight=self.weight,
                asof=asof,
                staleness_days=self.staleness_days(inputs, asof),
                inputs=inputs,
                diagnostics=diagnostics,
                blend_divisor_path=self.last_blend_divisor_path,
                # Computed here because this is the only place holding both the
                # extracted slice and the finished score. `score_currencies`
                # receives the score alone, and re-deriving the slice there would
                # put the registry's indicator keys in front of a module that is
                # deliberately free of them. The absent branch above does not
                # reach this: `missing_score` states its own 0.0.
                freshness_factor=self.pillar_freshness(per_currency, asof),
            )
        return scores

    def _emit_sd(
        self,
        normalised: Mapping[str, float | None],
        currencies: Sequence[str],
    ) -> float:
        """Measure the spread this pillar actually emitted across the universe.

        Args:
            normalised: `_normalise`'s output, ``None`` where the pillar could
                not score the currency.
            currencies: The universe, in run order.

        Returns:
            The population standard deviation of the scores the pillar emitted,
            over the currencies it scored. ``0.0`` when it scored fewer than two.

        Why this is measured on the score rather than on ``z``. The two differ
        only where `clip_and_scale` bites, and the composite is built from the
        score, so the score is what the pillar actually contributed. Reporting
        the unclipped spread would overstate a pillar whose outliers were
        trimmed, which is the direction that flatters.

        Why the currencies with no score are left out. Their placeholder is
        ``0.0``, and counting those zeros would pull the spread toward zero and
        report a pillar with thin coverage as a quiet one. Those are different
        facts: the first is answered by coverage, the second by this number.

        Why ``0.0`` when nothing scored. There is no cross-section to measure,
        and the convention is `missing_score`'s: a neutral placeholder with the
        coverage figure alongside saying the pillar was absent. A key that
        vanished instead would make a reader iterating diagnostics handle two
        shapes for the same fact.

        The population form, ``ddof=0``, because these eight are the whole
        scored universe rather than a sample of one, which is the same reason
        `cross_sectional_z` gives.

        """
        emitted = [
            self.clip_and_scale(normalised.get(currency), self.config.score_clip)
            for currency in currencies
            if normalised.get(currency) is not None
        ]
        if len(emitted) < 2:
            return 0.0
        return float(pstdev(emitted))

    def _diagnostics(
        self,
        extracted: Mapping[str, Sequence[Observation]],
        inputs: Sequence[Observation],
        asof: date,
    ) -> dict[str, float]:
        """Build the per-run measurements that ride alongside one score.

        Args:
            extracted: One currency's slice of `_extract`'s output.
            inputs: The same observations flattened, as they reach the score.
            asof: Run date.

        Returns:
            ``freshness.<component>`` for each component this currency had data
            for, straight from `component_freshness`, plus
            ``assumed_lag_inputs``. A component with no data has no key, because
            `component_freshness` distinguishes an absent component from one
            present and expired, and flattening the two here would undo that.

            ``assumed_lag_inputs`` is always present, including when it is zero.
            A key that disappears at zero cannot be read as zero by anything
            downstream, which is the same absence-versus-value confusion this
            module exists to avoid, one level up.

        Nothing here reaches the composite. `fbe.scoring` reads freshness only
        through the weight, once, via `pillar_freshness`.

        """
        diagnostics = {
            f"freshness.{component}": factor
            for component, factor in self.component_freshness(extracted, asof).items()
        }
        # An observation that survived the visibility rule without a
        # ``released_at`` was admitted on the assumed lag, so counting the
        # unstamped inputs counts exactly the ones resting on the assumption.
        diagnostics["assumed_lag_inputs"] = float(
            sum(1 for observation in inputs if observation.released_at is None)
        )
        return diagnostics

    def _notes(
        self,
        currency: str,
        components: Mapping[str, float | None],
        extracted: Mapping[str, Sequence[Observation]],
    ) -> str:
        """Return the working behind one scored currency's headline number.

        Args:
            currency: The currency being scored.
            components: That currency's component values from `_transform`,
                including the ``None`` entries for components it could not
                build.
            extracted: That currency's slice of `_extract`'s output, so a note
                can name the period of the print it describes. A quarterly
                series reads the same in the note whether it printed last month
                or five months ago, and the note exists to be checkable.

        Returns:
            Human prose, empty by default. Nothing may parse it for a decision:
            `PillarScore.notes` is the report's working, and issue #51 is the
            ruling that put every fact a consumer acts on in a typed field
            instead, `blend_divisor_path` and `diagnostics` being the two it
            moved.

        Only the scored branch calls this. A currency `compute` could not score
        already gets a note naming the indicators it lacked, and a pillar that
        appended to that would be explaining a number nobody has. The case that
        makes the distinction bite is a currency holding one component and
        refused by `MIN_COMPONENT_WEIGHT`: it has a value to describe and no
        score to attach the description to.

        **An override must not raise.** `scoring.score_currencies` catches
        anything a pillar raises by marking every currency in the universe
        unscored, so an exception while formatting prose costs the pillar its
        entire run. A note is cosmetic and nothing here is worth that.

        ``asof`` is deliberately not passed. The first draft of this hook took
        it, the only override ignored it, and what that override actually
        needed was the observations. A signature that offers the unused
        argument and withholds the used one invites the next pillar to write a
        note it cannot make true.

        Overridden where the headline number cannot be checked from itself.
        `raw` is a difference for several pillars, and a difference alone is
        not checkable: an inflation gap of ``+0.5`` is consistent with a 3.0%
        print against a 2.5% target and with 2.5% against 2.0%. The default is
        empty rather than a generic sentence, because a note that says nothing
        still occupies the line a reader looks at for the working.

        """
        return ""

    @staticmethod
    def _visible(observation: Observation, asof: date) -> bool:
        """Say whether a run dated ``asof`` could have read this observation.

        Args:
            observation: The observation to judge.
            asof: Run date.

        Returns:
            True when it had been published by ``asof``, inclusive on the day.
            With a ``released_at`` that is the fact. Without one it is an
            assumption: the period's start plus the frequency's entry in
            `DEFAULT_PUBLICATION_LAG_DAYS`, which is why a run records how many
            of its inputs were admitted this way.

        Period is deliberately not the test. US Q1 GDP has a period of 1 January
        and prints around 25 April, so a run dated 15 April that filtered on
        period would score the middle of April with a number that did not exist
        for another ten days. Most series here are lagged, so the error is
        systematic rather than occasional, and it flatters. It is smallest for
        the daily series, a two-year yield or a volatility index, where the lag
        is a day, and largest for the quarterly ones.

        The fallback has a hole this method cannot close, worth knowing rather
        than discovering. Period plus lag is the same answer for every vintage
        of one period, so an unstamped revision is admitted the moment the
        original print would have been, and `_newest_vintages` then prefers it
        on ``revision``. The engine is honest exactly when a source stamps its
        data and flattering when it does not. Closing it means changing the rule
        in `BasePillar._extract` rather than one pillar's reading of it, which
        is issue #121.

        """
        if observation.released_at is not None:
            return observation.released_at.date() <= asof
        lag = DEFAULT_PUBLICATION_LAG_DAYS[observation.frequency]
        return observation.period + timedelta(days=lag) <= asof

    @staticmethod
    def _newest_vintages(found: Sequence[Observation]) -> tuple[Observation, ...]:
        """Reduce to one observation per period and sort by period ascending.

        Args:
            found: One currency's visible observations of one indicator, in
                whatever order they arrived.

        Returns:
            One observation per period, newest vintage first by ``revision`` and
            then by the later ``released_at``, ordered oldest period first.
            Revision leads because a correction issued later under a lower
            revision number is not the current vintage; the stamp only breaks a
            tie. Two observations identical on both keep the one that arrived
            first, the only arbitrary case here, and arbitrary in a specific
            way: arrival order is the order the sources were read in, so two
            feeds carrying one period at one revision with different values
            resolve differently if the collector's source order changes, and
            nothing says so.

        The visibility filter has already run, so "newest vintage" means newest
        among what the run could see. A June 2020 run must read March 2020
        payrolls as first estimated in April 2020, not as revised in 2024, and
        taking the highest revision outright is how that goes wrong.

        """
        by_period: dict[date, Observation] = {}
        for observation in found:
            current = by_period.get(observation.period)
            if current is None or _vintage_key(observation) > _vintage_key(current):
                by_period[observation.period] = observation
        return tuple(by_period[period] for period in sorted(by_period))

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
            period, the newest vintage that existed at ``asof``. Every currency
            asked for is a key, and every key in `requires` is present under it,
            with an empty sequence where that currency has nothing. A currency
            with nothing at all therefore carries one empty sequence per
            indicator rather than an empty mapping.

            The reason is that a dropped key and an empty sequence read the same
            at a glance and are different facts, and keeping the key means this
            method's output shape does not depend on its input data. One
            consequence is worth stating because it is easy to write by
            accident: ``if not extracted[currency]`` is never the test for an
            unscorable currency, since a mapping of empty sequences is truthy.
            `component_freshness` does not settle this either way, and an
            earlier version of this paragraph wrongly claimed it did: its
            ``.get`` guard is a truthiness test, so an absent key and an empty
            sequence both come back as an absent component and it cannot tell
            the two apart.

        The visibility rule, which every implementation must apply. An
        observation counts only if it had been published by ``asof``:

            ``released_at`` present: visible when
            ``released_at.date() <= asof``. This is the real answer, and sources
            should supply it wherever they can.

            ``released_at`` absent: visible when
            ``period + DEFAULT_PUBLICATION_LAG_DAYS[frequency] <= asof``. An
            assumption, not a fact, and the run records in
            ``PillarScore.diagnostics["assumed_lag_inputs"]`` how many of its
            inputs were admitted this way, because that count is how much of the
            result rests on a guess. A count is a number, so it goes to
            ``diagnostics`` rather than into prose a consumer would have to
            parse; `compute` fills it.

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

        Concrete, and the default answer for a pillar reading per-currency
        series under the keys in `requires`. It was abstract while MONETARY was
        the only pillar; INFLATION then wrote the same twenty-one lines, which
        is the copy #122 exists to prevent one level up from the two helpers it
        names. Sharing the leaves and duplicating the composition would leave
        #121's fix landing in one file and being missed in the others.

        Override it where a pillar reads something else. POSITIONING needs full
        history rather than the newest vintage per period, and RISK reads a
        ``GLOBAL``-keyed series that belongs to no currency, so neither is
        served by the loop below. An override should call this through
        ``super()`` for the part it does share rather than restating the rule.

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
            # Two filters, answering different questions. The period bound
            # drops a figure describing a month that has not happened, which is
            # what a forecast or a forward-dated survey is; `_visible` drops a
            # figure that describes the past and had not been published yet.
            # A pillar reading the newest element as current needs both, and
            # `staleness_days` floors a forward-dated period at zero rather
            # than dropping it, so it cannot be relied on to do this one.
            if series is None or observation.period > asof:
                continue
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
            scored. The multi-component path carries the absence cases
            `blend_components` documents; the single-component path carries
            `cross_sectional_z`'s.

        Raises:
            ValueError: the pillar declares no `component_weights`. It has not
                said what it is built from, so there is nothing to blend and
                nothing to fall through to. Reporting every currency as absent
                would state a data outage, which is a different fact and one a
                reader would act on differently.

        The freshness factors of section 4.1 are **not** applied on this path.
        `blend_components` takes them and this method has nowhere to receive
        them from, so a blend reached through here weights every component at
        ``u_j`` with ``phi_j`` fixed at ``1.0``. See the note on #115 for why
        that is left rather than fixed here: `compute` is the method that holds
        the factors, it is scaffolded pending #51, and widening this signature
        would change a contract that `PositioningPillar` and `RiskPillar`
        override.

        """
        weights = self.component_weights
        if not weights:
            raise ValueError(
                f"{self.name} declares no component_weights, so there is "
                "nothing to blend and nothing to fall through to"
            )

        currencies = list(components)
        if len(weights) == 1:
            (only,) = weights
            return self.cross_sectional_z(
                {currency: components[currency].get(only) for currency in currencies}
            )

        # Only the components carrying a sub-weight are z-scored. A pillar may
        # emit others from `_transform` to fill `headline_component`, and those
        # must not reach the arithmetic at any stage.
        component_z = {
            component: self.cross_sectional_z(
                {
                    currency: components[currency].get(component)
                    for currency in currencies
                }
            )
            for component in weights
        }
        return self.blend_components(component_z)

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
        usable = {
            currency: value for currency, value in values.items() if value is not None
        }
        if len(usable) < MIN_CROSS_SECTION:
            return dict.fromkeys(values)

        if len(set(usable.values())) == 1:
            # Every usable currency reported the same value. That is a finding
            # and not a gap, so they score zero rather than coming back absent.
            #
            # Tested on the inputs rather than on the variance they produce. The
            # mean of three values of 0.1 is 0.10000000000000002, so each
            # deviation is about -1.4e-17 and the standard deviation is the same
            # size, and dividing one by the other returns -1.0 for all three: a
            # confident score off a cross-section that holds no information,
            # which is the failure this whole module is built to avoid. Only
            # values that happen to be exactly representable, such as 2.5, reach
            # a variance of exactly zero.
            return {
                currency: (0.0 if currency in usable else None) for currency in values
            }

        mean = fsum(usable.values()) / len(usable)
        variance = fsum((value - mean) ** 2 for value in usable.values()) / len(usable)
        deviation = sqrt(variance)
        return {
            currency: (
                (usable[currency] - mean) / deviation if currency in usable else None
            )
            for currency in values
        }

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

        Raises:
            ValueError: If ``lookback_years`` is negative, which would put the
                start of the window after its end and return ``None`` from a
                series that has the history the caller asked for. An empty
                answer and a misconfigured window are different facts.

        """
        if not series:
            return None

        when = asof if asof is not None else max(item.period for item in series)
        earliest = _years_earlier(when, lookback_years)
        window = [item for item in series if earliest <= item.period <= when]
        if len(window) < MIN_TIME_SERIES_WINDOW:
            return None

        readings = [item.value for item in window]
        if len(set(readings)) == 1:
            # A series that has not moved says nothing about whether its newest
            # print is high or low. Tested on the readings rather than on their
            # variance, for the reason `cross_sectional_z` records: a flat
            # history of a value like 0.1 reaches a variance near 1e-34 rather
            # than zero, and dividing by its root would answer a confident
            # number built on no movement at all.
            return None

        mean = fsum(readings) / len(readings)
        # ddof=1: the history is a sample of the process, not the whole of it.
        variance = fsum((value - mean) ** 2 for value in readings) / (len(readings) - 1)
        newest = max(window, key=lambda item: item.period)
        return (newest.value - mean) / sqrt(variance)

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
        if periods < 0:
            raise ValueError(
                f"momentum needs a horizon of zero periods or more, got {periods}. "
                "A negative one indexes back from the front of the series and "
                "returns a real-looking change against the wrong end of it."
            )
        if len(series) < periods + 1:
            return None
        return series[-1].value - series[-1 - periods].value

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
            was used instead. The path belongs in
            ``PillarScore.blend_divisor_path``, a typed field rather than a
            marker inside ``notes``: a score computed under the fallback is not
            on the same scale as one computed under the rolling estimate, so a
            report that does not say which was used is hiding the one fact
            needed to compare two runs, and ``--compare`` reads it for
            correctness rather than for display.

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
        component_freshness: Mapping[str, Mapping[str, float]] | None = None,
    ) -> dict[str, float | None]:
        """Combine per-component z-scores into one z-score per currency.

        Args:
            component_z: ``{component: {currency: z}}``, each component already
                z-scored across the cross-section. Components absent from
                ``weights`` are ignored, which is how a pillar carries a
                report-only value through `_transform` for
                `headline_component` without letting it into the arithmetic.
            weights: Sub-weights per component. Defaults to
                `component_weights`. The floor below is a fraction of what this
                map declares rather than of 1.0, so passing a subset does not
                put every currency below it. A map summing to zero or less
                raises, because it leaves no scale to judge coverage against.
            component_freshness: ``{component: {currency: factor}}`` from
                `BasePillar.component_freshness`, per currency. A component
                missing from a currency's mapping is treated as fully fresh at
                ``1.0``, so a pillar that passes nothing keeps the old
                behaviour. See the renormalisation rule below for what the
                factors change.

        Returns:
            ``{currency: z}``, centred on this run's own cross-section and
            divided by the scale from `blend_divisor`. ``None`` marks a currency
            this pillar cannot speak for, in three separate cases: it holds too
            little of the sub-weight, every component it holds has run past its
            allowance, or it has no usable component at all. Only currencies
            named by a component carrying a sub-weight appear at all, so a
            report-only component cannot put one into the run.

        Raises:
            ValueError: ``weights`` sums to zero or less, which is a pillar
                misconfigured rather than a currency with no data.

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
        handled like any other degenerate cross-section: every covered score
        becomes ``0.0``, and a currency that had no blend stays ``None``,
        because a missing scale cannot manufacture coverage. That branch only
        fires on the run-local path, since `blend_divisor` filters a history
        down to entries above ``1e-9``, and a run-local divisor that small means
        every usable blend was identical.

        Fewer than `MIN_CROSS_SECTION` currencies above the floor means the
        pillar has no cross-section this run, and every currency comes back
        ``None`` including those with complete data. `cross_sectional_z` refuses
        a thin cross-section one stage earlier for the same reason, and the same
        constant is reused rather than a second threshold introduced: with two
        points a z-score is ``+/-1.0`` whatever the gap, so a 0.1 separation and
        a 6.0 separation produce identical output, and one point produces
        ``0.0``, the value this module reserves for every usable currency
        reporting the same reading. Both would arrive at full weight, because
        ``z is not None`` is what `scoring.coverage` credits.

        Clearing stage 3 does not mean this check passes. The two stages thin
        the cross-section by different mechanisms: stage 3 runs per component
        and refuses a component, `MIN_COMPONENT_WEIGHT` runs per currency and
        refuses a currency, so every component can hold three usable currencies
        while only two currencies clear the floor.

        The cost is real and is the right outcome rather than a regrettable one:
        a pillar can drop for all eight currencies because six were thin. A
        cross-sectional score is a claim about where a currency sits relative to
        the others, so the two with complete data have not lost information
        about themselves, they have lost the comparison, and the comparison is
        what the pillar is for. The loss is visible rather than silent, which is
        what makes it acceptable: coverage falls and `coverage_demotion` cuts
        conviction. ``docs/decisions/0008-a-thin-blend-has-no-cross-section.md``
        carries the full argument, and #130 the reproduction.

        Renormalisation rule: for each currency the weighted mean runs over the
        components that currency actually has, and each enters at
        ``u_j * phi_j`` rather than at ``u_j``, where ``phi_j`` is that
        component's freshness factor. Take a two-component blend carrying
        GROWTH's GDP and retail sales sub-weights, 0.30 and 0.20: a GDP print at
        ``0.600`` against a retail sales print at ``1.000`` gives GDP
        ``0.30 * 0.600 = 0.180`` against retail's ``0.20``, so GDP takes 0.474
        of the blend where those two sub-weights alone would give it 0.600.

        That illustrates the arithmetic and is not a state GROWTH can reach, a
        distinction worth drawing because the figure has been read as the
        latter. GROWTH carries four components, so the same GDP print beside
        three fresh ones takes ``0.180 / 0.880``, which is 0.205 against the
        0.300 its sub-weight alone would give it. A GROWTH currency holding only
        GDP and retail sales holds exactly `MIN_COMPONENT_WEIGHT` of the
        sub-weight, so this method returns ``None`` for it and there is no blend
        to take a share of.

        A component at ``phi_j = 0.0`` is past its allowance and contributes
        nothing to the blend, without any special case: its discounted weight is
        already zero. A currency whose components are *all* at ``phi_j = 0.0``
        leaves nothing to renormalise over, and comes back ``None`` rather than
        ``0.0``: the series exist and have all run past their allowance, which
        is an absence of usable data and not a reading of neutral. The
        denominator is tested against ``1e-12`` rather than exactly zero, a
        local guard against a sum of small factors rather than a threshold
        anything is configured with.

        A currency holding at or below `MIN_COMPONENT_WEIGHT` of the pillar's
        sub-weight is returned as ``None`` instead, because at that point the
        surviving components are being asked to speak for the ones that are
        absent. The comparison is ``<=``, and the boundary is where it bites: a
        currency holding exactly half is absent, not scored.
        `MIN_COMPONENT_WEIGHT` carries the reason.

        The floor is judged on the sub-weight **present**, before the freshness
        factors are applied, and the reason is worth stating because the other
        reading is tempting. The floor exists for substitution: it asks whether
        the components that are there are being made to speak for components
        that are not. Uniform staleness creates no substitution. Every component
        of INFLATION shares one release, so judging the floor on the discounted
        total would make the whole pillar vanish the moment its factor crossed
        0.5, which converts the ramp into exactly the cliff section 4.1 of
        ``docs/scoring-spec.md`` says it exists to avoid. Old data with a small
        weight and no data at all are different report lines, and the model
        keeps them different: staleness moves the weight, absence moves the
        ``None``.

        A component present for only part of the cross-section is z-scored
        across the currencies that have it, and the currencies that do not
        renormalise around its absence. Be aware of what that costs: a z-score
        computed across four currencies sits on a different scale from one across
        eight, so the two groups are being ranked against different yardsticks on
        that component. The growth pillar's PMI gap is the live case and carries
        the discussion; the alternative, dropping the component for everyone,
        throws away the best series in a pillar whenever one country is missing.

        """
        sub_weights = self.component_weights if weights is None else weights
        declared = fsum(sub_weights.values())
        if declared <= 0.0:
            raise ValueError(
                f"{self.name} was asked to blend against sub-weights summing "
                f"to {declared}, which leaves no scale to judge coverage on"
            )
        factors = component_freshness or {}

        currencies: list[str] = []
        for component in sub_weights:
            for currency in component_z.get(component, {}):
                if currency not in currencies:
                    currencies.append(currency)

        blends: dict[str, float | None] = {}
        for currency in currencies:
            present: list[float] = []
            discounted: list[tuple[float, float]] = []
            for component, weight in sub_weights.items():
                z = component_z.get(component, {}).get(currency)
                if z is None:
                    continue
                present.append(weight)
                phi = factors.get(component, {}).get(currency, 1.0)
                discounted.append((weight * phi, z))

            # The floor is judged on the sub-weight present, before the
            # freshness factors, because it asks about substitution rather than
            # about age. See `MIN_COMPONENT_WEIGHT`.
            if fsum(present) / declared <= MIN_COMPONENT_WEIGHT:
                blends[currency] = None
                continue

            denominator = fsum(weight for weight, _ in discounted)
            if denominator <= 1e-12:
                # Every component the currency has is past its allowance, so
                # there is no weight left to renormalise over. That is an
                # absence, not a reading of zero.
                blends[currency] = None
                continue

            blends[currency] = (
                fsum(weight * z for weight, z in discounted) / denominator
            )

        usable = [blend for blend in blends.values() if blend is not None]
        if len(usable) < MIN_CROSS_SECTION:
            # Too thin to standardise against, so the pillar has no
            # cross-section this run and says so for everyone. ADR 0008.
            return dict.fromkeys(blends)

        mean = fsum(usable) / len(usable)
        variance = fsum((blend - mean) ** 2 for blend in usable) / len(usable)
        run_sd = sqrt(variance)
        divisor, path = self.blend_divisor(run_sd)
        # Recorded rather than discarded. Both facts are computed here and
        # nowhere else, and `compute` has to put them on every score without
        # re-deriving the blend, which would be the same arithmetic in two
        # places and therefore two arithmetics.
        self.last_blend_sd = run_sd
        self.last_blend_divisor_path = path
        if divisor < 1e-9:
            return {
                currency: (0.0 if blend is not None else None)
                for currency, blend in blends.items()
            }

        return {
            currency: ((blend - mean) / divisor if blend is not None else None)
            for currency, blend in blends.items()
        }

    # ------------------------------------------------------------------
    # Freshness and absence
    # ------------------------------------------------------------------

    def staleness_days(self, observations: Sequence[Observation], asof: date) -> int:
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
            Returns ``self.config.max_staleness_days + 1`` for an empty set,
            so an absent pillar sorts as stale rather than as fresh.

        An instance method rather than a static one, and the reason is the empty
        set. The sentinel is a configured value plus one, so a static method can
        only reach it by writing the number out or by building a default
        `ScoringConfig`, and both stop tracking a run that overrode the ceiling.
        This one returned 46 against a configured 60, which is inside the
        allowance, so the ramp reported an absent pillar as a late release at
        partial weight rather than as absent. See issue #28.

        **`missing_score` is the intended path for an absent pillar**, not this
        sentinel. An absent pillar needs ``z`` set to ``None``, which is what the
        aggregator detects and what excludes the weight from the composite; the
        age is only how such a pillar sorts once it is already marked absent.
        `missing_score` defaults its own ``staleness_days`` argument to the same
        expression for that reason. Reaching for this value to build the absent
        case would produce a pillar that looks stale and still carries weight.

        """
        if not observations:
            return self.config.max_staleness_days + 1
        newest = max(observation.period for observation in observations)
        return max(0, (asof - newest).days)

    def component_freshness(
        self,
        extracted: Mapping[str, Sequence[Observation]],
        asof: date,
    ) -> dict[str, float]:
        """Return the freshness factor of each component, in ``[0.0, 1.0]``.

        Args:
            extracted: One currency's slice of `_extract`'s output,
                ``{indicator: observations}``. The visibility rule has already
                been applied there, so nothing here re-checks what was publishable
                at ``asof``: these are the observations the run is allowed to see.
            asof: Run date.

        Returns:
            ``{component: factor}`` over the components in
            `component_indicators` that this currency has data for. A component
            with no observations at all is **absent from the mapping**, not
            present with a factor of ``0.0``. The two are different answers: an
            absent component lowers the sub-weight the currency holds, which is
            what `MIN_COMPONENT_WEIGHT` judges, while a component present at
            ``0.0`` is a series that exists and has run past its allowance.

        Each indicator is aged with `staleness_days`, from ``period``, and judged
        against its own `staleness_allowance`. A component built from more than
        one indicator takes the lowest of their factors, because a component is
        as stale as its stalest input.

        Sign and units do not enter here. This is an age in days turned into a
        weight multiplier, and it never touches the sign of a score.

        """
        factors: dict[str, float] = {}
        for component, indicators in self.component_indicators.items():
            per_indicator = [
                freshness(
                    self.staleness_days(extracted[indicator], asof),
                    self.config,
                    staleness_allowance(indicator, self.config),
                )
                for indicator in indicators
                if extracted.get(indicator)
            ]
            if per_indicator:
                factors[component] = min(per_indicator)
        return factors

    def pillar_freshness(
        self,
        extracted: Mapping[str, Sequence[Observation]],
        asof: date,
    ) -> float:
        """Return the factor this pillar's configured weight is multiplied by.

        Args:
            extracted: One currency's slice of `_extract`'s output.
            asof: Run date.

        Returns:
            The sub-weighted mean of `component_freshness` over the components
            the currency actually has, in ``[0.0, 1.0]``. ``0.0`` when the
            currency has no component at all, since no data is not fresh data.

        The mean runs over the components present rather than over all of them,
        so a missing component is handled once, by `MIN_COMPONENT_WEIGHT` and the
        renormalisation in `blend_components`, and is not charged again here as
        if it were stale. Staleness and absence are separate facts and each is
        counted in exactly one place.

        `compute` records the result on `PillarScore.freshness_factor`, and
        `scoring.score_currencies` reads it there and passes it to
        `scoring.apply_staleness_penalty` as ``freshness_factor``. It travels on
        the score rather than being recomputed by the scorer because this method
        needs the extracted slice, which `compute` holds and the scorer is never
        given. A pillar-level scalar computed from ``PillarScore.staleness_days``
        alone cannot do this job: GROWTH holding a five-month-old GDP print and a
        five-day-old retail sales print would report the age of the retail print
        and take full weight.

        """
        factors = self.component_freshness(extracted, asof)
        weights = self.component_weights
        present = sum(weights[component] for component in factors)
        if present <= 0.0:
            return 0.0
        return sum(weights[c] * phi for c, phi in factors.items()) / present

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
            staleness_days: Override for the reported age. Defaults to
                ``ScoringConfig.max_staleness_days + 1``, marking the pillar as
                past its useful life. It is reported, not acted on: the discount
                is decided by ``freshness_factor`` below, so overriding this does
                not buy an absent pillar any weight back.

        Returns:
            A neutral `PillarScore` carrying this pillar's configured weight and
            a ``freshness_factor`` of ``0.0``, so the weight survives
            `scoring.apply_staleness_penalty` as zero and the pillar drops out of
            `coverage`. ``z`` is ``None``, which is the marker the aggregator
            reads; the weight is what a report shows to say what the run lost.

            The ``0.0`` is stated rather than left to the age-based fallback,
            which reaches the same answer today only because the default age is
            one day past the ramp. A run configuring a higher
            ``max_staleness_days`` would separate them and hand an absent pillar
            partial weight, which is issue #28 one level down.

        """
        age = (
            self.config.max_staleness_days + 1
            if staleness_days is None
            else staleness_days
        )
        return PillarScore(
            pillar=self.name,
            currency=currency,
            raw=None,
            z=None,
            score=0.0,
            weight=self.weight,
            asof=asof,
            staleness_days=age,
            notes=notes,
            # No data is not fresh data, which is `pillar_freshness`'s own rule
            # for a currency holding no component at all. Stating it here rather
            # than leaving ``None`` keeps an absent pillar off the age-based
            # fallback, where the sentinel age happens to give 0.0 as well and
            # the agreement would be a coincidence rather than a decision.
            freshness_factor=0.0,
        )


def _vintage_key(observation: Observation) -> tuple[int, datetime]:
    """Order two observations of the same period by which vintage is current.

    Args:
        observation: The observation to key.

    Returns:
        ``(revision, the stamp)``. Revision leads. A naive stamp is read as UTC
        for the comparison only, so a source that omits the zone cannot raise
        here by being compared against one that supplies it. That is a real
        shape rather than a defensive guess: `ManualSource` reads a zoneless
        stamp as UTC by the same convention, so an `Observation` reaching a
        pillar naive has come from somewhere that did not.

        An unstamped observation takes `_EARLIEST` and so sorts below every
        real stamp of the same revision. An earlier draft carried a third
        element flagging whether a stamp existed; it was dead, because
        `_EARLIEST` already does that work.

    """
    stamped = observation.released_at
    if stamped is not None and stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return (observation.revision, stamped if stamped is not None else _EARLIEST)
