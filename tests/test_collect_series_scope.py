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
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fbe.cli import app
from fbe.config import DataConfig
from fbe.datasources import collect, registry
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy, SourceError
from fbe.datasources.oecd import BASE_URL, OecdSource
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
