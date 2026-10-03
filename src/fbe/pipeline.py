"""Build one morning run into a `BiasReport`, for every front end to share.

`fbe report` built its report through private helpers in `fbe.cli`, which left
any second front end two bad choices: copy them, and drift, or skip them, and
publish a report the terminal would not have written. Both produce a page that
disagrees with the audit trail on the same inputs, which is the one failure a
renderer here cannot be allowed (ADR 0018). So the run is built here, once, and
the CLI and the web API both call it.

This module computes nothing of its own. It sequences the cache read, the
scorer, the pair differencing and the filters, and it names the two ways a run
can be unusable. Printing, exit codes and argument checks stay with the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from fbe.bias import apply_filters, build_pair_biases, shortlist
from fbe.calendar_guard import CalendarCoverage, DailyCalendar
from fbe.datasources import ALL_SOURCES, CalendarSource
from fbe.datasources.base import SourceError
from fbe.datasources.collect import collect, lookback_start
from fbe.pillars import default_pillars
from fbe.scoring import score_currencies
from fbe.types import BiasReport, CurrencyScore, PairBias, TradeIdea
from fbe.universe import G10

if TYPE_CHECKING:
    from fbe.config import Config

__all__ = [
    "CalendarReader",
    "UnusableRunError",
    "bias_notes",
    "build_run",
    "coverage_collapsed",
    "daily_calendar",
    "filtered_pairs",
    "score_notes",
]

CalendarReader = Callable[["Config", date], DailyCalendar | None]
"""Reads the day's calendar for a run, or returns ``None`` when it was not read.

Injected into `build_run` rather than imported, so a caller decides where the
clock comes from and a test can switch the network fetch off.
"""


class UnusableRunError(Exception):
    """The run produced nothing that should be written or traded on.

    Its own type so a caller cannot swallow it with a stray ``except
    ValueError``. The message is written for the operator and says what to run
    next. The CLI prints it and exits `fbe.cli.EXIT_UNUSABLE`.
    """


def build_run(
    config: Config,
    run_date: date,
    *,
    read_calendar: CalendarReader,
) -> BiasReport:
    """Build the run `fbe report` records, from the cache only.

    Cache only, for the reason `fbe score` gives at the same call: a rolled-over
    TTL refetching mid-session would let two runs at the same date and the same
    digest write two different reports with nothing in either to explain it.
    That matters more here, because the result is the record.

    Every number on the result is read off the scorer and the bias layer. The
    calendar is read after the cache is known to hold something, so a run with
    nothing to score makes no network request for a calendar it will not use.

    Args:
        config: The effective config. The caller has already validated it.
        run_date: The run's as-of date. Observations released after it are not
            read.
        read_calendar: Reads the day's calendar, normally `daily_calendar` with
            the caller's clock bound in. ``None`` from it records
            ``event:unchecked`` on every pair.

    Returns:
        The run, with ``generated_at`` set to the current instant in UTC and the
        shortlist capped at ``config.risk.max_concurrent_positions``.

    Raises:
        ValueError: From `lookback_start`, when ``scoring.lookback_years``
            cannot produce a window ending at ``run_date``.
        UnusableRunError: When the cache held nothing for the window, or when
            every currency came back at zero coverage. A report of a run that
            scored on no data is 28 neutral pairs and eight composites of zero,
            and as tomorrow's baseline it would turn a data outage into a
            one-day fundamental move on every currency.

    """
    start = lookback_start(run_date, config.scoring.lookback_years)
    result = collect(
        replace(config.data, offline=True),
        start=start,
        end=run_date,
        sources=ALL_SOURCES,
    )
    if not result.usable:
        raise UnusableRunError(
            "No observations in the cache for this window, so there is nothing "
            "to report. Run fbe refresh to fill it, or fbe doctor to find out "
            "why it is empty."
        )

    scores = score_currencies(
        result.observations,
        default_pillars(config.scoring),
        config.scoring,
        run_date,
    )
    if coverage_collapsed(scores):
        raise UnusableRunError(
            "Every currency scored on no usable data, so there is nothing to "
            "record. Run fbe score to see which pillars came up short, and "
            "fbe doctor to find out why."
        )

    by_currency = {score.currency: score for score in scores}
    daily = read_calendar(config, run_date)
    pairs = filtered_pairs(
        build_pair_biases(scores, config, run_date),
        by_currency,
        config,
        run_date,
        daily,
    )
    return BiasReport(
        asof=run_date,
        generated_at=datetime.now(UTC),
        currencies=tuple(scores),
        pairs=pairs,
        events=() if daily is None else daily.events_on(run_date),
        # The cap lives in RiskConfig and nowhere else: there is no purpose in
        # shortlisting more trades than the risk rules permit to be open.
        shortlist=tuple(
            TradeIdea(bias=row)
            for row in shortlist(pairs, config.risk.max_concurrent_positions)
        ),
        warnings=(*score_notes(scores), *bias_notes(consulted=daily is not None)),
        config_digest=config.digest(),
    )


def coverage_collapsed(scores: Sequence[CurrencyScore]) -> bool:
    """Whether the run scored nothing at all.

    Args:
        scores: Every currency the scorer returned, before any row filter.

    Returns:
        True when no currency held any usable pillar weight. Judged on the
        whole run rather than on the printed rows: ``--currency JPY`` on a
        currency with no data is a thin currency in a working run, and the run
        is what the exit code describes.

    """
    return bool(scores) and all(row.coverage <= 0.0 for row in scores)


def score_notes(rows: Sequence[CurrencyScore]) -> tuple[str, ...]:
    """Collect the reasons pillars could not score, in first-seen order.

    Args:
        rows: The rows being printed.

    Returns:
        Each distinct non-empty note, once, from the pillars that could not
        score, in first-seen order.

        Only the unscored ones, and the distinction is what this function is
        for. `fbe.scoring.score_currencies` catches a pillar that raises and
        records why on each currency's copy of that score, so the same sentence
        arrives eight times and is worth printing once; dropping it would leave
        a run on six pillars looking like a run on seven, with only the
        coverage column hinting at it. That is a run-level fact and it belongs
        here, under the table and in the JSON ``warnings`` key.

        A pillar that *did* score writes something different into the same
        field: `BasePillar._notes` puts the working behind that currency's
        headline number there, which is per currency by nature and never
        repeats. Collected here it would publish eight lines of ordinary
        working as warnings on a healthy run, growing to fifty-six once every
        pillar has the hook, and a script reading the JSON key would find a
        run where nothing went wrong indistinguishable from one where six
        pillars failed.

        An earlier version of this docstring argued the opposite, on the
        grounds that `compute` puts the blend divisor path and the assumed-lag
        count on the notes of scored pillars. It does not, and had not since
        issue #51: both live in `PillarScore.blend_divisor_path` and
        `PillarScore.diagnostics` precisely so that nothing has to read them
        out of prose.

        ``z is None`` is the test rather than the absence of a score, because
        that is the marker every other consumer in the package uses for the
        same question.

    """
    seen: list[str] = []
    for row in rows:
        for score in row.pillars.values():
            if score.z is None and score.notes and score.notes not in seen:
                seen.append(score.notes)
    return tuple(seen)


def bias_notes(*, consulted: bool) -> tuple[str, ...]:
    """Say what the run did with the calendar, and which checks did not run.

    Args:
        consulted: Whether the run's calendar reader returned a calendar.

    Returns:
        One line. When the calendar was read, it says each pair lists its
        no-entry windows and that the morning run blocks nothing for them, so
        the entry time is the trader's to check against them (#335). When it
        was not read, the line it has always printed.

        `apply_filters` records ``cost:unchecked`` and ``event:unchecked`` on
        every pair, so the blackout and the dealing cost announce their own
        absence on the row. The 24-hour conviction cap does not: passing no
        `fbe.bias.EventHorizonGuard` leaves `build_pair_biases` assuming no
        event, and the tier it prints is the uncapped one with nothing beside
        it saying so. A pair can print ``high`` on an FOMC evening and look
        exactly like a pair checked and cleared.

        The cap is never applied, even when the calendar is read: with news
        on most days it would demote most pairs most days (#334). So the tier
        is not adjusted for a release, and the line says so rather than
        leaving the reader to assume a check ran. Absence with a name, never a
        plausible default.

    """
    if consulted:
        return (
            "Calendar read: each pair lists its no-entry windows for today as "
            "event:window. Nothing here is blocked or demoted for a release "
            "later in the day, so check your entry time against the windows.",
        )
    return (
        "No calendar was consulted: the blackout filter and the 24-hour "
        "conviction cap did not run, so no tier here is capped for an "
        "imminent release. Check the calendar by hand before acting on a row.",
    )


def daily_calendar(
    config: Config, run_date: date, now: datetime
) -> DailyCalendar | None:
    """Read the calendar for a morning run, or say it was not read.

    Args:
        config: The effective config. ``config.data.offline`` makes the source
            read its cache rather than the feed, which is what a report run
            after ``fbe refresh`` does.
        run_date: The run's date.
        now: The current instant, aware and in UTC. Passed in rather than read
            so the caller owns the clock and a test can pin it.

    Returns:
        ``None`` when ``run_date`` is not today, in UTC or in the local zone
        that ``date.today()`` gives the commands' default: the feed carries the
        current week only, so reading it for an older date would find nothing
        and print that day as clear. ``None`` leaves every pair at
        ``event:unchecked``. Otherwise the day, with every impact level
        fetched so `fbe.calendar_guard.is_high_impact` decides what qualifies,
        and with a failed fetch carried as a failed `CalendarCoverage`, which
        marks every pair ``event:unknown`` rather than clear.

    """
    if run_date not in (now.date(), date.today()):
        return None
    start = datetime(run_date.year, run_date.month, run_date.day, tzinfo=UTC)
    before = timedelta(minutes=config.data.calendar_blackout_before_min)
    after = timedelta(minutes=config.data.calendar_blackout_after_min)
    source = CalendarSource(config.data)
    try:
        events = tuple(
            source.events(
                list(G10),
                start - after,
                start + timedelta(days=1) + before,
                # The lowest level, so every release is fetched and
                # `is_high_impact` alone decides which ones open a window.
                min_impact="low",
            )
        )
    except SourceError as error:
        coverage = CalendarCoverage(
            events=(),
            covers_through=source.horizon(),
            fetch_ok=False,
            fetch_error=str(error),
        )
    else:
        coverage = CalendarCoverage(events=events, covers_through=source.horizon())
    return DailyCalendar(coverage, config.data, now)


def filtered_pairs(
    biases: Sequence[PairBias],
    by_currency: Mapping[str, CurrencyScore],
    config: Config,
    run_date: date,
    daily: DailyCalendar | None,
) -> tuple[PairBias, ...]:
    """Apply the filters, with the calendar's hooks when it was read.

    Args:
        biases: Every pair, unfiltered.
        by_currency: The run's scores.
        config: The effective config.
        run_date: The run's date.
        daily: From the run's calendar reader. ``None`` passes no hooks, which
            records ``event:unchecked``.

    Returns:
        The filtered pairs, in the order given.

    """
    if daily is None:
        return tuple(
            apply_filters(row, by_currency, config, run_date) for row in biases
        )
    return tuple(
        apply_filters(
            row,
            by_currency,
            config,
            run_date,
            calendar_guard=daily.guard,
            calendar_windows=daily.windows,
        )
        for row in biases
    )
