"""What every pillar did on a run, and whether two of them measure one thing.

Phase 3's definition of done asks two questions this module answers. They look
unrelated and are the same question twice: is the composite built from seven
measurements, or from fewer wearing seven names and a few silent gaps.

**Accounting.** For each currency, which pillars produced a score and which came
back absent, and for each absence which of three things happened. A composite of
``+0.94`` on seven pillars and the same figure on four is a different claim, and
`fbe.scoring.coverage` reduces to say so, but coverage is one number and cannot
name what is missing or why.

**Correlation.** Two pillars whose scores move together across the
cross-section are one pillar with two weights. `ScoringConfig` declares MONETARY
at 0.30 and INFLATION at 0.15, and if those two columns correlate at 0.95 the
run is not weighting two views at 0.45, it is weighting one view at 0.45 while
believing it has diversified. That is the same class of error as the effective
loadings problem `fbe.scoring.series_loading` exists for, one layer up:
`series_loading` catches one series feeding two pillars, and this catches two
pillars arriving at one answer by whatever route.

**Nothing here claims a measured result.** A correlation between two pillar
columns on one run's eight currencies is arithmetic about that run's inputs. It
says nothing about returns, nothing about whether the model predicts anything,
and nothing about whether the weights are right. `PillarAudit.render` says so in
the output rather than leaving a reader to supply the caveat, and acting on a
figure here means changing `ScoringConfig`, which is a decision with its own
issue and a human in front of it.

This module computes and reports. It folds nothing, reweights nothing, and
changes no score.

Why it sits beside `fbe.scoring` rather than inside it: the scorer aggregates one
currency at a time into a `CurrencyScore`, while everything here is a property of
the whole cross-section after that is done. Folding a cross-sectional diagnostic
into the aggregator would give the aggregator a second reason to change, and the
one thing every other module depends on is that the aggregator's arithmetic
stays still.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import combinations
from math import fsum, isfinite, sqrt

from fbe.scoring import PILLAR_FAILED
from fbe.types import CurrencyScore, PillarName, PillarScore

__all__ = [
    "DUPLICATE_CORRELATION",
    "MIN_CORRELATION_CURRENCIES",
    "AbsenceKind",
    "PillarAbsence",
    "PillarPair",
    "PillarAudit",
    "correlate",
    "pillar_pairs",
    "unmarked_zeros",
    "audit_run",
]


DUPLICATE_CORRELATION: float = 0.9
"""Absolute correlation at or above which two pillars are reported as duplicates.

Local to this module rather than in `ScoringConfig`, on the same grounds as
`fbe.risk.MIN_STOP_SPREAD_MULTIPLE`: it is a reporting threshold and not a
scoring parameter. Nothing refuses on it, no score moves with it, and no
composite would change if it were 0.85. It decides only which pairs are named in
the output as worth a person's attention.

0.9 is the figure Phase 3's definition of done names, so it is transcribed rather
than chosen here. It has not been calibrated against anything: two pillars at
0.88 are nearly as duplicated as two at 0.91, and the output prints every pair's
figure so a reader can see where the run actually sits rather than only which
side of this line it fell.

The comparison is on the absolute value. Two pillars correlated at minus 0.95 are
just as much one measurement as two at plus 0.95, with one of them reversed, and
a run that weights both is taking the same view twice with a sign flip in the
middle.
"""

MIN_CORRELATION_CURRENCIES: int = 3
"""Fewest currencies both pillars must have scored for a correlation to be given.

Two points always correlate at exactly plus or minus one, whatever the two
pillars measure, so a figure computed on two currencies is an artefact of having
two currencies. Three is the smallest cross-section on which the number carries
any information at all, and it matches `fbe.pillars.base.MIN_CROSS_SECTION`,
which refuses a cross-sectional z-score on the same reasoning.

Below it the pair comes back with ``correlation`` of ``None`` and a reason, never
with a plausible figure and never with zero. A zero would read as "these two
pillars are independent", which is the opposite of what too little data means.
"""


class AbsenceKind(StrEnum):
    """Why a currency has no score from a pillar.

    Three states rather than one, because each is a different thing to do about
    it and reporting them as one absence hides two of the three.

    Attributes:
        NOT_RUN: The pillar was not in the run's pillar set at all, so no
            currency has a score from it. Normally deliberate, which is how a
            pillar is switched off for a run without editing the weights, and it
            is reported so that "switched off" and "broken" never read alike.
        PILLAR_FAILED: The pillar raised, so `fbe.scoring.score_currencies`
            caught it and every currency lost the pillar at once. A defect to
            fix, and the run continued on the pillars that worked.
        NO_DATA: The pillar ran and had nothing it could score this currency on.
            Not a defect: NZD has no COT contract and CHF has no front-end
            series of the kind MONETARY reads, and those are facts about the
            universe rather than about the code.

    """

    NOT_RUN = "not_run"
    PILLAR_FAILED = "pillar_failed"
    NO_DATA = "no_data"


@dataclass(frozen=True, slots=True)
class PillarAbsence:
    """One currency's missing score from one pillar.

    Attributes:
        pillar: The pillar that produced nothing.
        currency: ISO code that went without it.
        kind: Which of the three states applies. See `AbsenceKind`.
        reason: The text the pillar or the scorer recorded, carried through
            verbatim from ``PillarScore.notes`` for a reader. Nothing in this
            module parses it: `kind` is what code branches on, and this is prose.
            Empty when the pillar was not run, since no score exists to carry a
            reason.

    """

    pillar: PillarName
    currency: str
    kind: AbsenceKind
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PillarPair:
    """Two pillars and how closely their scores moved together on this run.

    Attributes:
        left: First pillar, in `PillarName` declaration order.
        right: Second pillar, after ``left`` in that order. Each unordered pair
            appears once, and never a pillar against itself.
        correlation: Pearson correlation of the two score columns across the
            currencies both pillars scored, in ``[-1.0, 1.0]``. ``None`` when it
            could not be computed, which is an absence and never a zero: see
            `reason` for which case. Unitless.
        currencies: How many currencies both pillars scored, which is the number
            of points the figure rests on. Present whether or not a correlation
            came back, because "0.95 on three currencies" and "0.95 on eight"
            are different statements and only one of them is worth acting on.
        reason: Why ``correlation`` is ``None``, empty when it is not.

    """

    left: PillarName
    right: PillarName
    correlation: float | None
    currencies: int
    reason: str = ""

    @property
    def duplicated(self) -> bool:
        """Whether this pair is at or above `DUPLICATE_CORRELATION` in absolute value.

        False when ``correlation`` is ``None``. A pair that could not be measured
        is not a pair that was measured and found independent, and this property
        is read to decide what to name in the output, so the distinction has to
        survive here rather than being recovered later.
        """
        return self.correlation is not None and (
            abs(self.correlation) >= DUPLICATE_CORRELATION
        )


@dataclass(frozen=True, slots=True)
class PillarAudit:
    """Everything this module measured about one run.

    Attributes:
        asof: The run's date, carried so a figure read later is tied to the run
            that produced it.
        config_digest: `fbe.config.Config.digest` for that run. A correlation is
            a property of the weights and the data together, and the digest is
            what says which weights were in force. Without it a figure quoted
            three weeks later cannot be placed.
        scored: Pillar to the ISO codes it scored, in universe order. A pillar
            that scored nobody is present with an empty tuple rather than
            absent, so a reader can tell a pillar that produced nothing from one
            this audit never looked at.
        absences: Every missing score, in pillar then currency order.
        pairs: Every unordered pillar pair, 21 of them for seven pillars,
            whether or not a correlation could be computed.

    """

    asof: date
    config_digest: str
    scored: Mapping[PillarName, tuple[str, ...]]
    absences: tuple[PillarAbsence, ...] = field(default_factory=tuple)
    pairs: tuple[PillarPair, ...] = field(default_factory=tuple)

    @property
    def duplicates(self) -> tuple[PillarPair, ...]:
        """The pairs at or above `DUPLICATE_CORRELATION`, strongest first.

        Ordered by absolute correlation descending, then by pillar order, so the
        pair most worth a person's attention is first and two runs with the same
        figures list them identically.
        """
        found = [pair for pair in self.pairs if pair.duplicated]
        found.sort(
            key=lambda pair: (
                -abs(pair.correlation or 0.0),
                pair.left.value,
                pair.right.value,
            )
        )
        return tuple(found)

    def render(self) -> str:
        """Return the audit as text for a person, with its own caveat attached.

        Returns:
            A block naming the run date and digest, then what each pillar
            scored, then every absence with its kind and reason, then the
            correlations. Pairs that could not be measured are printed as such
            rather than omitted, because a pair missing from a list reads as a
            pair that was fine.

            The closing line states that a fold or a reweight is a decision this
            computation does not take. That sentence is part of the output rather
            than of this docstring because the person who needs it is reading the
            output, and because a correlation figure with no caveat beside it is
            the shape an unmeasured claim takes.

        """
        lines = [
            f"Pillar audit for {self.asof.isoformat()}, config digest "
            f"{self.config_digest}.",
            "",
            "Scored:",
        ]
        for pillar in PillarName:
            currencies = self.scored.get(pillar)
            if currencies is None:
                continue
            got = ", ".join(currencies) if currencies else "nobody"
            lines.append(f"  {pillar.value:<12} {len(currencies)}/8  {got}")

        lines.extend(["", "Absent:"])
        if not self.absences:
            lines.append("  nothing. Every pillar scored every currency.")
        for absence in self.absences:
            lines.append(
                f"  {absence.pillar.value:<12} {absence.currency}  "
                f"{absence.kind.value}"
                + (f": {absence.reason}" if absence.reason else "")
            )

        lines.extend(["", "Pillar cross-correlation, this run's cross-section:"])
        for pair in self.pairs:
            label = f"  {pair.left.value:<12} {pair.right.value:<12}"
            if pair.correlation is None:
                lines.append(f"{label} not measured ({pair.reason})")
                continue
            flag = "  DUPLICATE" if pair.duplicated else ""
            lines.append(
                f"{label} {pair.correlation:+.4f} on {pair.currencies} currencies{flag}"
            )

        duplicates = self.duplicates
        lines.append("")
        if duplicates:
            named = ", ".join(
                f"{pair.left.value} and {pair.right.value} at {pair.correlation:+.4f}"
                for pair in duplicates
                if pair.correlation is not None
            )
            lines.append(
                f"At or above {DUPLICATE_CORRELATION:.2f} in absolute value: "
                f"{named}. Two pillars that move together across the "
                "cross-section are one measurement carrying two weights."
            )
        else:
            lines.append(
                f"No pair reached {DUPLICATE_CORRELATION:.2f} in absolute value "
                "on this run's cross-section."
            )
        lines.append(
            "These figures are arithmetic about this run's inputs across eight "
            "currencies. They say nothing about returns and nothing about "
            "whether the model works. Folding two pillars together or "
            "reweighting either one is a decision, not something this "
            "computation performs: it changes ScoringConfig and wants its own "
            "issue and a person's approval."
        )
        return "\n".join(lines) + "\n"


def correlate(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation of two equal-length columns, or `None` when undefined.

    Args:
        left: One pillar's scores, one per currency.
        right: The other pillar's scores, in the same currency order. Must be
            the same length as ``left``.

    Returns:
        The correlation in ``[-1.0, 1.0]``, unitless. ``None`` when either
        column has no spread at all, since the coefficient divides by both
        standard deviations and a constant column makes the quotient undefined
        rather than zero. A pillar that scored every currency the same is a
        pillar carrying no cross-sectional information, and reporting 0.0 for it
        would claim it was measured and found independent.

        Clamped into ``[-1.0, 1.0]``. The unclamped quotient can land a few
        floating-point steps outside on columns that are exact multiples of one
        another, and a correlation printed as ``1.0000000000000002`` invites a
        reader to wonder what else is wrong.

    Raises:
        ValueError: If the two columns are different lengths, or if either holds
            a value that is not finite. A NaN would propagate to a correlation
            of NaN, which fails every comparison and so would silently never be
            reported as a duplicate.

    """
    if len(left) != len(right):
        raise ValueError(
            f"columns must be the same length, got {len(left)} and {len(right)}"
        )
    for label, column in (("left", left), ("right", right)):
        for value in column:
            if not isfinite(value):
                raise ValueError(
                    f"{label} column holds {value!r}, which is not finite. A "
                    "correlation over it fails every comparison rather than "
                    "reporting a figure."
                )

    count = len(left)
    left_mean = fsum(left) / count
    right_mean = fsum(right) / count
    left_spread = [value - left_mean for value in left]
    right_spread = [value - right_mean for value in right]
    left_sq = fsum(value * value for value in left_spread)
    right_sq = fsum(value * value for value in right_spread)
    if left_sq <= 0.0 or right_sq <= 0.0:
        return None
    covariance = fsum(a * b for a, b in zip(left_spread, right_spread, strict=True))
    return max(-1.0, min(1.0, covariance / sqrt(left_sq * right_sq)))


def _scored_columns(
    scores: Sequence[CurrencyScore],
) -> Mapping[PillarName, Mapping[str, float]]:
    """Build each pillar's score column, holding only genuinely scored currencies.

    Args:
        scores: The run's currency scores.

    Returns:
        Pillar to currency to score, with every absence left out. An absence
        carries ``score`` of ``0.0`` with ``z`` of ``None``, and reading that
        zero as a score is the defect this whole module exists to detect: it
        would pull both columns toward the middle and report two pillars as
        independent because they were both absent on the same currencies.

    """
    columns: dict[PillarName, dict[str, float]] = {}
    for row in scores:
        for pillar, score in row.pillars.items():
            if score.z is None:
                continue
            columns.setdefault(pillar, {})[row.currency] = score.score
    return columns


def pillar_pairs(scores: Sequence[CurrencyScore]) -> tuple[PillarPair, ...]:
    """Correlate every pair of pillars across the currencies both scored.

    Args:
        scores: The run's currency scores, normally the whole universe.

    Returns:
        One `PillarPair` per unordered pair of `PillarName`, in declaration
        order, 21 of them for seven pillars. Every pair is present whether or not
        a figure came back: a pair dropped from the list because it could not be
        measured would read as a pair that was measured and found harmless.

        The correlation for each pair is computed over the currencies **both**
        pillars scored, which is the only set on which the two columns are
        comparable. That intersection is reported as ``currencies``, so a figure
        resting on three currencies cannot be mistaken for one resting on eight.

    """
    columns = _scored_columns(scores)
    pairs: list[PillarPair] = []
    for left, right in combinations(list(PillarName), 2):
        left_column = columns.get(left, {})
        right_column = columns.get(right, {})
        shared = sorted(set(left_column) & set(right_column))
        if len(shared) < MIN_CORRELATION_CURRENCIES:
            pairs.append(
                PillarPair(
                    left=left,
                    right=right,
                    correlation=None,
                    currencies=len(shared),
                    reason=(
                        f"both scored only {len(shared)} currencies in common, "
                        f"fewer than the {MIN_CORRELATION_CURRENCIES} a "
                        "correlation needs to mean anything"
                    ),
                )
            )
            continue
        figure = correlate(
            [left_column[currency] for currency in shared],
            [right_column[currency] for currency in shared],
        )
        pairs.append(
            PillarPair(
                left=left,
                right=right,
                correlation=figure,
                currencies=len(shared),
                reason=(
                    ""
                    if figure is not None
                    else (
                        "one of the two scored every shared currency the same, "
                        "so it carries no cross-sectional spread to correlate"
                    )
                ),
            )
        )
    return tuple(pairs)


def unmarked_zeros(scores: Sequence[CurrencyScore]) -> tuple[PillarScore, ...]:
    """Find any score of exactly zero that is neither a reading nor a marked absence.

    The invariant Phase 3 asks for, stated as what it forbids. A pillar score of
    ``0.0`` is legitimate in exactly two shapes:

        * a genuine reading of zero, which carries a ``z`` that is not ``None``.
          RISK sits here when its input is at the middle of its band, and so does
          any pillar whose cross-sectional z-score lands on the mean.
        * a marked absence, which carries ``raw`` and ``z`` both ``None`` and a
          reason in ``notes``. That is `fbe.scoring` and
          `fbe.pillars.base.BasePillar.missing_score`'s contract, and the two
          ``None`` markers are precisely what lets the aggregator tell absence
          of evidence from evidence of neutrality.

    Anything else is a zero nobody can classify: a ``z`` of ``None`` beside a
    ``raw`` that is not, or the reverse, is half-marked, and a consumer testing
    one of the two fields reaches the opposite conclusion from one testing the
    other.

    Args:
        scores: The run's currency scores.

    Returns:
        Every offending `PillarScore`, empty when the run is clean. Returned
        rather than raised: this is a diagnostic over a finished run, and a
        caller that wants it fatal can check for emptiness, while one writing a
        report wants to print all of them.

    """
    offenders: list[PillarScore] = []
    for row in scores:
        for score in row.pillars.values():
            if score.score != 0.0:
                continue
            marked_absence = score.z is None and score.raw is None
            genuine_reading = score.z is not None
            if not (marked_absence or genuine_reading):
                offenders.append(score)
    return tuple(offenders)


def audit_run(
    scores: Sequence[CurrencyScore],
    *,
    config_digest: str,
    asof: date | None = None,
) -> PillarAudit:
    """Account for every pillar on a run and correlate them pairwise.

    Args:
        scores: The run's currency scores, from
            `fbe.scoring.score_currencies`. The whole universe, not a subset:
            every figure here is cross-sectional and a subset silently narrows
            what they mean.
        config_digest: `fbe.config.Config.digest` for the run, recorded so a
            figure quoted later can be tied to the weights that produced it.
        asof: The run's date. Defaults to the date the scores carry, which is
            the honest source, and raises if they disagree.

    Returns:
        A `PillarAudit`. Pillars absent from every currency's score map are
        reported as `AbsenceKind.NOT_RUN` for each currency; an absence carrying
        `fbe.scoring.PILLAR_FAILED` is `AbsenceKind.PILLAR_FAILED`; every other
        absence is `AbsenceKind.NO_DATA`.

    Raises:
        ValueError: If ``scores`` is empty, since an audit of nothing would
            report seven pillars as not run and twenty-one pairs as unmeasurable,
            which looks like a broken run rather than an empty argument. Or if
            the scores carry more than one ``asof`` and none was supplied, since
            this module cannot say which date the figures belong to and guessing
            would date the record wrongly.

    """
    if not scores:
        raise ValueError(
            "cannot audit an empty run: every pillar would report as not run "
            "and every pair as unmeasurable, which is indistinguishable from a "
            "run where nothing worked"
        )

    dates = {row.asof for row in scores}
    if asof is None:
        if len(dates) != 1:
            raise ValueError(
                "the scores carry "
                f"{len(dates)} different asof dates ("
                f"{', '.join(sorted(one.isoformat() for one in dates))}) and no "
                "asof was supplied, so this audit cannot say which run it "
                "describes"
            )
        asof = next(iter(dates))

    universe = tuple(row.currency for row in scores)
    scored: dict[PillarName, tuple[str, ...]] = {}
    absences: list[PillarAbsence] = []
    for pillar in PillarName:
        got: list[str] = []
        for row in scores:
            score = row.pillars.get(pillar)
            if score is None:
                absences.append(
                    PillarAbsence(
                        pillar=pillar, currency=row.currency, kind=AbsenceKind.NOT_RUN
                    )
                )
                continue
            if score.z is None:
                absences.append(
                    PillarAbsence(
                        pillar=pillar,
                        currency=row.currency,
                        kind=(
                            AbsenceKind.PILLAR_FAILED
                            if score.diagnostics.get(PILLAR_FAILED)
                            else AbsenceKind.NO_DATA
                        ),
                        reason=score.notes,
                    )
                )
                continue
            got.append(row.currency)
        # Ordered by the universe rather than by the order they were found, so
        # two runs over the same data list a pillar's currencies identically.
        found = set(got)
        scored[pillar] = tuple(currency for currency in universe if currency in found)

    return PillarAudit(
        asof=asof,
        config_digest=config_digest,
        scored=scored,
        absences=tuple(absences),
        pairs=pillar_pairs(scores),
    )
