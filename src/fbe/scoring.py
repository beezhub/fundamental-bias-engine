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
        2. Apply `apply_staleness_penalty` to each `PillarScore`.
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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def freshness(staleness_days: int, config: ScoringConfig) -> float:
    """Return the freshness factor a pillar's weight is multiplied by.

    Args:
        staleness_days: Age in days of the newest input the pillar used, from
            ``PillarScore.staleness_days``.
        config: Scoring configuration supplying ``staleness_full_days`` and
            ``max_staleness_days``.

    Returns:
        A factor in ``[0.0, 1.0]``. With the defaults of 15 and 45 days:

            ``s <= 15``: ``1.0``. Most G10 monthly macro is published inside
            fifteen days of the period it describes, so data this age is simply
            data, and there is nothing to penalise.

            ``15 < s <= 45``: ``(45 - s) / 30``, falling linearly to zero. Past
            fifteen days the release is late relative to its own schedule, which
            usually means a source outage rather than a quiet month.

            ``s > 45``: ``0.0``. The pillar stops counting toward coverage
            entirely.

    Why a ramp rather than a cliff. A hard cutoff would let a currency's
    composite jump on a day when no data changed and nothing happened except the
    calendar turning over. Spreading the adjustment over thirty days matches the
    horizon the engine's output is held for.

    """
    raise NotImplementedError


def apply_staleness_penalty(
    pillar_score: PillarScore,
    config: ScoringConfig,
    asof: date,
) -> PillarScore:
    """Discount a pillar's weight by the age of its inputs.

    Args:
        pillar_score: The score as the pillar produced it, carrying its
            configured weight.
        config: Scoring configuration supplying the staleness thresholds.
        asof: The date the run represents. Present so the penalty can be
            recomputed against a date other than the one the pillar used, which
            a backtest replaying stored scores needs.

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

    Past ``max_staleness_days`` the freshness factor is ``0.0``, so the pillar's
    effective weight is zero, it drops out of `coverage`, and the composite
    renormalises around it. ``z`` is also set to ``None`` at that point, since it
    is the ``None`` marker that the rest of the module tests for, and the reason
    is appended to ``notes``.

    """
    raise NotImplementedError
