"""Core data contracts for the fundamental bias engine.

Every module in this package speaks in terms of the types defined here.
Data sources produce `Observation`s. Pillars turn `Observation`s into
`PillarScore`s. The scorer aggregates `PillarScore`s into `CurrencyScore`s.
The bias layer differences `CurrencyScore`s into `PairBias` rows.

Nothing here performs I/O or computation. Keep it that way: this module is
imported by everything, so it must stay dependency-free apart from the
standard library.
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
        period: The period the data describes, not the day it was released.
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
    """Standard deviation across pillar scores. High dispersion means the
    pillars disagree, which should reduce conviction on any pair using this
    currency."""
    coverage: float = 1.0
    """Fraction of pillar weight that had usable data. Below 1.0 the composite
    is extrapolated from a partial picture."""


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
    """Fraction of pillars pointing the same way as ``direction``."""
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
    """``"high"``, ``"medium"`` or ``"low"`` as published by the source."""
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
    """Hash of the effective config, so a report can be tied to the weights
    that produced it."""


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
