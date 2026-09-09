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
        weight: The weight this pillar carries in the composite, echoed here so
            a report can reproduce the arithmetic without re-reading config.
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
    """Output of the risk module: how much to trade, in units the broker takes."""

    pair: str
    account_currency: str
    account_balance: float
    risk_fraction: float
    risk_amount: float
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
