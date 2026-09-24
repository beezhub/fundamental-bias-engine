"""Core data contracts for the fundamental bias engine.

Every module in this package speaks in terms of the types defined here.
Data sources produce `Observation`s. Pillars turn `Observation`s into
`PillarScore`s. The scorer aggregates `PillarScore`s into `CurrencyScore`s.
The bias layer differences `CurrencyScore`s into `PairBias` rows.

Nothing here performs I/O. Keep it that way: this module is imported by
everything, so it must stay dependency-free apart from the standard library.

Nothing here computes either, with one stated exception. A dataclass may carry
a property deriving a value from its own fields, where the alternative is a
renderer deriving it instead. `PositionSize.realised_risk_fraction` is the only
one and its docstring says why. The rule that matters is not that arithmetic is
forbidden here, it is that a number the engine reports must exist on the object
that reports it: a figure computed in a template exists nowhere a consumer can
read it, and nothing can check it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Direction",
    "Conviction",
    "PillarName",
    "Frequency",
    "Observation",
    "PillarScore",
    "CurrencyScore",
    "PairBias",
    "BiasReport",
    "CalendarEvent",
    "TradeIdea",
    "PositionSize",
    "DataSource",
    "Pillar",
]


class Direction(StrEnum):
    """Directional bias for a currency pair, expressed on the base currency."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Conviction(StrEnum):
    """How strongly the model backs a directional call.

    Conviction gates position size and whether a pair reaches the shortlist at
    all. It is derived from the size of the score spread, the agreement between
    pillars, and the freshness of the underlying data.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PillarName(StrEnum):
    """The seven fundamental pillars scored for every currency."""

    MONETARY = "monetary"
    INFLATION = "inflation"
    GROWTH = "growth"
    EMPLOYMENT = "employment"
    EXTERNAL = "external"
    POSITIONING = "positioning"
    RISK = "risk"


class Frequency(StrEnum):
    """Release frequency of an underlying macro series."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


@dataclass(frozen=True, slots=True)
class Observation:
    """A single macro or market data point, as published.

    Attributes:
        indicator: Canonical indicator key, e.g. ``"cpi_yoy"`` or ``"yield_2y"``.
            Indicator keys are shared across currencies so pillars can compare
            like with like; see ``docs/data-sources.md`` for the registry.
        currency: ISO 4217 code the observation describes, e.g. ``"USD"``.
            Use ``"GLOBAL"`` for cross-market series such as VIX.
        value: The published value in the unit given by ``unit``.
        period: The first day of the span the figure describes. August 2026
            monthly is ``2026-08-01`` and 2026Q2 is ``2026-04-01``. A
            point-in-time reading has no span and is stamped on its own day:
            a yield is the trading day it closed and a COT snapshot is the
            Tuesday the positions were counted. First-day because that is how
            FRED and the OECD stamp their series, because the first day of a
            span is defined at every frequency where the last day is not for
            ``Frequency.IRREGULAR``, and because it makes the age a module
            computes an upper bound on how old the information is, which
            errs toward less weight rather than more. Age is therefore days
            since the period began: ``BasePillar.staleness_days`` computes it
            that way and ``BasePillar._visible`` admits an unstamped
            observation on ``period`` plus an assumed lag. Release timing
            never lives here; it lives on ``released_at``.
        released_at: When the number hit the tape. Used to avoid look-ahead
            bias when backtesting; may be ``None`` for series where the source
            does not publish a release timestamp.
        source: Short source key, e.g. ``"fred"``, ``"cftc"``, ``"stooq"``.
        series_id: The source's own identifier, e.g. ``"DGS2"``.
        unit: Unit of ``value``, e.g. ``"percent"``, ``"index"``, ``"contracts"``.
        frequency: How often the series is published.
        revision: Vintage marker when a source republishes a period.

    """

    indicator: str
    currency: str
    value: float
    period: date
    source: str
    series_id: str
    unit: str
    frequency: Frequency = Frequency.MONTHLY
    released_at: datetime | None = None
    revision: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PillarScore:
    """One pillar's verdict on one currency.

    Attributes:
        raw: The pillar's headline number in its natural unit before
            normalisation, e.g. a 2y yield differential in percentage points.
            Kept so a report can show the reasoning, not just the score.
        z: ``raw`` normalised cross-sectionally against the rest of the
            universe for this run.
        score: ``z`` clipped and rescaled to the engine's ``-3..+3`` band. This
            is the only field the aggregator consumes.
        weight: The weight this pillar carries in the composite. A pillar emits
            its configured weight; the staleness penalty then returns a copy
            with this field scaled by the freshness factor, and every consumer
            downstream (composite, coverage, dispersion, agreement) reads the
            scaled value. So the field means configured weight before the
            penalty and effective weight after it, and anything reproducing the
            arithmetic must say which stage its inputs came from.
        staleness_days: Age of the newest input. Feeds the freshness penalty.
        inputs: Observations the pillar consumed, for audit and for the
            "show your working" section of the report.

    """

    pillar: PillarName
    currency: str
    raw: float | None
    z: float | None
    score: float
    weight: float
    asof: date
    staleness_days: int = 0
    inputs: Sequence[Observation] = field(default_factory=tuple)
    notes: str = ""
    """Human prose for the report's "show your working" section.

    Nothing may parse this for a decision. It is written for a person reading a
    report and its wording is free to change, so any fact a consumer needs must
    have a field or a `diagnostics` key of its own. The two facts that used to
    be required here, the blend divisor path and the assumed-lag input count,
    now have both: see `blend_divisor_path` and
    ``diagnostics["assumed_lag_inputs"]``.
    """
    diagnostics: Mapping[str, float] = field(default_factory=dict)
    """Per-run measurements about the cross-section this score came from, as
    opposed to the score itself. Read by the report and the reasoning layer,
    never by the aggregator: nothing here may change a composite.

    Two are expected. ``contamination`` is ``sqrt((n - max_z**2) / (n - 1))``,
    which falls as one currency dominates the cross-section, and is worth
    showing because at eight points a single outlier moves the mean and the
    standard deviation together and can flip the sign of a currency that did
    not move. ``emit_sd`` is the standard deviation the pillar actually emitted
    across the universe, which is how a reader can tell whether a pillar is
    speaking at the volume its weight implies. A pillar emitting well below one
    contributes less than its declared weight, and that gap is invisible in the
    composite.

    ``assumed_lag_inputs`` is the third, and it counts how many of this
    currency's inputs were admitted on ``DEFAULT_PUBLICATION_LAG_DAYS`` rather
    than on a real ``released_at``. It is how much of the score rests on an
    assumption about when a figure was published rather than on the fact, and a
    reader comparing a backtest with a live run needs it.
    """
    blend_divisor_path: str = ""
    """Which scale this pillar's blend was divided by: how, not how much.

    ``"rolling"`` means the divisor came from the median of recent runs and
    ``"run_local"`` means there were too few of those and this run's own blend
    standard deviation was used instead. An empty string means the pillar
    recorded no path, which is the honest answer for a single-component pillar
    and for one whose ``_normalise`` never blends.

    A typed field rather than a marker inside `notes`, because ``--compare``
    reads it to decide whether two runs are on the same scale, which makes it a
    fact read for correctness and not for display. Scores computed under the
    fallback are not comparable with scores computed under the rolling estimate,
    and a report that cannot say which it is holding is hiding the one thing
    needed to compare two days. Recorded by `fbe.pillars.base.BasePillar.compute`
    on every score it builds, absent ones included.
    """
    freshness_factor: float | None = None
    """Fraction of its configured weight this pillar's inputs still justify.

    A pure multiplier in ``[0.0, 1.0]``, carrying no unit and no sign: 1.0 means
    every input is inside its own release schedule, 0.0 means the pillar is past
    it and drops out of the composite entirely. It never touches ``score``, only
    ``weight``, because a stale pillar has not changed its mind, it has stopped
    being able to see.

    Written by `fbe.pillars.base.BasePillar.compute` from `pillar_freshness`,
    which ages each component against that indicator's own allowance in the
    registry and averages over the sub-weights the currency actually holds. Read
    by `fbe.scoring.score_currencies`, which passes it to
    `fbe.scoring.apply_staleness_penalty` as ``freshness_factor``. Nothing else
    reads it.

    ``None`` means the pillar did not compute one, which is the honest answer
    for an implementation of `Pillar` that is not a `BasePillar`. The scorer
    then falls back to `fbe.scoring.freshness` on ``staleness_days`` alone,
    which knows no allowance and judges every series as if it published monthly.
    That fallback is conservative rather than correct, so ``None`` is a marked
    absence and not a neutral default: 1.0 would claim the inputs were checked
    and found fresh.

    A typed field rather than a `diagnostics` key, for the reason
    `blend_divisor_path` gives above and the stronger form of it. ``diagnostics``
    is documented as never read by the aggregator, and this number exists to
    change the weight the aggregator applies, so it is read for correctness and
    not for display.
    """


@dataclass(frozen=True, slots=True)
class CurrencyScore:
    """Aggregate fundamental standing of a single currency for one run.

    ``composite`` is the weighted sum of the pillar scores, on the same
    ``-3..+3`` band. Positive means fundamentally strong, negative weak.
    """

    currency: str
    composite: float
    pillars: Mapping[PillarName, PillarScore]
    asof: date
    rank: int | None = None
    dispersion: float = 0.0
    """The effective-weighted standard deviation of this currency's pillar
    scores about its composite, and not the unweighted standard deviation
    across those scores.

    The weights are the post-staleness effective weights, renormalised to sum
    to 1.0 so the measure stays on the score band whatever the coverage was.
    ``docs/scoring-spec.md`` section 4.4 defines it and `fbe.scoring.dispersion`
    computes it. Read by `fbe.bias.conviction_for` against
    ``ScoringConfig.max_dispersion``, above which conviction is demoted one
    step.

    High dispersion means the pillars disagree, which should reduce conviction
    on any pair using this currency. The two readings are far enough apart to
    change that decision. On a currency with MONETARY at +2.0, INFLATION at
    -1.0 and the other five at 0.0, all fresh, the weighted figure is 1.071214
    and the unweighted one is 0.832993, against a threshold of 1.20 that was
    judged against the first.

    ``0.0`` when fewer than two pillars are usable. That is a floor rather than
    a finding, and the coverage alongside it is the real signal in that case.
    """
    coverage: float = 1.0
    """The sum of the effective pillar weights, ``sum over p of w_eff(p)``.

    ``docs/scoring-spec.md`` section 4.2 defines it and `fbe.scoring.coverage`
    computes it. Effective weight carries the staleness discount, so this is
    the share of pillar weight that had usable and fresh data rather than the
    share of pillars present: a pillar scoring on a 30-day-old input
    contributes half its weight, not all of it. It is therefore continuous
    rather than a count.

    Below 1.0 the composite is extrapolated from a partial picture. Read by
    `fbe.bias.apply_filters` against ``ScoringConfig.min_coverage``, which
    blocks the pair outright, and by `fbe.bias.conviction_for` against
    ``ScoringConfig.coverage_demotion``, which demotes it one step. Exactly
    ``0.0`` means the currency has no composite and every pair using it is
    blocked with ``no_coverage``.
    """


@dataclass(frozen=True, slots=True)
class PairBias:
    """Directional bias for one tradeable pair.

    The engine never scores a pair directly. It scores the two legs and takes
    the difference, which is the whole point of a relative-value framework:
    there is no such thing as a strong currency, only a currency stronger than
    the one it is quoted against.
    """

    pair: str
    base: str
    quote: str
    spread: float
    """``composite(base) - composite(quote)``."""
    direction: Direction
    conviction: Conviction
    asof: date
    base_score: float = 0.0
    quote_score: float = 0.0
    agreement: float = 0.0
    """The share of pillar weight pointing the same way as ``direction``.

    ``sum of w_pair over agreeing / sum of w_pair over considered``, where
    ``w_pair`` is the mean of the two legs' post-staleness effective weights.
    ``docs/scoring-spec.md`` section 5.3 defines it and `fbe.bias.agreement`
    computes it. Read by `fbe.bias.conviction_for` against
    ``ScoringConfig.min_agreement``, below which conviction is capped at
    `Conviction.LOW`.

    This is not a count of pillars, and the two readings differ by enough to
    mislead. On the worked USDJPY case in section 7.7, four of the seven
    pillars agree, a headcount of 57%, while the weight share is 0.70. Both
    renderers print "of pillar weight agrees" for that reason, and a reader who
    meets the field here first should not have to find the renderer to learn
    which of the two they are holding.

    Two exclusions, and they are different rules. A pillar scoring the two legs
    identically has no opinion on this pair, so it is excluded from both the
    numerator and the denominator: leaving it in the denominator would make a
    thin run look like a disputed one. A pillar with no usable data on either
    leg is excluded too, because the neutral ``0.0`` that
    `fbe.scoring.missing_score` stamps it with would otherwise read as an
    opinion held by whichever leg does have data.

    ``0.0`` when no pillar is considered, which pairs with a coverage figure
    low enough to block the trade anyway.
    """
    tradeable: bool = True
    """False when the pair fails a hard filter such as spread cost or an
    imminent high-impact event on either leg."""
    blockers: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A scheduled economic release that may warrant standing aside."""

    title: str
    currency: str
    scheduled_for: datetime
    impact: str
    """The source's own rating, verbatim and case included.

    One of ``"High"``, ``"Medium"``, ``"Low"`` or ``"Holiday"``, capitalised
    exactly like that. ``Holiday`` marks a market closure rather than a
    release, so it is not a fourth severity and does not sort against the
    other three.

    `fbe.datasources.calendar.IMPACT_LEVELS` is the authority on this
    vocabulary and is where a new value lands if the feed adds one. Compare
    case-insensitively: this field carries whatever the source sent, so a
    consumer written against a lower-case spelling matches nothing and reports
    every week as clear.
    """
    source: str = "forexfactory"
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Output of the risk module: how much to trade, in units the broker takes.

    Every money field on this dataclass is in ``account_currency``. There are no
    exceptions, including ``notional``, which therefore needs the same
    conversion leg as the risk figures rather than being left as
    ``units * entry`` in the quote currency. A mixed-unit money field on a
    ticket is the kind of error that reads as plausible: on a ZAR account it
    would understate a dollar-quoted position by the USDZAR rate, roughly
    seventeen-fold, in the one number shown for leverage awareness.

    Attributes:
        risk_fraction: The intended fraction of balance at risk, inside the
            plan's 1-2% band.
        risk_amount: The intended money at risk, ``account_balance *
            risk_fraction``, before the size is rounded to a whole lot step.
        realised_risk_amount: What is actually at risk once ``lots`` has been
            rounded down. This is always at or below ``risk_amount`` and it is
            the figure the trader is really exposed to, so it is the one the
            pre-trade check reads and the one the journal must record. An
            R-multiple computed against the intended figure is overstated by
            the rounding ratio, which on a small account is routinely 10% or
            more, and that number is the sole input to the conviction
            calibration the whole model is meant to be judged on.
        realised_risk_fraction: ``realised_risk_amount / account_balance``,
            the share of the account actually exposed once ``lots`` has been
            rounded down. Derived rather than stored, so it cannot disagree
            with the two numbers it divides.

            ``docs/trading-plan.md`` states the per-trade rule in both
            forms at once, "1% - 2% of the account balance (R20 - R40)", and
            the engine held only one of each pair: the money realised and the
            fraction intended. This is the missing cell. ``risk_fraction``
            beside it is what was asked for rather than what was obtained.

            Read by the report and the dashboard, which printed it by dividing
            until the engine held it. `fbe.risk.check_limits` is specified to
            read ``realised_risk_amount`` instead, because a cap stated in
            money stays in money, and the pre-trade checklist in
            ``docs/risk-and-execution.md`` section 8 is ticked in money for
            the same reason.

            The denominator is guaranteed on every path that computes a size:
            `fbe.risk.position_size` is the only thing in the package that
            builds a `PositionSize`, and it refuses a balance that is not
            finite and strictly positive in the same guard as a malformed
            price. A `PositionSize` rebuilt from a report sidecar by
            `fbe.report.load_report` is trusted rather than rechecked, so a
            hand-edited sidecar can still carry a zero here.

            There is deliberately no branch and no fallback. A zero or absent
            balance is a refused input rather than a position risking an
            unknown share, and
            ``docs/decisions/0002-representing-not-known.md`` rules out
            putting a plausible number in its place.
        notional: Face value of the position in ``account_currency``.
        warnings: Soft failures. The module never silently adjusts anything; it
            sizes what was asked for and says what is wrong with it.

    """

    pair: str
    account_currency: str
    account_balance: float
    risk_fraction: float
    risk_amount: float
    realised_risk_amount: float
    entry: float
    stop: float
    stop_distance_pips: float
    units: float
    lots: float
    notional: float
    warnings: Sequence[str] = field(default_factory=tuple)

    @property
    def realised_risk_fraction(self) -> float:
        """Return the share of the account this position actually risks.

        A property rather than a field, on three counts. It costs no slot on a
        ``slots=True`` dataclass and takes no constructor argument, so adding
        it changed nothing that builds a `PositionSize`. It cannot drift from
        the two numbers it divides, where a stored value would have to be
        computed by every builder and a builder passing an inconsistent one
        produces a ticket whose own figures disagree. And it stays out of the
        report sidecar, which walks ``dataclasses.fields``, so the committed
        record keeps the two measured numbers and recomputes the ratio rather
        than carrying a third that could be read back out of step with them.

        **The journal is the opposite case and the names collide.**
        `fbe.journal.TradeRecord` does store a fraction, deliberately, so that
        a breach of the band is visible without arithmetic, and it calls that
        field ``risk_fraction`` while meaning the realised share. Here
        ``risk_fraction`` means the intended one. So the value a
        ``TradeRecord`` wants is this property rather than the field of the
        same name.

        What writing ``size.risk_fraction`` into ``record.risk_fraction``
        costs, precisely, because a warning naming the wrong consequence gets
        read past: it records 2.00% where 1.79% was on the book, so the one
        field whose stated job is to make a breach of the band visible without
        arithmetic reports a breach that did not happen, or hides one that
        did. It does not touch the R-multiple. ``TradeRecord.r_multiple`` is
        ``outcome_zar / risk_amount`` and that ``risk_amount`` is the realised
        money, so the measure `fbe.journal.evaluate` sums is protected by a
        different field and a different rule.

        Returns:
            A fraction in ``[0.0, risk_fraction]``, carrying no unit and no
            sign: 0.0 means the size was refused and nothing is exposed, which
            is a measurement rather than an absence. It does not exceed
            ``risk_fraction``, because rounding the lot step down can only
            reduce what is at risk, and where the step divides the size
            exactly the two meet to within floating-point error rather than
            being equal bit for bit.

        """
        return self.realised_risk_amount / self.account_balance


@dataclass(frozen=True, slots=True)
class TradeIdea:
    """A pair bias joined to the technical and risk layers.

    This is the handoff point between this engine and the discretionary
    trading plan: the engine says which way to lean and how hard, the trader
    supplies the trendline or channel entry.
    """

    bias: PairBias
    size: PositionSize | None = None
    blackout_until: datetime | None = None
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class BiasReport:
    """Everything one run of the engine produced."""

    asof: date
    generated_at: datetime
    currencies: Sequence[CurrencyScore]
    pairs: Sequence[PairBias]
    events: Sequence[CalendarEvent] = field(default_factory=tuple)
    shortlist: Sequence[TradeIdea] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)
    config_digest: str = ""
    """Hash of the settings that change what a run computes, so a report can be
    tied to the weights and limits that produced it, and so ``--compare`` can
    treat a change as a re-weighting and report two runs as not comparable.

    It covers the whole of ``ScoringConfig`` and all of ``RiskConfig`` except
    ``account_balance``. It leaves out ``account_balance``, ``DataConfig`` and
    ``BrokerConfig``, so two reports carrying the same digest may still have
    been sized on different balances, read under different data settings and
    produced against different broker profiles. `fbe.config.Config.digest` is
    the definition of record and says why each is left out.
    """


@runtime_checkable
class DataSource(Protocol):
    """Anything that can supply `Observation`s.

    Implementations live in ``fbe.datasources``. They are responsible for their
    own caching and for translating source-specific identifiers into the
    canonical indicator keys in the registry.
    """

    name: str

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Return every observation the source holds for the given request."""
        ...

    def available(self) -> bool:
        """Report whether the source is usable, e.g. its API key is configured."""
        ...


@runtime_checkable
class Pillar(Protocol):
    """Anything that turns `Observation`s into one score per currency.

    A pillar receives the full observation set for the whole universe, not just
    one currency, because normalisation is cross-sectional by design.
    """

    name: PillarName
    requires: Sequence[str]
    """Canonical indicator keys the pillar needs."""

    def compute(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, PillarScore]:
        """Return one `PillarScore` per currency in ``currencies``."""
        ...
