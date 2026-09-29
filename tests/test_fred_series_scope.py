"""FRED at series scope, and the ref it declines before it is routed.

ADR 0016 and #296. FRED had the first-error-wins loop the OECD had before
#275, and one more wrinkle: two USD refs it holds by registry carry a
transform FRED's ``units`` cannot express, and under series scope the
collector would have asked for each alone and recorded the empty answer as a
failure every morning. The ruling: a source may decline a ref before it is
routed, through `serves`, and a test ties that hook to the registry so a
declined ref is always a named gap and never a quiet drop.

Through `collect.collect` wherever the claim is about what the collector
records; through `fetch` where the claim is the source's own. No test reaches
the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES, collect, registry
from fbe.datasources.base import BaseDataSource, SourceError
from fbe.datasources.fred import BASE_URL, FredSource

KEY = "abcdef0123456789abcdef0123456789"
OBSERVATIONS_URL = f"{BASE_URL}series/observations"
START = date(2026, 6, 1)
END = date(2026, 6, 30)

USD_TWO_YEAR = registry.INDICATORS["yield_2y"].series["USD"].series_id
USD_CPI = registry.INDICATORS["cpi_yoy"].series["USD"].series_id
DECLINED = ("yield_2y_chg_1m", "yield_2y_chg_3m")
"""The two USD refs FRED holds by registry and cannot serve (ADR 0004)."""


def _body(*rows: tuple[str, str]) -> dict[str, object]:
    return {
        "observations": [
            {
                "realtime_start": "2026-09-12",
                "realtime_end": "9999-12-31",
                "date": day,
                "value": value,
            }
            for day, value in rows
        ]
    }


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(
        fred_api_key=KEY,
        cache_dir=tmp_path / "cache",
        manual_dir=tmp_path / "manual",
        reports_dir=tmp_path / "reports",
    )


def _collect(
    data_config: DataConfig, indicators: list[str]
) -> collect.CollectionResult:
    return collect.collect(
        data_config,
        start=START,
        end=END,
        sources=(FredSource,),
        indicators=indicators,
        currencies=["USD"],
    )


def _serve(series_id: str, *rows: tuple[str, str]) -> respx.Route:
    return respx.get(OBSERVATIONS_URL, params__contains={"series_id": series_id}).mock(
        return_value=httpx.Response(200, json=_body(*rows))
    )


# ---------------------------------------------------------------------------
# Series scope: one series failing costs that series
# ---------------------------------------------------------------------------


def test_fred_declares_series_scope() -> None:
    assert FredSource.failure_scope == "series"


@respx.mock
def test_one_fred_series_failing_is_partial_and_the_rest_are_served(
    data_config: DataConfig,
) -> None:
    """The #169 morning under the new rule: one series raises, the other lands."""
    respx.get(OBSERVATIONS_URL, params__contains={"series_id": USD_CPI}).mock(
        return_value=httpx.Response(503, text="down")
    )
    _serve(USD_TWO_YEAR, ("2026-06-30", "3.55"))

    result = _collect(data_config, ["yield_2y", "cpi_yoy"])

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "USD")]
    assert "503" in outcome.failures[0].error
    assert outcome.detail == "1 of 2 series failed"
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("yield_2y", "USD")
    }


# ---------------------------------------------------------------------------
# The declined refs: no request, no failure, still a registry gap
# ---------------------------------------------------------------------------


@respx.mock
def test_a_declined_ref_is_neither_requested_nor_counted(
    data_config: DataConfig,
) -> None:
    """The two change refs make no request and do not enter the denominator.

    Before ADR 0016 series scope would have asked for each alone, received
    nothing, and printed two failure lines every morning for series nobody
    could serve.
    """
    route = _serve(USD_TWO_YEAR, ("2026-06-30", "3.55"))

    result = _collect(data_config, ["yield_2y", *DECLINED])

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.COMPLETED
    assert outcome.failures == ()
    assert outcome.series == 1
    assert route.call_count == 1
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("yield_2y", "USD")
    }
    # Declined is not dropped: both refs stay on the coverage-gap report.
    for indicator in DECLINED:
        assert "USD" in result.gaps[indicator]


@respx.mock
def test_a_declined_ref_does_not_enter_the_partial_denominator(
    data_config: DataConfig,
) -> None:
    respx.get(OBSERVATIONS_URL, params__contains={"series_id": USD_CPI}).mock(
        return_value=httpx.Response(503, text="down")
    )
    _serve(USD_TWO_YEAR, ("2026-06-30", "3.55"))

    result = _collect(data_config, ["yield_2y", "cpi_yoy", *DECLINED])

    assert result.outcomes[0].detail == "1 of 2 series failed"


def test_fred_serves_exactly_the_transforms_units_can_express(
    data_config: DataConfig,
) -> None:
    source = FredSource(data_config)
    try:
        declined = {
            pair for pair, ref in source.refs().items() if not source.serves(ref)
        }
    finally:
        source.close()
    assert declined == {(indicator, "USD") for indicator in DECLINED}


# ---------------------------------------------------------------------------
# Rule 3: a requested series with nothing in the window is a failure
# ---------------------------------------------------------------------------


@respx.mock
def test_a_fred_series_with_no_rows_in_the_window_raises(
    data_config: DataConfig,
) -> None:
    """Before #296 this returned nothing and the currency went quietly uncovered."""
    _serve(USD_TWO_YEAR)
    source = FredSource(data_config)
    try:
        with pytest.raises(SourceError, match="served no observation"):
            source.fetch(["yield_2y"], ["USD"], START, END)
    finally:
        source.close()


@respx.mock
def test_a_fred_series_with_no_rows_is_a_named_failure_through_the_collector(
    data_config: DataConfig,
) -> None:
    _serve(USD_CPI)
    _serve(USD_TWO_YEAR, ("2026-06-30", "3.55"))

    result = _collect(data_config, ["yield_2y", "cpi_yoy"])

    outcome = result.outcomes[0]
    assert outcome.status is collect.SourceStatus.PARTIAL
    assert [(f.indicator, f.currency) for f in outcome.failures] == [("cpi_yoy", "USD")]
    assert "served no observation" in outcome.failures[0].error


@respx.mock
def test_a_body_of_only_missing_values_is_a_failure_not_a_zero(
    data_config: DataConfig,
) -> None:
    """FRED's null is the string "."; a series made only of it served nothing."""
    _serve(USD_TWO_YEAR, ("2026-06-29", "."), ("2026-06-30", "."))
    source = FredSource(data_config)
    try:
        with pytest.raises(SourceError, match="served no observation"):
            source.fetch(["yield_2y"], ["USD"], START, END)
    finally:
        source.close()


# ---------------------------------------------------------------------------
# The guard: a declined ref must be one the registry already calls unverified
# ---------------------------------------------------------------------------


def _declined_verified(
    source_class: type[BaseDataSource], data_config: DataConfig
) -> set[tuple[str, str]]:
    source = source_class(data_config)
    try:
        return {
            pair
            for pair, ref in source.refs().items()
            if not source.serves(ref) and ref.fetchable
        }
    finally:
        source.close()


@pytest.mark.parametrize(
    "source_class", ALL_SOURCES, ids=lambda cls: getattr(cls, "name", cls.__name__)
)
def test_every_declined_ref_is_already_a_registry_gap(
    source_class: type[BaseDataSource], data_config: DataConfig
) -> None:
    """A hook that means "do not ask me" is the shape a quiet drop takes.

    So every ref a source declines must be one `stale_refs` already lists,
    which keeps it on the coverage-gap report. A source that wants to decline
    a ref the registry calls verified is a new decision, not an edit here.
    """
    assert _declined_verified(source_class, data_config) == set()


def test_the_guard_would_catch_a_source_declining_a_verified_ref(
    data_config: DataConfig,
) -> None:
    """The test above has teeth: a throwaway source that declines DGS2 fails it."""

    class _Declining(FredSource):
        def serves(self, ref: registry.SeriesRef) -> bool:
            return ref.series_id != USD_TWO_YEAR

    assert ("yield_2y", "USD") in _declined_verified(_Declining, data_config)


def test_the_base_serves_everything() -> None:
    class _Plain(BaseDataSource):
        name = "plain"

        def available(self) -> bool:
            return True

        def fetch(self, indicators, currencies, start, end):  # type: ignore[no-untyped-def]
            return ()

        def refs(self) -> Mapping[tuple[str, str], registry.SeriesRef]:
            return {}

    source = _Plain(DataConfig())
    try:
        assert source.serves(registry.INDICATORS["yield_2y"].series["USD"])
    finally:
        source.close()
