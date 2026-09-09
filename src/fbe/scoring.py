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
    "STALENESS_GRACE_FRACTION",
]


STALENESS_GRACE_FRACTION: float = 0.5
"""Fraction of ``max_staleness_days`` a pillar keeps its full score for.

Macro data is old the moment it is published, so penalising age from day one
would penalise the model for the release calendar rather than for anything it
did. Inside the grace period a pillar counts in full; past it the score decays
linearly to zero at ``max_staleness_days``.
"""


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
        pillar_scores: This currency's scores, keyed by pillar.
        weights: Pillar weights from `ScoringConfig`, summing to 1.0.

    Returns:
        The weighted mean of the usable pillar scores, on the same ``-3..+3``
        band as the inputs. ``0.0`` when nothing is usable.

    The renormalisation rule: a pillar counts only when its ``z`` is not
    ``None``, and the weighted sum is divided by the weight that counted rather
    than by 1.0. Without that division a currency missing the monetary pillar
    would have its composite shrunk by 30% and would drift toward neutral for a
    reason that has nothing to do with its fundamentals, which is a worse error
    than the one renormalising introduces.

    The error renormalising does introduce is worth naming, because it is what
    `coverage` exists to expose. Dividing by the present weight means the
    surviving pillars are asked to speak for the absent ones. A currency scored
    on the monetary pillar alone gets that pillar's score as its whole composite,
    stated with exactly the same confidence as a currency scored on all seven. The
    arithmetic cannot tell those apart, so it does not try: it reports the
    composite and reports the coverage beside it, and `bias.conviction_for` is
    where the difference is made to matter.

    Weights are read from ``weights`` rather than from ``PillarScore.weight``.
    The score carries its weight for audit, so a report can reproduce the sum
    without config, but the config is the authority.

    """
    raise NotImplementedError


def coverage(
    pillar_scores: Mapping[PillarName, PillarScore],
    weights: Mapping[PillarName, float],
) -> float:
    """Return the fraction of pillar weight that had usable data.

    Args:
        pillar_scores: This currency's scores, keyed by pillar.
        weights: Pillar weights from `ScoringConfig`.

    Returns:
        A value in ``[0.0, 1.0]``: the summed weight of the usable pillars
        divided by the summed weight of every pillar in ``weights``. ``1.0``
        means all seven pillars scored the currency, ``0.70`` means the monetary
        pillar was missing, and so on.

    A pillar is usable when both of these hold: its ``z`` is not ``None``, and
    its ``staleness_days`` is at or below ``ScoringConfig.max_staleness_days``.
    The second condition is why staleness is a coverage question and not only a
    score question. A pillar built on data from four months ago is not a weak
    signal that deserves a small score, it is an absent signal wearing a stale
    number, and `apply_staleness_penalty` is what converts one into the other.

    The denominator is the configured weight, not the weight of the pillars that
    ran. So a run with only six of the seven pillars wired up reports coverage of
    at most 0.90 for every currency, which is the correct reading: the engine is
    working from a partial model, and the number should say so rather than
    quietly renormalising the shortfall out of sight.

    """
    raise NotImplementedError


def dispersion(pillar_scores: Mapping[PillarName, PillarScore]) -> float:
    """Return how much the usable pillars disagree about a currency.

    Args:
        pillar_scores: This currency's scores, keyed by pillar.

    Returns:
        The population standard deviation, ``ddof=0``, of the ``score`` values
        of the usable pillars. ``0.0`` when fewer than two pillars are usable,
        which is a floor and not a finding; the low coverage alongside it is the
        real signal in that case.

    Deliberately unweighted. The question dispersion answers is whether the
    pillars tell one story or several, and a 0.10-weight pillar shouting the
    opposite of the other six is exactly the disagreement worth catching.
    Weighting it would let the 0.30 monetary pillar drown out the argument it is
    having with everyone else. This is the one place in the engine where the
    minor pillars get an equal vote, and it is on purpose.

    Reading the number: dispersion near zero means every channel points the same
    way, which is the setup worth sizing into. Dispersion above roughly 1.0 on a
    ``-3..+3`` band means the pillars are describing different economies, usually
    because a risk regime and a rates story are pulling a haven currency apart,
    and the composite in the middle is an average of two real views rather than a
    view of its own. It feeds conviction downstream through the pair's
    `agreement` figure, which is the pairwise version of the same question.

    """
    raise NotImplementedError


def apply_staleness_penalty(
    pillar_score: PillarScore,
    config: ScoringConfig,
    asof: date,
) -> PillarScore:
    """Fade a pillar's score as its inputs age, and drop it once they expire.

    Args:
        pillar_score: The score as the pillar produced it.
        config: Scoring configuration supplying ``max_staleness_days``.
        asof: The date the run represents.

    Returns:
        A new `PillarScore`. Nothing is mutated: `PillarScore` is frozen, and a
        report needs the original alongside the penalised version to explain
        itself.

    The ramp, with the default ``max_staleness_days`` of 45:

        0 to 22 days: full score. Inside the grace period set by
        `STALENESS_GRACE_FRACTION`, a monthly release is simply doing what
        monthly releases do and there is nothing to penalise.

        23 to 45 days: the score is multiplied by a factor falling linearly from
        1.0 to 0.0. The data is now late relative to its own schedule, which
        usually means a source outage rather than a quiet month.

        Over 45 days: ``z`` is set to ``None`` and ``score`` to ``0.0``. Past the
        limit the pillar stops counting toward `coverage` entirely, so the
        currency's composite renormalises around it and the report shows the
        shortfall. The reason is appended to ``notes``.

    Why fade rather than cut. A hard cutoff at 45 days would let a currency's
    composite jump on a day when no data changed and nothing happened except the
    calendar turning over. The ramp spreads that adjustment across three weeks,
    which matches the horizon the engine's output is held for.

    Note the asymmetry with `coverage`, which is not a bug: a pillar at 40 days
    still counts as covered while contributing only a fifth of its score. It has
    data, the data is just old, and the composite is already discounting it.

    """
    raise NotImplementedError
