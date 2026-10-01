"""One series inside a series-scoped source is a named gap, not the end of it.

ADR 0015 and #275. The OECD answered 38 of 39 series on 2026-09-25, one raised,
and the collector recorded the source as failed with zero observations. These
tests pin the three rules at series scope: the other series are returned, each
failure is a status with the series named on it, and a series that answers
with nothing is still a failure. They go through `collect.collect` and the
``refresh`` command because the boundary lives in the collector, and a source
cannot be trusted to report its own partial failures.

The first half uses a fake source so the collector's own bookkeeping is under
test. The second half uses the real `OecdSource` against mocked routes, so the
opt-in and rule 3 are tested where they live. No test reaches the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fbe.cli import SOURCE_WIDTH, app
from fbe.config import DataConfig
from fbe.datasources import collect, registry
from fbe.datasources.base import (
    BaseDataSource,
    RateLimit,
    RetryPolicy,
    SourceError,
    WindowTooNarrow,
)
from fbe.datasources.oecd import BASE_URL, OecdSource
from fbe.datasources.registry import CYCLE_DAYS
from fbe.types import Frequency, Observation

FIXTURES = Path(__file__).parent / "fixtures"
GBR_MONTHLY = (FIXTURES / "oecd_gbr_cpi_monthly.csv").read_text()
HEADER_ONLY = GBR_MONTHLY.splitlines()[0] + "\n"
"""A valid SDMX CSV that carries no rows: the shape of a dead or mis-keyed key."""

START = date(2026, 5, 1)
END = date(2026, 7, 31)
PERIOD = date(2026, 6, 1)

PAIRS = (("cpi_yoy", "GBP"), ("cpi_yoy", "JPY"), ("core_cpi_yoy", "GBP"))
runner = CliRunner()


def _ref(source: str) -> registry.SeriesRef:
    return registry.SeriesRef(
        source=source,
        series_id="X",
        unit="percent",
        frequency=Frequency.MONTHLY,
        last_observed=PERIOD,
    )


def _observation(indicator: str, currency: str, source: str) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        value=2.5,
        period=PERIOD,
        source=source,
        series_id="X",
        unit="percent",
        frequency=Frequency.MONTHLY,
    )


def _per_series_source(
    name: str,
    *,
    scope: str,
    failing: Mapping[tuple[str, str], Exception],
) -> tuple[type[BaseDataSource], list[tuple[tuple[str, ...], tuple[str, ...]]]]:
    """Build a source whose ``fetch`` raises for the named pairs.

    Args:
        name: The source key.
        scope: ``"series"`` or ``"source"``; the collector reads it.
        failing: Pair to the exception ``fetch`` raises when asked for it.
            Asked for several pairs at once, the first failing one raises,
            which is the whole-source behaviour the fake must reproduce.

    Returns:
        The class and a list recording each ``fetch`` call's indicators and
        currencies, so the per-series loop can be asserted directly.

    """
    asked: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    ref_map = {pair: _ref(name) for pair in PAIRS}

    class _Built(BaseDataSource):
        rate_limit = RateLimit(requests=1000, per_seconds=60.0)
        retry = RetryPolicy(attempts=1)

        def available(self) -> bool:
            return True

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            wanted_i = tuple(sorted(indicators))
            wanted_c = tuple(sorted(currencies))
            asked.append((wanted_i, wanted_c))
            served: list[Observation] = []
            for indicator, currency in PAIRS:
                if indicator not in wanted_i or currency not in wanted_c:
                    continue
                if (indicator, currency) in failing:
                    raise failing[indicator, currency]
                served.append(_observation(indicator, currency, name))
            return served

        def refs(self) -> Mapping[tuple[str, str], registry.SeriesRef]:
            return ref_map

    _Built.name = name
    _Built.failure_scope = scope  # type: ignore[assignment]
    return _Built, asked


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(
        cache_dir=tmp_path / "cache",
        manual_dir=tmp_path / "manual",
        reports_dir=tmp_path / "reports",
    )


def _collect(
    data_config: DataConfig, *sources: type[BaseDataSource]
) -> collect.CollectionResult:
    return collect.collect(
        data_config,
        start=START,
        end=END,
        sources=sources,
        indicators=["cpi_yoy", "core_cpi_yoy"],
        currencies=["GBP", "JPY"],
    )


# ---------------------------------------------------------------------------
# The collector's own bookkeeping, with a fake source
# ---------------------------------------------------------------------------


def test_one_series_failing_is_partial_and_the_others_are_served(
    data_config: DataConfig,
) -> None:
    """Rules 1 and 2 at once: served observations, and a named failure status."""
    source, asked = _per_series_source(
        "fake", scope="series", failing={("cpi_yoy", "JPY"): SourceError("down")}
    )

    result = _collect(data_config, source)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert outcome.failures == (
        collect.SeriesFailure("cpi_yoy", "JPY", "SourceError: down"),
    )
    assert outcome.detail == "1 of 3 series failed"
    assert outcome.series == 2
    assert outcome.observations == 2
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("cpi_yoy", "GBP"),
        ("core_cpi_yoy", "GBP"),
    }
    assert len(asked) == 3


def test_a_series_scoped_source_is_asked_once_per_series(
    data_config: DataConfig,
) -> None:
    """One request per series, the same number as before, each on its own call."""
    source, asked = _per_series_source("fake", scope="series", failing={})

    result = _collect(data_config, source)

    assert result.outcomes[0].status is collect.SourceStatus.COMPLETED
    assert result.outcomes[0].failures == ()
    assert sorted(asked) == [
        (("core_cpi_yoy",), ("GBP",)),
        (("cpi_yoy",), ("GBP",)),
        (("cpi_yoy",), ("JPY",)),
    ]


def test_every_series_failing_is_failed_with_each_one_named(
    data_config: DataConfig,
) -> None:
    source, _ = _per_series_source(
        "fake",
        scope="series",
        failing={pair: SourceError(f"down {pair[1]}") for pair in PAIRS},
    )

    result = _collect(data_config, source)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.FAILED
    assert outcome.detail == "all 3 series failed"
    assert {(f.indicator, f.currency) for f in outcome.failures} == set(PAIRS)
    assert outcome.series == 0
    assert outcome.observations == 0
    assert result.observations == ()
    assert not result.usable


def test_nothing_served_is_failed_even_when_a_series_was_not_judged(
    data_config: DataConfig,
) -> None:
    """A series nobody could judge is not a series served, so this is not partial.

    Two series lost and the third unaskable leaves the run with nothing from
    this source, which is ADR 0015's none-served case however the rest is
    accounted for. Partial would claim a success the counts do not show.
    """
    source, _ = _per_series_source(
        "fake",
        scope="series",
        failing={
            ("cpi_yoy", "GBP"): SourceError("down GBP"),
            ("core_cpi_yoy", "GBP"): SourceError("down GBP core"),
            ("cpi_yoy", "JPY"): WindowTooNarrow("five days judges nothing"),
        },
    )

    result = _collect(data_config, source)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.FAILED
    assert outcome.detail == "2 of 3 series failed, 1 not judged"
    assert [(f.indicator, f.currency) for f in outcome.failures] == [
        ("core_cpi_yoy", "GBP"),
        ("cpi_yoy", "GBP"),
    ]
    assert outcome.unjudged == (
        collect.UnjudgedSeries("cpi_yoy", "JPY", "five days judges nothing"),
    )
    assert outcome.series == 0
    assert outcome.observations == 0
    assert result.observations == ()


def test_a_completed_outcome_cannot_carry_a_failed_series() -> None:
    """The quiet partial ADR 0015 forbids is unconstructable, not just untested."""
    with pytest.raises(ValueError, match="cannot be completed"):
        collect.SourceOutcome(
            source="fake",
            status=collect.SourceStatus.COMPLETED,
            series=1,
            observations=1,
            elapsed_seconds=0.0,
            failures=(collect.SeriesFailure("cpi_yoy", "JPY", "SourceError: x"),),
        )


def test_partial_is_not_completed_and_does_not_count_as_failed() -> None:
    """The three status checks downstream must each see partial as its own thing."""
    outcome = collect.SourceOutcome(
        source="fake",
        status=collect.SourceStatus.PARTIAL,
        series=2,
        observations=2,
        elapsed_seconds=0.0,
        detail="1 of 3 series failed",
        failures=(collect.SeriesFailure("cpi_yoy", "JPY", "SourceError: x"),),
    )
    result = collect.CollectionResult(
        observations=(_observation("cpi_yoy", "GBP", "fake"),),
        outcomes=(outcome,),
        gaps={},
    )
    assert outcome.status is not collect.SourceStatus.COMPLETED
    assert result.failed == ()
    assert result.usable


def test_a_source_scoped_source_still_fails_whole(data_config: DataConfig) -> None:
    """Opting in is explicit. A source that did not is treated exactly as before."""
    source, asked = _per_series_source(
        "whole", scope="source", failing={("cpi_yoy", "JPY"): SourceError("down")}
    )

    result = _collect(data_config, source)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.FAILED
    assert outcome.failures == ()
    assert "SourceError: down" in outcome.detail
    assert result.observations == ()
    assert len(asked) == 1


def test_the_base_declares_source_scope_by_default() -> None:
    assert BaseDataSource.failure_scope == "source"
    assert OecdSource.failure_scope == "series"


# ---------------------------------------------------------------------------
# The refresh command: the counts line marked partial, one line per failure
# ---------------------------------------------------------------------------


def test_refresh_prints_the_partial_line_and_each_failed_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 2 as the operator reads it: which currency lost which indicator."""
    source, _ = _per_series_source(
        "fake", scope="series", failing={("cpi_yoy", "JPY"): SourceError("down")}
    )
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (source,))
    monkeypatch.setattr("fbe.datasources.collect.ALL_SOURCES", (source,))
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
    )

    result = runner.invoke(app, ["--config", str(config), "refresh"])

    lines = result.stdout.splitlines()
    counts = next(line for line in lines if line.startswith("fake"))
    assert "2 series" in counts
    assert "partial (1 of 3 series failed)" in counts
    assert "completed" not in counts
    failure = lines[lines.index(counts) + 1]
    assert failure.startswith("  fake")
    assert "JPY cpi_yoy failed (SourceError: down)" in failure
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# The real OECD source: the opt-in, rule 3, and offline
# ---------------------------------------------------------------------------


def _key_for(indicator: str, currency: str) -> str:
    """A pattern matching the URL the source builds for one registry pair.

    The dimension key sits in the path, dots included, so it is escaped and
    matched as a regular expression: respx's URL pattern has no substring
    lookup.
    """
    key = registry.INDICATORS[indicator].series[currency].series_id.split("/", 1)[1]
    return re.escape(key)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


@respx.mock
def test_one_oecd_series_failing_costs_that_series_alone(
    data_config: DataConfig,
) -> None:
    """The 2026-09-25 morning, replayed: 503 on one key, the rest answer."""
    respx.get(url__regex=_key_for("cpi_yoy", "JPY")).mock(
        return_value=httpx.Response(503, text="down")
    )
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=GBR_MONTHLY)
    )

    result = _collect(data_config, OecdSource)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "JPY")]
    assert "503" in outcome.failures[0].error
    served = {(o.indicator, o.currency) for o in result.observations}
    assert served == {
        ("cpi_yoy", "GBP"),
        ("core_cpi_yoy", "GBP"),
        ("core_cpi_yoy", "JPY"),
    }
    # What the score sees: JPY keeps its core reading and loses only the
    # headline, GBP loses nothing. The pillar renders that one absence as n/a,
    # which tests/test_cli_score.py pins.
    assert ("core_cpi_yoy", "JPY") in served
    assert ("cpi_yoy", "JPY") not in served


@respx.mock
def test_an_oecd_series_with_no_rows_in_the_window_is_a_failure(
    data_config: DataConfig,
) -> None:
    """Rule 3. A valid CSV with no observations is a dead or mis-keyed series."""
    respx.get(url__regex=_key_for("cpi_yoy", "JPY")).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=GBR_MONTHLY)
    )

    result = _collect(data_config, OecdSource)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "JPY")]
    assert "served no observation" in outcome.failures[0].error
    assert len({(o.indicator, o.currency) for o in result.observations}) == 3


@respx.mock
def test_an_oecd_series_with_no_rows_raises_from_fetch_itself(
    data_config: DataConfig,
) -> None:
    """Rule 3 is the source's, so it holds without the collector too."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    source = OecdSource(data_config)
    try:
        with pytest.raises(SourceError, match="served no observation"):
            source.fetch(["cpi_yoy"], ["GBP"], START, END)
    finally:
        source.close()


@respx.mock
def test_offline_with_one_series_uncached_serves_the_rest(
    data_config: DataConfig,
) -> None:
    """The 38-of-39 case, offline: the cache miss is one series' own failure."""
    respx.get(url__regex=_key_for("cpi_yoy", "JPY")).mock(
        return_value=httpx.Response(503, text="down")
    )
    online = respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=GBR_MONTHLY)
    )
    _collect(data_config, OecdSource)
    assert online.call_count == 3
    respx.reset()
    later = respx.route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))

    result = _collect(replace(data_config, offline=True), OecdSource)

    assert later.call_count == 0
    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "JPY")]
    assert "offline" in outcome.failures[0].error
    assert len({(o.indicator, o.currency) for o in result.observations}) == 3


# ---------------------------------------------------------------------------
# #298: a window too narrow to judge is a third answer
#
# Rule 3 reasons about the default five-year lookback. `fbe refresh --since` a
# few days back asks every quarterly series for a window that cannot hold a
# print, so each answers empty and rule 3 names each as dead: a healthy
# provider reported as a dead one, which is the mirror of what #275 fixed.
# Not raising is only half the answer. Before #275 the same command gave the
# OECD a completed line with zero observations, and returning to that would
# trade a loud wrong cause for a quiet wrong outcome.
# ---------------------------------------------------------------------------

NARROW_START = date(2026, 7, 26)
"""Five days before END. `--since` a few days back, which is the reported case."""


def _oecd_pairs() -> frozenset[tuple[str, str]]:
    """Every ``(indicator, currency)`` the registry routes to the OECD.

    Counted from the registry rather than written down, so a leg added or
    retired changes the expectation with it instead of leaving a stale number
    that the refresh line would quietly disagree with.
    """
    return frozenset(
        (indicator, currency)
        for indicator, spec in registry.INDICATORS.items()
        for currency, ref in spec.series.items()
        if ref.source == registry.SOURCE_OECD
    )


def _collect_narrow(
    data_config: DataConfig, *sources: type[BaseDataSource]
) -> collect.CollectionResult:
    """The same request as `_collect`, over a window that judges nothing."""
    return collect.collect(
        data_config,
        start=NARROW_START,
        end=END,
        sources=sources,
        indicators=["cpi_yoy", "core_cpi_yoy"],
        currencies=["GBP", "JPY"],
    )


@respx.mock
def test_a_narrow_window_does_not_name_an_empty_series_as_dead(
    data_config: DataConfig,
) -> None:
    """The reported defect. Every series answers empty over five days, and not
    one of them is evidence of anything."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )

    result = _collect_narrow(data_config, OecdSource)

    outcome = result.outcomes[0]
    assert outcome.failures == ()
    assert outcome.status is not collect.SourceStatus.FAILED


@respx.mock
def test_a_narrow_window_does_not_read_as_a_clean_fetch_either(
    data_config: DataConfig,
) -> None:
    """The other half, and the one that matters more. A source that served
    nothing and is recorded as completed with no further word is the silent
    empty success #275 removed."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )

    result = _collect_narrow(data_config, OecdSource)

    outcome = result.outcomes[0]
    assert outcome.observations == 0
    assert outcome.series == 0
    assert [(u.indicator, u.currency) for u in outcome.unjudged] == [
        ("core_cpi_yoy", "GBP"),
        ("core_cpi_yoy", "JPY"),
        ("cpi_yoy", "GBP"),
        ("cpi_yoy", "JPY"),
    ]
    assert outcome.detail == "4 of 4 series not judged"
    assert outcome.status is collect.SourceStatus.COMPLETED


@respx.mock
def test_the_three_answers_are_distinguishable_on_one_run(
    data_config: DataConfig,
) -> None:
    """Served, dead and not judged, side by side in one outcome.

    The monthly legs clear the gate over this window and the quarterly ones do
    not, so one request shape produces all three answers: GBP's monthly series
    serves rows, JPY's monthly series answers empty and is a failure, and the
    quarterly legs answer empty and are not judged. Anything that collapses
    two of the three into one fails here.
    """
    respx.get(url__regex=_key_for("cpi_yoy", "JPY")).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    respx.get(url__regex=_key_for("cpi_yoy", "AUD")).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=GBR_MONTHLY)
    )

    result = collect.collect(
        data_config,
        start=START,
        end=END,
        sources=(OecdSource,),
        indicators=["cpi_yoy"],
        currencies=["GBP", "JPY", "AUD"],
    )

    outcome = result.outcomes[0]
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("cpi_yoy", "GBP")
    }
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "JPY")]
    assert [(u.indicator, u.currency) for u in outcome.unjudged] == [("cpi_yoy", "AUD")]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert outcome.series == 1
    assert outcome.detail == "1 of 3 series failed, 1 not judged"


@respx.mock
def test_one_quarterly_cycle_is_still_not_judged_through_the_source(
    data_config: DataConfig,
) -> None:
    """The ruling's third point, through `OecdSource.fetch` rather than the
    gate alone. 92 days contains a quarterly period boundary, so a rule written
    on frequency would judge this window and name the series dead. The period
    it contains carries a 120-day lag and is not published yet."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )

    result = collect.collect(
        data_config,
        start=END - timedelta(days=CYCLE_DAYS[Frequency.QUARTERLY]),
        end=END,
        sources=(OecdSource,),
        indicators=["cpi_yoy"],
        currencies=["AUD"],
    )

    outcome = result.outcomes[0]
    assert outcome.failures == ()
    assert [(u.indicator, u.currency) for u in outcome.unjudged] == [("cpi_yoy", "AUD")]


@respx.mock
def test_the_default_lookback_still_names_a_dead_series(
    data_config: DataConfig,
) -> None:
    """Rule 3 survives for the case it was written for, which is what stops
    this change undoing #275. Five years of quarterly is 1826 - 120 = 1706
    against a cycle of 92."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )

    result = collect.collect(
        data_config,
        start=date(2021, 7, 31),
        end=END,
        sources=(OecdSource,),
        indicators=["cpi_yoy"],
        currencies=["AUD"],
    )

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.FAILED
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "AUD")]
    assert outcome.unjudged == ()


@respx.mock
def test_a_narrow_window_that_serves_rows_is_just_served(
    data_config: DataConfig,
) -> None:
    """The gate decides what an empty answer means and nothing else. A series
    that answers with rows over a narrow window is data, and this must not
    quietly discard it."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=GBR_MONTHLY)
    )

    result = _collect_narrow(data_config, OecdSource)

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.COMPLETED
    assert outcome.unjudged == ()
    assert outcome.observations > 0
    assert outcome.detail == ""


@respx.mock
def test_an_unjudged_series_is_not_a_failure_for_the_exit_code(
    data_config: DataConfig,
) -> None:
    """A run that judged nothing collected nothing, so `usable` is already
    False and the command exits 1 on an empty cache. What must not happen is
    the run reading as a source outage: the provider answered every request."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )

    result = _collect_narrow(data_config, OecdSource)

    assert result.failed == ()
    assert not result.usable


@respx.mock
def test_fetch_itself_refuses_to_call_a_narrow_window_dead(
    data_config: DataConfig,
) -> None:
    """The gate is the source's, as rule 3 is, so it holds without the
    collector. `WindowTooNarrow` is a `SourceError`, so a caller that catches
    the base class still catches it; the type is what lets the collector tell
    the two apart."""
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    source = OecdSource(data_config)
    try:
        with pytest.raises(WindowTooNarrow) as raised:
            source.fetch(["cpi_yoy"], ["AUD"], START, END)
        message = str(raised.value)
        assert "cpi_yoy" in message
        assert "AUD" in message
        # The window, the lag and the cycle, recomputed here rather than read
        # off the implementation: 2026-05-01 to 2026-07-31 is 91 days, the
        # Australian quarterly leg carries the 120-day default, and a
        # quarterly cycle is 92 days. An operator who cannot see all three
        # cannot tell a narrow window from a broken registry entry.
        assert message.endswith(
            "between 2026-05-01 and 2026-07-31; that window judges nothing, "
            "because 91 days less this leg's 120-day publication lag does not "
            "span the 92-day quarterly cycle"
        )
        assert "dead" not in message
        assert isinstance(raised.value, SourceError)
    finally:
        source.close()


@respx.mock
def test_refresh_names_every_series_it_could_not_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the operator reads. The counts line says how many were not judged,
    and every one of them takes its own line: "37 of 38 series not judged"
    tells nobody which currency lost which pillar, which is ADR 0015 rule 2
    applied to this third answer.

    The window is five days back from the run date rather than a fixed date,
    because ``refresh`` ends its window at today. A fixed ``--since`` would
    widen by a day every day and start judging the monthly legs a few weeks
    from now, and the test would then fail for the calendar rather than for
    the code.
    """
    respx.get(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, text=HEADER_ONLY)
    )
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (OecdSource,))
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
    )
    today = date.today()
    since = today - timedelta(days=5)
    routed = _oecd_pairs()

    result = runner.invoke(
        app, ["--config", str(config), "refresh", "--since", since.isoformat()]
    )

    rows = result.stdout.splitlines()
    counts = next(row for row in rows if row.startswith("oecd"))
    assert f"({len(routed)} of {len(routed)} series not judged)" in counts
    named = [row for row in rows if "not judged" in row and not row.startswith("oecd")]
    assert len(named) == len(routed)
    ref = registry.series_for("cpi_yoy", "GBP")
    assert ref is not None
    label = "  oecd".ljust(SOURCE_WIDTH)
    assert (
        f"{label}GBP cpi_yoy not judged (oecd served no observation for "
        f"cpi_yoy GBP ({ref.series_id}) between {since} and {today}; that "
        f"window judges nothing, because 5 days less this leg's 45-day "
        f"publication lag does not span the 31-day monthly cycle)"
    ) in rows
    assert "dead" not in result.stdout
    assert result.exit_code == 1
