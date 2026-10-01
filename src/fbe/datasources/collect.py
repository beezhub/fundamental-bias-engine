"""The collector: one request, fanned out across every source.

Every source in ``ALL_SOURCES`` knows how to answer for its own series.
Something has to take one request for the whole universe, send each
``(indicator, currency)`` to the source the registry names for it, and
reconcile what comes back into one set of `Observation`s. That is this module,
and the commands in `fbe.cli` are its only callers.

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
07:00 must leave the others to fill the cache, because a partial refresh with a
named gap beats no data at all. Every failure is recorded against its source
and reported; nothing is swallowed.

**A series inside a series-scoped source is a gap in the same way.** A source
that declares ``failure_scope = "series"`` is asked for one ``(indicator,
currency)`` at a time, each call caught on its own, so one dead series costs
its own currency and the rest are served. The outcome is `SourceStatus.PARTIAL`
with every failed series named on it. The loop lives here and not in the
source, because a source that caught its own failures would need a channel to
report them and one that forgot would produce a quiet partial. ADR 0015
records the ruling; the OECD's 39 series were the case, one of them failing
three times in two days and taking the other 38 with it each time.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES
from fbe.datasources.base import BaseDataSource, WindowTooNarrow
from fbe.datasources.registry import (
    GLOBAL,
    INDICATORS,
    SOURCE_MANUAL,
    SeriesRef,
    staleness_allowance,
)
from fbe.types import Observation
from fbe.universe import G10

__all__ = [
    "CollectionResult",
    "SeriesFailure",
    "SourceOutcome",
    "SourceStatus",
    "UnjudgedSeries",
    "collect",
    "lookback_start",
    "observed_gaps",
]


class SourceStatus(StrEnum):
    """What became of one source on one collection."""

    COMPLETED = "completed"
    """It was asked and it answered, with any number of observations."""

    PARTIAL = "partial"
    """It was asked one series at a time, some answered and some raised.

    Only a source with ``failure_scope = "series"`` can produce this. What was
    served is on the counts; what was not is on ``failures``, by name. A partial
    result is usable, and it does not change a command's exit code (ADR 0015).
    """

    SKIPPED = "skipped"
    """It was never asked: not selected, not configured, or not yet built."""

    FAILED = "failed"
    """It was asked and it raised. A named gap, not a reason to stop.

    For a series-scoped source, it served nothing and at least one series
    raised; each of those is on ``failures``. The rest may be on ``unjudged``,
    which is not a failure and does not make the source one on its own: a run
    that judged nothing and lost nothing is completed with no observations.
    """


@dataclass(frozen=True, slots=True)
class SeriesFailure:
    """One series a series-scoped source could not serve.

    Attributes:
        indicator: Canonical indicator key.
        currency: ISO 4217 code, or ``GLOBAL``.
        error: What the call raised, as ``TypeName: message``. The message is
            the source's own, which names the series id and the reason, so an
            operator can tell a cache miss from a 500 from an empty answer.

    """

    indicator: str
    currency: str
    error: str


@dataclass(frozen=True, slots=True)
class UnjudgedSeries:
    """One series whose empty answer proved nothing either way.

    Neither served nor lost, so it is neither on the counts nor on
    ``failures``. A window narrower than one release cycle, after the leg's own
    publication lag, is empty from a live series as readily as from a dead one:
    see `fbe.datasources.registry.empty_is_judgeable`. Recording it as a
    failure is a false cause on the refresh line, and dropping it is the silent
    empty success ADR 0015 rule 3 removed, so it is its own answer.

    Attributes:
        indicator: Canonical indicator key.
        currency: ISO 4217 code, or ``GLOBAL``.
        reason: The source's own message, which names the window, the lag and
            the cycle, so an operator can see why the request could not answer
            the question rather than only that it did not.

    """

    indicator: str
    currency: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """One source's contribution to one collection.

    Attributes:
        source: The source key, matching ``SeriesRef.source``.
        status: Which of the four things happened.
        series: Distinct ``(indicator, currency)`` pairs this source returned
            observations for. Counted from what came back rather than from what
            was asked, so it reconciles with ``observations`` on the same line.
            Zero for a skipped or failed source.
        observations: How many observations it returned, before the manual
            override is applied, so a source's own line says what it served
            rather than what survived.
        elapsed_seconds: Wall-clock time inside ``fetch``, summed across calls
            for a series-scoped source. Zero for a source that was never asked.
        detail: Why it was skipped, or what it raised. For a partial or a
            series-scoped failure, how many series were asked and how many
            failed, and beside that how many the window could not judge.
            Empty only when a source completed with nothing else to say, so a
            renderer cannot read an empty ``detail`` as a clean run: a
            completed outcome that judged nothing carries the count here.
        failures: The series a series-scoped source could not serve, in the
            order they were asked. Empty for a source-scoped source, whose one
            failure is on ``detail``, and always empty on a completed outcome.
        unjudged: The series whose empty answer proved nothing, in the order
            they were asked. These are not failures: the provider answered
            every one of them, and no count of them alone makes a source
            partial or failed. They do decide one boundary, because a series
            nobody could judge was not a series served: where every series
            either failed or went unjudged, nothing was served and the status
            is ``FAILED``. They are always on ``detail`` and on their own
            lines, because a source that served nothing and says nothing more
            is the quiet empty success this vocabulary exists to prevent. A
            completed outcome may carry these, and that combination is the
            point rather than a contradiction.

    Raises:
        ValueError: On construction, when ``status`` is completed and
            ``failures`` is not empty. A completed line that hides a failed
            series is exactly the quiet partial ADR 0015 forbids, so it cannot
            be built.

    """

    source: str
    status: SourceStatus
    series: int
    observations: int
    elapsed_seconds: float
    detail: str = ""
    failures: tuple[SeriesFailure, ...] = ()
    unjudged: tuple[UnjudgedSeries, ...] = ()

    def __post_init__(self) -> None:
        """Refuse the one combination of fields that would hide a failure."""
        if self.status is SourceStatus.COMPLETED and self.failures:
            raise ValueError(
                f"{self.source} cannot be completed with "
                f"{len(self.failures)} failed series; that is a partial outcome"
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
        gaps: Coverage gaps in what this run holds, keyed by indicator with
            the currencies that have no usable observation. Built by
            `observed_gaps` from the observations above, not from the
            registry's hand-check dates.

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
        For an ordinary source, the pairs its own ``refs()`` claims and its
        ``serves`` accepts, so no source is asked about an indicator the
        registry routes elsewhere or about a ref it cannot serve. For
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
    # Claimed by refs() and accepted by serves(): a ref the source holds by
    # registry but cannot serve is not asked for, so under series scope it
    # cannot be recorded as an empty answer (ADR 0016).
    return frozenset(
        pair
        for pair, ref in source.refs().items()
        if pair in requested and source.serves(ref)
    )


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

    if source.failure_scope == "series":
        return _collect_by_series(source, pairs, start, end)

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


def _collect_by_series(
    source: BaseDataSource,
    pairs: frozenset[tuple[str, str]],
    start: date,
    end: date,
) -> tuple[SourceOutcome, Sequence[Observation]]:
    """Ask a series-scoped source for one series at a time.

    Each call is caught exactly as a whole-source fetch is, so a series that
    raises is a `SeriesFailure` on the outcome and the next series is still
    asked. The source's ``fetch`` contract is unchanged: it returns or it
    raises, once per call, and it is the same instance throughout, so the
    rate limiter and the one client hold across the calls.

    The status follows from the counts with no threshold. No failure is
    completed; some failed with something served is partial; nothing served at
    all is failed. 38 of 39 failing therefore reads as partial rather than
    failed, which ADR 0015 accepts: there is no free parameter to defend, and
    coverage makes the case visible on the page.

    A series the window could not judge is none of those three. It is on
    ``unjudged`` and on ``detail``, it is not served and it is not lost, and it
    never moves the status by itself (#298).

    Args:
        source: The constructed, series-scoped source.
        pairs: The ``(indicator, currency)`` pairs routed to it, already
            intersected with the request.
        start: Earliest period, inclusive.
        end: Latest period, inclusive.

    Returns:
        The outcome and every observation the served series returned. For a
        failed outcome the observations are empty and every series is named,
        on ``failures`` or on ``unjudged``.

    """
    name = source.name
    served: list[Observation] = []
    failures: list[SeriesFailure] = []
    unjudged: list[UnjudgedSeries] = []
    elapsed = 0.0
    for indicator, currency in sorted(pairs):
        started = time.monotonic()
        try:
            served.extend(source.fetch([indicator], [currency], start, end))
        except WindowTooNarrow as error:
            # Caught before the general case and on its own, because this is
            # not a failure: the provider answered, and the window could not
            # ask the question. Naming it a failure is the false cause #298
            # reported, and dropping it is the silent empty success ADR 0015
            # rule 3 removed.
            unjudged.append(UnjudgedSeries(indicator, currency, str(error)))
        except Exception as error:  # noqa: BLE001
            # The same breadth as the whole-source catch, for the same reason:
            # one bad row in one series is that series' problem alone.
            failures.append(
                SeriesFailure(indicator, currency, f"{type(error).__name__}: {error}")
            )
        elapsed += time.monotonic() - started

    asked = len(pairs)
    observations = tuple(served)
    if not failures:
        return (
            SourceOutcome(
                source=name,
                status=SourceStatus.COMPLETED,
                series=len({(o.indicator, o.currency) for o in observations}),
                observations=len(observations),
                elapsed_seconds=elapsed,
                detail=_unjudged_detail(unjudged, asked),
                unjudged=tuple(unjudged),
            ),
            observations,
        )
    if len(failures) + len(unjudged) == asked:
        # Every series either failed or could not be judged, so nothing was
        # served. A series nobody could judge was not a series served, which
        # makes this ADR 0015's none-served case, and partial would claim a
        # success the counts do not show. Written from the two counts rather
        # than from an empty observations tuple so a source that returns no
        # rows without raising keeps the status it had before #298.
        lost = (
            f"all {asked} series failed"
            if len(failures) == asked
            else f"{len(failures)} of {asked} series failed"
        )
        return (
            SourceOutcome(
                source=name,
                status=SourceStatus.FAILED,
                series=0,
                observations=0,
                elapsed_seconds=elapsed,
                detail=_with_unjudged(lost, unjudged),
                failures=tuple(failures),
                unjudged=tuple(unjudged),
            ),
            (),
        )
    return (
        SourceOutcome(
            source=name,
            status=SourceStatus.PARTIAL,
            series=len({(o.indicator, o.currency) for o in observations}),
            observations=len(observations),
            elapsed_seconds=elapsed,
            detail=_with_unjudged(
                f"{len(failures)} of {asked} series failed", unjudged
            ),
            failures=tuple(failures),
            unjudged=tuple(unjudged),
        ),
        observations,
    )


def _unjudged_detail(unjudged: Sequence[UnjudgedSeries], asked: int) -> str:
    """Return the detail for a source that lost nothing.

    Args:
        unjudged: The series whose empty answer proved nothing.
        asked: How many series the source was asked for.

    Returns:
        Empty when every series answered, so a clean fetch prints the line it
        always did. Otherwise how many of them could not be judged, because a
        source that served nothing and says nothing else is indistinguishable
        from one that served everything there was, which is the failure #298
        reported one level along.

    """
    if not unjudged:
        return ""
    return f"{len(unjudged)} of {asked} series not judged"


def _with_unjudged(detail: str, unjudged: Sequence[UnjudgedSeries]) -> str:
    """Return ``detail`` with the unjudged count appended, where there is one.

    Args:
        detail: What the status already had to say, which already carries how
            many series were asked, so this adds the count alone.
        unjudged: The series whose empty answer proved nothing.

    Returns:
        Both facts on one line, in that order. A run can lose some series and
        fail to judge others, and reporting only the first would leave the
        second invisible on exactly the line an operator reads to find it.

    """
    if not unjudged:
        return detail
    return f"{detail}, {len(unjudged)} not judged"


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

    held = tuple(
        observation
        for _, observation in sorted(merged.items(), key=lambda item: item[0])
    )
    return CollectionResult(
        observations=held,
        outcomes=tuple(outcomes),
        gaps=observed_gaps(held, end, _judged(requested, chosen)),
    )


def _judged(
    requested: frozenset[tuple[str, str]], ran: Collection[str]
) -> frozenset[tuple[str, str]]:
    """Return the requested pairs whose absence this run could have seen.

    Args:
        requested: Every ``(indicator, currency)`` the run asked for.
        ran: The source keys that were selected for the run.

    Returns:
        The pairs whose registry ref names a source that ran, plus the pairs
        the registry has no ref for at all. A pair routed to a source the run
        did not select is left out: ``fbe refresh --source fred`` holds no
        OECD observation because it never asked for one, and reporting that as
        a gap would print every other source's data as missing.

    """
    judged: set[tuple[str, str]] = set()
    for indicator, currency in requested:
        spec = INDICATORS.get(indicator)
        if spec is None:
            continue
        ref = spec.series.get(currency)
        if ref is None or ref.source in ran:
            judged.add((indicator, currency))
    return frozenset(judged)


def observed_gaps(
    observations: Iterable[Observation],
    asof: date,
    judged: Collection[tuple[str, str]],
) -> Mapping[str, tuple[str, ...]]:
    """List the currencies the run holds no usable observation for.

    Args:
        observations: What the run collected, from every source that answered.
        asof: The run date, which every newest print is aged against.
        judged: The ``(indicator, currency)`` pairs the run could have seen,
            from `_judged`. Anything outside it is not reported either way.

    Returns:
        Indicator key to the currencies with a gap, in ``G10`` order.
        Indicators with no gap are omitted, so an empty mapping means every
        judged pair holds a print inside its allowance. An indicator with a
        ``GLOBAL`` ref reports the whole of ``G10`` when that one print is
        missing or late, because it serves all eight currencies at once.

    A pair is a gap when the run holds no observation for it, or when its
    newest observation is older than `registry.staleness_allowance` for its
    own leg and frequency. Age is ``asof`` less the newest ``period``, floored
    at zero, which is how `fbe.pillars.base.BasePillar.staleness_days` ages
    the same print. A gap here is therefore a leg the pillars will score at
    zero weight or not at all, and nothing else.

    This replaced `registry.stale_refs` as the source of the list on
    2026-10-01. That function ages ``SeriesRef.last_observed``, the date a
    person last checked the series by hand, so a refresh that had just fetched
    a two-day-old yield printed it as missing for every currency.

    """
    newest: dict[tuple[str, str], Observation] = {}
    for observation in observations:
        key = (observation.indicator, observation.currency)
        current = newest.get(key)
        if current is None or observation.period > current.period:
            newest[key] = observation

    def unusable(indicator: str, currency: str, ref: SeriesRef | None) -> bool:
        latest = newest.get((indicator, currency))
        if latest is None:
            return True
        age = max(0, (asof - latest.period).days)
        return age > staleness_allowance(ref, latest.frequency)

    gaps: dict[str, tuple[str, ...]] = {}
    for indicator, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            if (indicator, GLOBAL) in judged and unusable(
                indicator, GLOBAL, global_ref
            ):
                gaps[indicator] = tuple(G10)
            continue
        missing = tuple(
            code
            for code in G10
            if (indicator, code) in judged
            and unusable(indicator, code, spec.series.get(code))
        )
        if missing:
            gaps[indicator] = missing
    return gaps
