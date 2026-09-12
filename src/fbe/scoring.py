"""Aggregation: seven pillar scores per currency into one number and a rank.

Why normalisation is cross-sectional, everywhere except where it is stated
otherwise. This model has an opinion about ordering, not about levels. It does
not claim to know whether the euro is cheap, and it has no view on where
EUR/USD should trade. What it claims is that on this date the euro's
fundamentals are better or worse than the dollar's, and by roughly how much
relative to the spread of the other six. That is the only claim a relative-value
framework is entitled to make, and it happens to be the only claim an FX pair
can express: a pair is a ratio, so an absolute view on one currency is not
tradeable without an absolute view on the other.

The practical consequence is that every number here is defined against the other
seven currencies on the same day. A z-score of ``+1.5`` on the monetary pillar
does not mean rates are high; it means rates are high compared with the rest of
the G10 right now. It follows that scores are not comparable across dates. A
composite of ``+2.0`` in a run where the whole universe is tightening is a
different economic statement from ``+2.0`` in a run where nothing is moving, and
the engine deliberately declines to distinguish them: what it exports is the
ranking and the gaps, and the gaps are what `bias.py` differences into pairs.

Everything in this module is pure. Pillars have already done the fetching and
the economics; the scorer only weights, aggregates, penalises for age, and
ranks.

`series_loading` is the one function here that does not take part in a run. It
answers what the model's total response to a single underlying series is, which
is a different question from what a pillar weighs, and it lives at this layer
because answering it needs both a pillar's sub-weights and the pillar weights in
`ScoringConfig`. It exists because `real_policy_rate` puts a coefficient of
minus one on ``cpi_yoy`` inside MONETARY while INFLATION loads on the same
series positively, so no declared weight is the model's inflation response. See
``docs/decisions/0003-publish-effective-loadings-rather-than-remove-real-policy-rate.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from fbe.config import ScoringConfig
from fbe.types import CurrencyScore, Observation, Pillar, PillarName, PillarScore

__all__ = [
    "score_currencies",
    "composite",
    "coverage",
    "dispersion",
    "series_loading",
    "apply_staleness_penalty",
    "freshness",
]


def score_currencies(
    observations: Sequence[Observation],
    pillars: Sequence[Pillar],
    config: ScoringConfig,
    asof: date,
) -> Sequence[CurrencyScore]:
    """Run every pillar, weight the results, and rank the universe.

    The pipeline, in order:

        1. Call ``pillar.compute`` once per pillar over the whole universe. The
           full observation set goes to every pillar because normalisation is
           cross-sectional and no pillar can judge one currency in isolation.
        2. Apply `apply_staleness_penalty` to each `PillarScore`, passing the
           pillar's own `BasePillar.pillar_freshness` as the factor. The pillar
           computes it because the per-indicator allowance is a registry fact and
           the indicator keys are the pillar's, not the scorer's. Omitting it
           falls back to the default ramp, which reads every series as if it
           published monthly.
        3. Build each currency's `composite`, `coverage` and `dispersion`.
        4. Rank by composite, descending, so rank 1 is the strongest currency.

    Args:
        observations: Every observation fetched for this run, all currencies and
            all indicators.
        pillars: The pillars to run, normally `pillars.default_pillars`. A pillar
            absent from this sequence contributes no weight and shows up as
            reduced coverage on every currency, which is how a pillar is
            switched off for a run without editing the weights.
        config: Scoring configuration supplying the weights, the clip band and
            the staleness limit.
        asof: The date the run represents.

    Returns:
        One `CurrencyScore` per currency the pillars scored, sorted by composite
        descending with `rank` filled in. Ties are broken by ISO code so that two
        identical runs produce byte-identical reports.

    Raises:
        ValueError: If two pillars in ``pillars`` share a `PillarName`, since
            the second would silently overwrite the first in the weight map.

    A pillar that raises is not allowed to take the run down with it. The
    exception is caught, every currency receives that pillar's `missing_score`,
    and the reason is recorded so it reaches ``BiasReport.warnings``. The engine
    is more useful running on six pillars than not running at all, and the
    coverage figure is what tells the reader which case they are looking at.

    """
    raise NotImplementedError(
        "fbe.scoring.score_currencies is scaffolded; see docs/roadmap.md Phase 2"
    )


def composite(
    pillar_scores: Mapping[PillarName, PillarScore],
    weights: Mapping[PillarName, float],
) -> float:
    """Combine a currency's pillar scores into one number on the score band.

    Args:
        pillar_scores: This currency's scores, already passed through
            `apply_staleness_penalty`, so each ``PillarScore.weight`` is the
            effective weight ``w_eff`` rather than the configured weight.
        weights: Configured pillar weights from `ScoringConfig`, summing to 1.0.
            Used to detect pillars that did not run at all, which are absent
            from ``pillar_scores`` and therefore contribute no effective weight.

    Returns:
        The effective-weighted mean of the usable pillar scores, on the same
        ``-3..+3`` band as the inputs. ``0.0`` when ``coverage`` is ``0.0``.

    The arithmetic, matching section 4.3 of ``docs/scoring-spec.md``:

        ``composite = sum over p of w_eff(p) * score(p), divided by coverage``

    where ``coverage`` is ``sum over p of w_eff(p)``, so the divisor is the
    weight that actually counted rather than 1.0.

    The renormalisation rule. A pillar counts only when its ``z`` is not
    ``None``. Dividing by the present weight rather than by 1.0 is what stops a
    currency missing the monetary pillar from having its composite shrunk by 30%
    and drifting toward neutral for a reason that has nothing to do with its
    fundamentals. A missing pillar is an absence of evidence, not evidence of
    neutrality.

    The cost of renormalising is worth naming, because it is what `coverage`
    exists to expose. Dividing by the present weight means the surviving pillars
    are asked to speak for the absent ones. A currency scored on the monetary
    pillar alone gets that pillar's score as its whole composite, stated with
    exactly the same confidence as a currency scored on all seven. The arithmetic
    cannot tell those apart, so it does not try: it reports the composite and
    reports the coverage beside it, and the conviction stage in `bias.py` and the
    hard filter are where the difference is made to matter.

    Staleness has already been applied to the weights, not to the scores, so
    there is no second discount here. An old pillar arrives with a smaller
    ``w_eff`` and therefore a smaller share of the composite, which is the same
    effect achieved once instead of twice.

    """
    raise NotImplementedError(
        "fbe.scoring.composite is scaffolded; see docs/roadmap.md Phase 2"
    )


def coverage(
    pillar_scores: Mapping[PillarName, PillarScore],
    weights: Mapping[PillarName, float],
) -> float:
    """Return the fraction of pillar weight that had usable, fresh data.

    Args:
        pillar_scores: This currency's scores, already passed through
            `apply_staleness_penalty`.
        weights: Configured pillar weights from `ScoringConfig`.

    Returns:
        A value in ``[0.0, 1.0]``: ``sum over p of w_eff(p)`` across the usable
        pillars. Since the configured weights sum to 1.0, that sum is directly
        the fraction of the model that had something to say. ``1.0`` means all
        seven pillars scored the currency on fresh data, ``0.70`` means the
        monetary pillar was missing or fully expired, and so on.

    A pillar contributes ``w_eff = weight * freshness`` when its ``z`` is not
    ``None``, and ``0.0`` otherwise. Note that freshness is continuous, so
    coverage is too: a pillar whose newest input is 30 days old contributes half
    its weight rather than all or none of it. That is the point of doing the
    discount on the weight instead of on the score. Age reduces how much a pillar
    is allowed to say, and `coverage` is where the reduction becomes visible.

    A pillar absent from ``pillar_scores`` entirely, because it was not in the
    run, contributes nothing. So a run wired up with only six of the seven
    pillars reports coverage of at most 0.90 for every currency, which is the
    correct reading: the engine is working from a partial model and the number
    should say so rather than quietly renormalising the shortfall out of sight.

    Coverage of exactly ``0.0`` is a special case handled downstream: the
    currency has no composite, and every pair using it is blocked with
    ``no_coverage``.

    """
    raise NotImplementedError(
        "fbe.scoring.coverage is scaffolded; see docs/roadmap.md Phase 2"
    )


def dispersion(
    pillar_scores: Mapping[PillarName, PillarScore],
    weights: Mapping[PillarName, float],
) -> float:
    """Return how much the usable pillars disagree about a currency.

    Args:
        pillar_scores: This currency's scores, already passed through
            `apply_staleness_penalty`.
        weights: Configured pillar weights from `ScoringConfig`.

    Returns:
        The effective-weighted standard deviation of the pillar scores about the
        composite, on the score band. ``0.0`` when fewer than two pillars are
        usable, which is a floor and not a finding; the low coverage alongside it
        is the real signal in that case.

    The arithmetic, matching section 4.4 of ``docs/scoring-spec.md``:

        ``w_tilde(p) = w_eff(p) / coverage``, which sums to 1.0

        ``dispersion = sqrt( sum over p of w_tilde(p) * (score(p) - composite)^2 )``

    Deviations are taken about the composite rather than about the unweighted
    mean of the scores, so this measures dispersion about the number the engine
    actually publishes.

    Weighted rather than unweighted. The question is whether the composite is a
    consensus or an average of arguments, and a pillar's contribution to that
    question is proportional to its contribution to the composite. A 0.10-weight
    pillar disagreeing moves the composite by little and should move the
    dispersion by little; the monetary pillar at 0.30 disagreeing is the case
    worth catching. Using ``w_tilde`` rather than raw ``w_eff`` keeps the measure
    on the score band regardless of how much coverage the currency had, so the
    ``max_dispersion`` threshold means the same thing for a thinly covered
    currency as for a fully covered one.

    Reading the number: near zero means every channel points the same way, which
    is the setup worth sizing into. Above ``ScoringConfig.max_dispersion`` (1.20)
    the pillars are typically more than a full band unit from the composite,
    which in practice means at least one is arguing hard in the other direction,
    usually a risk regime and a rates story pulling a haven currency apart. That
    threshold demotes conviction on every pair using the currency, in
    `bias.conviction_for`.

    """
    raise NotImplementedError(
        "fbe.scoring.dispersion is scaffolded; see docs/roadmap.md Phase 2"
    )


def series_loading(
    pillar_weight: float,
    sub_weight: float,
    sub_indicator_sd: float,
    blend_divisor: float,
    universe_size: int,
    coefficient: float = 1.0,
) -> float:
    """Return how far one unit of an underlying series moves a composite.

    Args:
        pillar_weight: The pillar's weight from ``ScoringConfig.weights``.
        sub_weight: The component's sub-weight inside that pillar, from the
            pillar's ``component_weights``.
        sub_indicator_sd: Cross-sectional standard deviation of the component,
            in the component's own units, from this run's stage 3.
        blend_divisor: The pillar's divisor from `BasePillar.blend_divisor`,
            whichever path it took. A loading computed under the ``run_local``
            fallback is not on the same scale as one computed under the rolling
            estimate, so quote the path alongside the figure.
        universe_size: Number of currencies in the cross-section.
        coefficient: The series' coefficient inside the component's
            transformation. ``+1.0`` where the component is the series or a
            constant offset of it, such as ``cpi_gap = cpi_yoy -
            inflation_target``. ``-1.0`` for ``real_policy_rate =
            policy_rate - cpi_yoy``.

    Returns:
        Composite band points per one unit of the series, in whatever unit the
        series is published in: percentage points for ``cpi_yoy``, basis points
        for ``yield_2y_chg_3m``. Sign convention is the engine's throughout, so
        a positive result means a rise in the series raises the composite and is
        currency-strengthening, and a negative result means the opposite.

    Raises:
        ValueError: If ``sub_indicator_sd`` or ``blend_divisor`` is not
            positive, or ``universe_size`` is below 2. Each of those is a
            cross-section that cannot be normalised, so there is no loading to
            report. Returning zero would read as "this series does not matter",
            which is a different statement and a false one.

    The arithmetic:

        ``loading = pillar_weight * sub_weight * coefficient *
        ((n - 1) / n) / (sub_indicator_sd * blend_divisor)``

    The ``(n - 1) / n`` factor is there because the normalisation is
    cross-sectional. One currency's print moving by a unit also moves the mean
    it is measured against, by a unit over ``n``, so its own z-score responds by
    seven eighths of the naive amount at ``n = 8``. Omitting it overstates every
    loading by about 14%.

    There is no second such factor for the re-standardisation pass in section
    2.3 of ``docs/scoring-spec.md``. Every component arrives at the blend
    already centred on the cross-section, so the blend's mean is identically
    zero and subtracting it is a no-op rather than a term with a derivative.

    This is a local linearisation that holds both dispersions fixed. Both move
    when the underlying data moves, so the figure describes the model's response
    to a small change around one run's cross-section and not to a large one. It
    is a property of the model, not a measurement of anything the model
    predicts.

    To get the model's total loading on a series that appears in more than one
    pillar, call this once per appearance and sum. That total is the number a
    reader assumes a declared weight gives them, and for ``cpi_yoy`` it does
    not: MONETARY's ``real_policy_rate`` term carries a coefficient of minus one
    on the same series INFLATION loads on positively. Section 3.2 of
    ``docs/scoring-spec.md`` publishes both sides on the section 7 fixture.

    """
    if sub_indicator_sd <= 0.0:
        raise ValueError(
            f"sub_indicator_sd must be positive, got {sub_indicator_sd!r}; "
            "a cross-section with no dispersion has no defined loading"
        )
    if blend_divisor <= 0.0:
        raise ValueError(
            f"blend_divisor must be positive, got {blend_divisor!r}; "
            "a blend with no dispersion has no defined loading"
        )
    if universe_size < 2:
        raise ValueError(
            f"universe_size must be at least 2, got {universe_size!r}; "
            "a cross-sectional z-score needs something to compare against"
        )

    own_share_of_the_move = (universe_size - 1) / universe_size
    return (
        pillar_weight
        * sub_weight
        * coefficient
        * own_share_of_the_move
        / (sub_indicator_sd * blend_divisor)
    )


def freshness(
    staleness_days: int,
    config: ScoringConfig,
    allowance_days: int | None = None,
) -> float:
    """Return the freshness factor a weight is multiplied by, in ``[0.0, 1.0]``.

    Args:
        staleness_days: Age in days of the input being judged, measured from
            ``Observation.period``, which is the first day of the span the
            figure describes.
        config: Scoring configuration supplying the ramp's shape,
            ``staleness_full_days / max_staleness_days``.
        allowance_days: How old this particular series may be and still count,
            from ``IndicatorSpec.max_staleness_days``. ``None`` means the caller
            has no per-series allowance and the ramp falls back to
            ``ScoringConfig.max_staleness_days``, which is the right answer only
            for a series that publishes about as often as the default assumes.

    Returns:
        The factor, where ``S`` is the allowance and ``s0 = S * (
        staleness_full_days / max_staleness_days)``, one third of ``S`` on the
        shipped defaults:

            ``s <= s0``: ``1.0``. The series is inside its own schedule.

            ``s0 < s <= S``: ``(S - s) / (S - s0)``, falling linearly to zero as
            the release runs later and later.

            ``s > S``: ``0.0``. Past the allowance the input stops counting
            toward coverage.

    Raises:
        ValueError: If ``allowance_days`` is not positive. A zero or negative
            allowance is a configuration error, not a stale series, and
            returning 0.0 for it would hide the difference.

    Why the ramp is scaled to the series rather than measured in absolute days.
    ``period`` is the first day of the span a figure describes, so a punctual
    monthly print is 30 to 45 days old the day it publishes and a punctual
    quarterly print is about 120 days old. One 45-day ramp for everything gave
    thirteen of the seventeen registered indicators a factor of 0.0 at the age
    their own frequency says they publish at, which is every monthly and
    quarterly series in the model. Scaling by the allowance the registry already
    derives from each series' release calendar means a series that has just been
    published counts as fresh and a series that has missed its release does not.

    The allowance also keeps this function and `registry.coverage_report` from
    giving two answers about the same series. ``SeriesRef.stale_on`` compares
    ``age > allowance``, so the ramp reaches zero on the last day the registry
    still counts a ref as usable: the scoring side is never the more permissive
    of the two.

    Why a ramp rather than a cliff. A hard cutoff would let a currency's
    composite jump on a day when no data changed and nothing happened except the
    calendar turning over.

    """
    allowance = config.max_staleness_days if allowance_days is None else allowance_days
    if allowance <= 0:
        raise ValueError(
            f"allowance_days is {allowance}, expected above 0; "
            "an indicator with no usable allowance cannot be scored"
        )
    full = allowance * (config.staleness_full_days / config.max_staleness_days)
    if staleness_days <= full:
        return 1.0
    if staleness_days > allowance:
        return 0.0
    return (allowance - staleness_days) / (allowance - full)


def apply_staleness_penalty(
    pillar_score: PillarScore,
    config: ScoringConfig,
    asof: date,
    freshness_factor: float | None = None,
) -> PillarScore:
    """Discount a pillar's weight by the age of its inputs.

    Args:
        pillar_score: The score as the pillar produced it, carrying its
            configured weight.
        config: Scoring configuration supplying the staleness thresholds.
        asof: The date the run represents. Present so the penalty can be
            recomputed against a date other than the one the pillar used, which
            a backtest replaying stored scores needs.
        freshness_factor: The pillar's own freshness in ``[0.0, 1.0]``, from
            `BasePillar.pillar_freshness`, which ages each component against its
            own indicator allowance and averages over the sub-weights present.
            ``None`` falls back to ``freshness(pillar_score.staleness_days,
            config)``, which knows no allowance and therefore judges every
            series as if it published monthly. That fallback is the conservative
            answer, not the correct one: it is what produced a factor of 0.0 for
            every quarterly series in the model, so a caller that can supply the
            factor must.

    Returns:
        A new `PillarScore` whose ``weight`` is ``weight * freshness(...)``.
        Nothing is mutated: `PillarScore` is frozen, and a report needs the
        original alongside the penalised version to explain itself.

    The discount lands on the weight, never on the score. This is the part worth
    being careful about. A stale pillar has not changed its mind, it has simply
    stopped being able to see, so the right response is to give it less of a vote
    rather than to move its vote toward zero. Shrinking the score instead would
    also double-count, because `composite` renormalises by the weight that
    counted: a pillar whose score was halved and whose weight was left alone
    would drag the composite toward neutral, which is the exact error
    renormalisation exists to prevent.

    Past its allowance the freshness factor is ``0.0``, so the pillar's
    effective weight is zero, it drops out of `coverage`, and the composite
    renormalises around it. ``z`` is also set to ``None`` at that point, since it
    is the ``None`` marker that the rest of the module tests for, and the reason
    is appended to ``notes``.

    The allowance is per indicator and lives in the registry, so the lookup
    happens on the pillar side where the indicator keys are known. This module
    stays free of the registry: it takes a factor and a ramp, and computes.

    """
    raise NotImplementedError(
        "fbe.scoring.apply_staleness_penalty is scaffolded; see docs/roadmap.md Phase 2"
    )
