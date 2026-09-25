"""The collector: one request, fanned out across every source.

Six sources each know how to answer for their own series. Something has to take
one request for the whole universe, send each ``(indicator, currency)`` to the
source the registry names for it, and reconcile what comes back into one set of
`Observation`s. That is this module, and `fbe.cli.refresh` is its only caller
today.

Why it lives here
-----------------
Everything it routes on is this package's vocabulary: `SeriesRef`, ``refs()``
and ``ALL_SOURCES``. It performs no transformation, so it is not a pipeline
stage beside `fbe.scoring` and `fbe.bias`: it takes requests and returns
``Observation``s, which is exactly what `fbe.types.DataSource.fetch` already
returns. It is an aggregate of sources rather than a stage between two
contracts, and it changes when the sources change. #61 records the ruling.

It is deliberately not imported by ``fbe.datasources.__init__``. This module
reads ``ALL_SOURCES`` from the package, and importing it back would make the
package import itself part-built.

The two rules that are easy to get wrong
----------------------------------------
**Manual is an override, not a routed source.** Every other source is asked
only for the pairs its own ``refs()`` claims. `ManualSource` is asked for the
whole request, because an operator types a correction for a series some vendor
already serves, and a manual entry that only arrived for pairs the registry
routes to ``manual`` could never override anything. It runs last, so its value
wins for a ``(indicator, currency, period)`` another source also answered.

**A source that fails is a gap, not the end of the run.** One dead provider at
07:00 must leave the other five to fill the cache, because a partial refresh
with a named gap beats no data at all. Every failure is recorded against its
source and reported; nothing is swallowed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES
from fbe.datasources.base import BaseDataSource, FailureScope
from fbe.datasources.registry import GLOBAL, INDICATORS, SOURCE_MANUAL, stale_refs
from fbe.types import Observation
from fbe.universe import G10

__all__ = [
    "CollectionResult",
    "SeriesFailure",
    "SourceOutcome",
    "SourceStatus",
    "collect",
    "lookback_start",
]

StaleRefs = Callable[..., Mapping[str, tuple[str, ...]]]
"""Signature of `fbe.datasources.registry.stale_refs`, injectable for testing."""


class SourceStatus(StrEnum):
    """What became of one source on one collection."""

    COMPLETED = "completed"
    """It was asked and it answered, with any number of observations."""

    SKIPPED = "skipped"
    """It was never asked: not selected, not configured, or not yet built."""

    PARTIAL = "partial"
    """It answered, and named series are gone.

    Only a source declaring `fbe.datasources.base.FailureScope.SERIES` can
    reach this. A member rather than prose on ``COMPLETED``, per ADR 0015: a
    completed outcome carrying its losses in ``detail`` is a fact nothing can
    group and nothing reads, and the refresh line would print as success. What
    was lost is in ``SourceOutcome.failures``.
    """

    FAILED = "failed"
    """It was asked and it raised. A named gap, not a reason to stop.

    Also where a series-scoped source ends up when every series it was asked
    for failed. Thirty named gaps and a source that did not fail is honest and
    useless: it buries the one fact the operator needs, which is that the
    provider is unavailable. There is no threshold between this and
    ``PARTIAL``: all served is completed, some served is partial, none served
    is failed.
    """


@dataclass(frozen=True, slots=True)
class SeriesFailure:
    """One series a source was asked for and could not serve.

    Attributes:
        indicator: Canonical indicator key, as `SeriesRef` names it.
        currency: ISO 4217 code, or ``GLOBAL``.
        detail: The exception type and message, so the refresh line says what
            went wrong rather than that something did. Prose, and the only
            field here that is: the pair above it is what anything grouping or
            counting these reads.

    """

    indicator: str
    currency: str
    detail: str


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """One source's contribution to one collection.

    Attributes:
        source: The source key, matching ``SeriesRef.source``.
        status: Which of the three things happened.
        series: Distinct ``(indicator, currency)`` pairs this source returned
            observations for. Counted from what came back rather than from what
            was asked, so it reconciles with ``observations`` on the same line.
            Zero for a skipped or failed source.
        observations: How many observations it returned, before the manual
            override is applied, so a source's own line says what it served
            rather than what survived.
        elapsed_seconds: Wall-clock time inside ``fetch``. Zero for a source
            that was never asked. For a series-scoped source, the time across
            every call it took to ask for each series.
        detail: Why it was skipped, or what it raised. Empty when it completed.
        failures: The series this source was asked for and could not serve,
            ordered by indicator then currency so two runs print the same.
            Empty for every status but ``PARTIAL`` and a series-scoped
            ``FAILED``: a whole source that raised has no per-series detail to
            give, and its one reason is in ``detail``.

    """

    source: str
    status: SourceStatus
    series: int
    observations: int
    elapsed_seconds: float
    detail: str = ""
    failures: tuple[SeriesFailure, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a completed outcome that is carrying losses.

        The state is contradictory rather than merely unlikely: ``COMPLETED``
        is what every consumer reads as "this source is fine", and the refresh
        line prints it as a clean run. Making it unconstructible costs one
        comparison and removes the ADR 0002 shape at the source, rather than
        leaving a test to notice it later.

        Raises:
            ValueError: When ``COMPLETED`` carries failures.

        """
        if self.status is SourceStatus.COMPLETED and self.failures:
            raise ValueError(
                f"{self.source} is completed and carries "
                f"{len(self.failures)} failed series, which cannot both be "
                "true: some served is PARTIAL and none served is FAILED"
            )


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Everything one collection produced.

    Attributes:
        observations: The reconciled set, sorted by indicator, currency and
            period so two runs over the same data print in the same order.
        outcomes: One per source in ``ALL_SOURCES`` order, including the ones
            that were skipped, because a source missing from the output and a
            source that returned nothing are different facts.
        gaps: Coverage gaps from `fbe.datasources.registry.stale_refs`, keyed by
            indicator with the currencies that have no usable ref.

    """

    observations: tuple[Observation, ...]
    outcomes: tuple[SourceOutcome, ...]
    gaps: Mapping[str, tuple[str, ...]]

    @property
    def failed(self) -> tuple[SourceOutcome, ...]:
        """The sources that raised, in the order they were tried."""
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is SourceStatus.FAILED
        )

    @property
    def usable(self) -> bool:
        """Whether this run produced anything worth reading.

        False covers both conditions ``docs/interfaces.md`` gives for exit 1,
        every source failing and coverage collapsing, because both arrive here
        as an empty set: a refresh that reconciled nothing has put nothing in
        the cache and left the scorer with nothing, whether that was one outage
        or eight. A source that completed and honestly held nothing counts the
        same way, since the cache is no fuller for it.
        """
        return bool(self.observations)


def lookback_start(today: date, years: int) -> date:
    """Return the same calendar day ``years`` earlier.

    The scoring lookback is configured in whole years, and a refresh has to
    turn that into the earliest period it asks each source for.

    Args:
        today: The run date.
        years: Whole years to step back, from `ScoringConfig.lookback_years`.

    Returns:
        The same day and month, ``years`` earlier. 29 February steps back to
        28 February when the earlier year is not a leap year, which shortens
        the window by one day rather than lengthening it by one: a window that
        is a day short asks for a period nobody wanted, and one that is a day
        long is the first step towards a lookback nobody chose.

    Raises:
        ValueError: If ``years`` is negative, which would ask each source for a
            window starting in the future and return nothing from every one of
            them, with no failure to show for it.

    """
    if years < 0:
        raise ValueError(f"a lookback cannot be negative, got {years} years")
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        # 29 February in a year whose counterpart is not a leap year.
        return today.replace(year=today.year - years, month=2, day=28)


def _skipped(name: str, detail: str) -> SourceOutcome:
    """Build the outcome for a source that was never asked."""
    return SourceOutcome(
        source=name,
        status=SourceStatus.SKIPPED,
        series=0,
        observations=0,
        elapsed_seconds=0.0,
        detail=detail,
    )


def _failed(name: str, detail: str, elapsed: float) -> SourceOutcome:
    """Build the outcome for a source that was asked and raised."""
    return SourceOutcome(
        source=name,
        status=SourceStatus.FAILED,
        series=0,
        observations=0,
        elapsed_seconds=elapsed,
        detail=detail,
    )


def _source_name(source_class: type[BaseDataSource]) -> str:
    """Return a source class's key, before it has been constructed.

    Read with ``getattr`` rather than from the class's own ``__dict__``, so a
    subclass that inherits its parent's key answers to that key. The instance
    would: ``self.name`` is the same inherited attribute, and the two
    disagreeing would label a source's output line with one name while routing
    it under another. `fbe.cli` reads it the same way for the same reason.

    Args:
        source_class: The class, not an instance.

    Returns:
        The declared key, or the class name for a class that never set one,
        which is a programming error worth naming the class in.

    """
    return getattr(source_class, "name", source_class.__name__)


def _routed_pairs(
    source: BaseDataSource, requested: frozenset[tuple[str, str]]
) -> frozenset[tuple[str, str]]:
    """Return the requested pairs this source should be asked for.

    Args:
        source: The constructed source.
        requested: Every ``(indicator, currency)`` the run wants.

    Returns:
        For an ordinary source, the pairs its own ``refs()`` claims, so no
        source is asked about an indicator the registry routes elsewhere. For
        `ManualSource`, the whole request: a manual entry exists to override a
        value some other source already serves, and a manual source asked only
        for the pairs the registry routes to ``manual`` could never override
        anything. ``ALL_SOURCES`` puts it last so that its answer wins.

    Raises:
        NotImplementedError: From a source whose ``refs()`` is still a stub.
            The caller turns that into a skip rather than a failure, because a
            source that has not been built yet has not gone wrong.
        Exception: Anything else ``refs()`` raises. The caller turns that into
            a failure against this source alone.

    """
    if source.name == SOURCE_MANUAL:
        return requested
    return frozenset(pair for pair in source.refs() if pair in requested)


def _collect_one(
    source: BaseDataSource,
    requested: frozenset[tuple[str, str]],
    start: date,
    end: date,
    force: bool,
) -> tuple[SourceOutcome, Sequence[Observation]]:
    """Ask one source for its share of the request.

    Nothing here raises. Every way a source can fail to answer becomes an
    outcome, because one dead provider at 07:00 must leave the others to fill
    the cache.

    Args:
        source: The constructed source.
        requested: Every ``(indicator, currency)`` the run wants.
        start: Earliest period, inclusive.
        end: Latest period, inclusive.
        force: Drop this source's cached responses first, so a request inside
            the TTL goes back to the provider.

    Returns:
        The outcome and the observations, which are empty for anything but a
        completed fetch.

    """
    name = source.name
    try:
        usable = source.available()
    except NotImplementedError:
        return _skipped(name, "scaffolded, not yet built"), ()
    except Exception as error:  # noqa: BLE001
        # Availability is a question about configuration, so an exception here
        # is a misconfiguration rather than an outage. Either way it is this
        # source's problem alone and the run continues without it.
        return (
            _skipped(
                name,
                f"could not report availability: {type(error).__name__}: {error}",
            ),
            (),
        )
    if not usable:
        return _skipped(name, "unavailable, not configured"), ()

    try:
        pairs = _routed_pairs(source, requested)
    except NotImplementedError:
        return _skipped(name, "scaffolded, not yet built"), ()
    except Exception as error:  # noqa: BLE001
        # A registry lookup inside refs() can raise for its own reasons. That
        # is still one source's problem, and letting it out of here would cost
        # the whole refresh for a defect in one adapter's routing table.
        return (
            _failed(
                name, f"could not list its series: {type(error).__name__}: {error}", 0.0
            ),
            (),
        )
    if not pairs:
        return _skipped(name, "no series routed to it"), ()

    if force:
        # The shared request path has no way to bypass a live cache entry, so
        # forcing means removing the entries first. Only this source's own
        # directory goes, and only when it is in this run, so -s fred --force
        # cannot throw away the OECD's copies. An entry this run does not go on
        # to request is removed rather than refreshed, which is the cost of
        # doing it here; a later offline read of that entry then fails loudly
        # rather than serving the copy the operator asked to be rid of.
        source.cache.clear(name)

    if source.failure_scope is FailureScope.SERIES:
        return _collect_per_series(source, pairs, start, end)

    started = time.monotonic()
    try:
        observations = tuple(
            source.fetch(
                sorted({indicator for indicator, _ in pairs}),
                sorted({currency for _, currency in pairs}),
                start,
                end,
            )
        )
    except Exception as error:  # noqa: BLE001
        # Not only SourceError. A parser raising ValueError on one bad row is
        # still one source's problem, and letting it out of here would cost the
        # whole morning's refresh for a defect in a single provider's adapter.
        return (
            _failed(
                name,
                f"{type(error).__name__}: {error}",
                time.monotonic() - started,
            ),
            (),
        )

    return (
        SourceOutcome(
            source=name,
            status=SourceStatus.COMPLETED,
            series=len({(o.indicator, o.currency) for o in observations}),
            observations=len(observations),
            elapsed_seconds=time.monotonic() - started,
        ),
        observations,
    )


def _collect_per_series(
    source: BaseDataSource,
    pairs: frozenset[tuple[str, str]],
    start: date,
    end: date,
) -> tuple[SourceOutcome, Sequence[Observation]]:
    """Ask a series-scoped source for one series at a time.

    The narrowing lives here rather than in the source, per ADR 0015. ``fetch``
    keeps its contract of returning or raising, so no adapter has to report its
    own partial failures and none can forget to, and the same catch that covers
    a whole fetch covers each call.

    Args:
        source: The constructed source, declaring
            `fbe.datasources.base.FailureScope.SERIES`.
        pairs: The ``(indicator, currency)`` pairs routed to it, already
            narrowed to what this run asked for.
        start: Earliest period, inclusive.
        end: Latest period, inclusive.

    Returns:
        One outcome and every observation the series that answered returned.
        ``COMPLETED`` when none failed, ``PARTIAL`` when some did, ``FAILED``
        when all did, with each failure named either way. No threshold sits
        between the three.

        A series that answers with nothing and does not raise is served, not
        failed. Whether an empty window is data or a dead series is the
        source's call and not the collector's: COT documents empty as a
        reading, and `fbe.datasources.oecd.OecdSource` raises on it.

    """
    served: list[Observation] = []
    failures: list[SeriesFailure] = []
    started = time.monotonic()
    for indicator, currency in sorted(pairs):
        try:
            served.extend(source.fetch([indicator], [currency], start, end))
        except Exception as error:  # noqa: BLE001
            # Not only SourceError, for the same reason the whole-source catch
            # below takes everything: a parser raising ValueError on one bad
            # row is that series' problem, and letting it out would cost the
            # other thirty-eight.
            failures.append(
                SeriesFailure(
                    indicator=indicator,
                    currency=currency,
                    detail=f"{type(error).__name__}: {error}",
                )
            )

    elapsed = time.monotonic() - started
    observations = tuple(served)
    if not failures:
        status = SourceStatus.COMPLETED
    elif len(failures) == len(pairs):
        status = SourceStatus.FAILED
    else:
        status = SourceStatus.PARTIAL
    return (
        SourceOutcome(
            source=source.name,
            status=status,
            # Counted from what came back, as a completed source's count is,
            # so the line reconciles with the observations beside it rather
            # than reporting what was asked for.
            series=len({(o.indicator, o.currency) for o in observations}),
            observations=len(observations),
            elapsed_seconds=elapsed,
            detail=_series_detail(failures, len(pairs)),
            failures=tuple(failures),
        ),
        observations,
    )


def _series_detail(failures: Sequence[SeriesFailure], asked: int) -> str:
    """Return the one-line summary beside a series-scoped outcome.

    Args:
        failures: What could not be served.
        asked: How many series the source was asked for.

    Returns:
        Empty when nothing failed, so a completed source's line is unchanged.
        Otherwise the count against the number asked for, which is the figure
        that says whether this was one bad series or an outage. The series
        themselves are in ``SourceOutcome.failures`` rather than here: a reader
        that has to parse this sentence to find out what was lost is the shape
        ADR 0015 rejected.

    """
    if not failures:
        return ""
    return f"{len(failures)} of {asked} series failed"


def collect(
    config: DataConfig,
    *,
    start: date,
    end: date,
    sources: Sequence[type[BaseDataSource]] | None = None,
    selected: Iterable[str] | None = None,
    indicators: Iterable[str] | None = None,
    currencies: Iterable[str] | None = None,
    force: bool = False,
    stale: StaleRefs | None = None,
) -> CollectionResult:
    """Fan one request out across every source and reconcile the answers.

    Each ``(indicator, currency)`` goes to the source its `SeriesRef` names and
    to no other, read from each source's own ``refs()``. `ManualSource` is the
    exception and is asked for the whole request, so an operator's entry can
    override a fetched value; see `_routed_pairs`.

    Args:
        config: The run's `DataConfig`. Carries the cache directory, the TTL
            and the offline flag, all of which the sources apply themselves.
        start: Earliest period wanted, inclusive.
        end: Latest period wanted, inclusive. Also the date the coverage gaps
            are aged against.
        sources: Source classes to fan out over, in the order they are tried,
            with the override source last. Defaults to ``ALL_SOURCES``.
        selected: Restrict the run to these source keys. ``None`` runs every
            source. An empty sequence is refused rather than treated as either
            of those: no caller wants to fan out over no sources, and the two
            plausible readings of it, everything and nothing, are too far apart
            to guess between.
        indicators: Canonical indicator keys. Defaults to every key in the
            registry.
        currencies: ISO 4217 codes. Defaults to the G10 plus ``GLOBAL``.
        force: Drop each selected source's cached responses before fetching, so
            a request inside the TTL goes back to the provider.
        stale: The coverage gap function, defaulting to
            `fbe.datasources.registry.stale_refs`. Injectable so a test can
            assert what it was aged against without standing up a registry.

    Returns:
        A `CollectionResult`. No source failure reaches the caller as an
        exception: a source that could not answer is a `SourceOutcome` with
        `SourceStatus.FAILED` and the reason on it, because a partial refresh
        with a named gap is worth more than none at all.

    Raises:
        ValueError: If ``selected`` names a source this run does not have, if
            it is empty, or if ``start`` is later than ``end``. Each would
            otherwise produce an empty, apparently successful run: the first by
            silently refreshing a subset of what was asked for, the second by
            fetching from nothing, the third by asking every source for a
            window that cannot contain anything.

    """
    if start > end:
        raise ValueError(
            f"the window starts at {start}, after it ends at {end}, so no "
            "source could return anything"
        )

    source_classes = tuple(ALL_SOURCES if sources is None else sources)
    known = {_source_name(source_class) for source_class in source_classes}
    if selected is None:
        chosen = known
    else:
        chosen = set(selected)
        unknown = sorted(chosen - known)
        if unknown:
            raise ValueError(
                f"no source named {', '.join(unknown)}; this run knows "
                f"{', '.join(sorted(known))}"
            )
        if not chosen:
            raise ValueError(
                "a selection of no sources would fetch nothing and report a "
                "run that did what it was asked; omit the selection to run "
                "every source"
            )

    wanted_indicators = tuple(INDICATORS) if indicators is None else tuple(indicators)
    wanted_currencies = (*G10, GLOBAL) if currencies is None else tuple(currencies)
    requested = frozenset(
        (indicator, currency)
        for indicator in wanted_indicators
        for currency in wanted_currencies
    )

    outcomes: list[SourceOutcome] = []
    # Keyed by the triple the override rule is written in terms of, so a later
    # source replaces an earlier one's value for the same period. Two vintages
    # of one period from one source collapse to the last of them: the contract
    # here is one value per (indicator, currency, period), and Phase 6's
    # point-in-time reads go to FredSource.fetch_series rather than through
    # this function.
    merged: dict[tuple[str, str, date], Observation] = {}

    for source_class in source_classes:
        name = _source_name(source_class)
        if name not in chosen:
            outcomes.append(_skipped(name, "not selected"))
            continue
        try:
            source = source_class(config)
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                _failed(
                    name,
                    f"could not be constructed: {type(error).__name__}: {error}",
                    0.0,
                )
            )
            continue
        try:
            outcome, observations = _collect_one(source, requested, start, end, force)
        finally:
            # One client per source per run, and a run that leaves them open
            # leaks a socket for every source every morning.
            source.close()
        outcomes.append(outcome)
        for observation in observations:
            key = (observation.indicator, observation.currency, observation.period)
            merged[key] = observation

    resolve = stale_refs if stale is None else stale
    return CollectionResult(
        observations=tuple(
            observation
            for _, observation in sorted(merged.items(), key=lambda item: item[0])
        ),
        outcomes=tuple(outcomes),
        gaps=resolve(asof=end),
    )
