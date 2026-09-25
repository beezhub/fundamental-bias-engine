"""Tests for the OECD SDMX source.

This module exists because FRED's mirror of OECD prices stopped updating while
continuing to answer, so the failure this source guards against is a body that
looks like a success and is not. Three shapes of that: the throttle, which is
prose served where CSV was asked for; a key aimed at the wrong price dataflow,
which answers as though the series were dead; and an arity error, which the API
answers with a 4xx and a sentence rather than with data.

The period convention is the other half. Adopting the ruling on #27, a period
is stamped on its first day, so `2026-07` is `2026-07-01` and `2026-Q2` is
`2026-04-01`.

No test reaches the network. Every route is mocked with `respx`, every cache
lives under ``tmp_path``, and the two CSV bodies are byte-exact captures whose
provenance is recorded in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import registry
from fbe.datasources.base import BaseDataSource, FailureScope, SourceError
from fbe.datasources.oecd import (
    BASE_URL,
    BTS_ACTIVITY_MANUFACTURING,
    BTS_ADJUSTMENT,
    BTS_FLOW,
    BTS_MEASURE,
    BTS_UNIT_BALANCE,
    CORE_CPI_FLOW,
    CPI_FLOW,
    CSV_ACCEPT,
    DIMENSIONS,
    FINMARK_FLOW,
    FLOW_AGENCIES,
    FLOW_VERSIONS,
    THROTTLE_MARKER,
    OecdSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
GBR_MONTHLY = (FIXTURES / "oecd_gbr_cpi_monthly.csv").read_text()
AUS_QUARTERLY = (FIXTURES / "oecd_aus_cpi_quarterly.csv").read_text()
ARITY_ERROR = (FIXTURES / "oecd_arity_error.txt").read_text()
DEU_BTS_MONTHLY = (FIXTURES / "oecd_deu_bts_monthly.csv").read_text()
AUS_BTS_QUARTERLY = (FIXTURES / "oecd_aus_bts_quarterly.csv").read_text()

PRICES_FLOW = "DSD_PRICES@DF_PRICES_ALL"
GBR_CPI_KEY = "GBR.M.N.CPI.PA._T.N.GY"
START = date(2026, 5, 1)
END = date(2026, 7, 31)


@pytest.fixture
def source(tmp_path: Path) -> Iterator[OecdSource]:
    oecd = OecdSource(DataConfig(cache_dir=tmp_path / "cache"))
    yield oecd
    oecd.close()


def _oecd_pairs() -> list[tuple[str, str]]:
    """Every (indicator, currency) the registry routes to the OECD."""
    return [
        (key, currency)
        for key, spec in registry.INDICATORS.items()
        for currency, ref in spec.series.items()
        if ref.source == "oecd"
    ]


def _route() -> respx.Route:
    return respx.get(url__startswith=BASE_URL)


# ---------------------------------------------------------------------------
# split_series_id, which is the only thing that knows the flow/key convention
# ---------------------------------------------------------------------------


def test_split_series_id_separates_flow_from_key(source: OecdSource) -> None:
    assert source.split_series_id(f"{PRICES_FLOW}/{GBR_CPI_KEY}") == (
        PRICES_FLOW,
        GBR_CPI_KEY,
    )


def test_every_registry_series_id_splits(source: OecdSource) -> None:
    """The registry is the only producer of these strings, so it must round-trip."""
    for indicator, currency in _oecd_pairs():
        ref = registry.INDICATORS[indicator].series[currency]
        flow, key = source.split_series_id(ref.series_id)
        assert flow in FLOW_VERSIONS
        assert key
        assert f"{flow}/{key}" == ref.series_id


def test_a_series_id_with_no_separator_raises(source: OecdSource) -> None:
    with pytest.raises(ValueError):
        source.split_series_id("DSD_PRICES@DF_PRICES_ALL")


def test_an_unknown_flow_raises_rather_than_404ing_later(source: OecdSource) -> None:
    """An unknown flow has no version, so it cannot become a URL at all."""
    with pytest.raises(ValueError) as excinfo:
        source.split_series_id(f"DSD_MADE_UP@DF_NOPE/{GBR_CPI_KEY}")
    assert "DSD_MADE_UP@DF_NOPE" in str(excinfo.value)


# ---------------------------------------------------------------------------
# fetch_key: the composed URL, the header, and the failure modes
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_key_composes_the_documented_url_and_header(
    source: OecdSource,
) -> None:
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    request = route.calls.last.request
    agency = FLOW_AGENCIES[PRICES_FLOW]
    version = FLOW_VERSIONS[PRICES_FLOW]
    assert str(request.url).startswith(
        f"{BASE_URL}data/{agency},{PRICES_FLOW},{version}/{GBR_CPI_KEY}?"
    )
    assert request.url.params["startPeriod"] == START.isoformat()
    assert request.url.params["endPeriod"] == END.isoformat()
    assert request.headers["Accept"] == CSV_ACCEPT


@respx.mock
def test_the_version_comes_from_the_flow_table(
    source: OecdSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DF_FINMARK answers at 4.0 and 404s at 1.0, so this cannot be a literal."""
    monkeypatch.setitem(FLOW_VERSIONS, PRICES_FLOW, "9.9")
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert f",{PRICES_FLOW},9.9/" in str(route.calls.last.request.url)


@respx.mock
def test_fetch_key_returns_period_value_pairs_unparsed(source: OecdSource) -> None:
    """Periods stay as the API's own strings: only the caller knows the frequency."""
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    assert sorted(source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)) == [
        ("2026-05", 3.0),
        ("2026-06", 2.8),
        ("2026-07", 3.1),
    ]


@respx.mock
def test_the_captured_body_is_not_assumed_to_be_sorted(source: OecdSource) -> None:
    """The real capture arrives June, May, July. Nothing may depend on the order."""
    assert GBR_MONTHLY.splitlines()[1].endswith(",2026-06,2.8,A,,,,2")
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    returned = source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert len(returned) == 3
    assert dict(returned)["2026-05"] == 3.0


@respx.mock
def test_a_throttle_at_429_raises_rather_than_reading_as_empty(
    source: OecdSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prose parsed as CSV is zero rows, which reports a live currency as uncovered."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    body = THROTTLE_MARKER + " for data downloads or very large data ranges"
    _route().mock(return_value=httpx.Response(429, text=body))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert "429" in str(excinfo.value)


@respx.mock
def test_a_throttle_served_at_200_also_raises(source: OecdSource) -> None:
    """The status is not the guard. The body is, because prose can arrive at 200."""
    body = THROTTLE_MARKER + " for data downloads or very large data ranges"
    _route().mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert "throttl" in str(excinfo.value).lower()


@respx.mock
def test_a_throttle_at_200_never_returns_an_empty_sequence(
    source: OecdSource,
) -> None:
    body = THROTTLE_MARKER + " for data downloads"
    _route().mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError):
        result = source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
        assert result != []


@pytest.mark.parametrize("status", [403, 404, 422])
@respx.mock
def test_an_arity_or_no_records_error_raises_naming_status_and_key(
    source: OecdSource, status: int
) -> None:
    """404 NoRecordsFound and the arity error must be told apart in a log.

    403 is included because that is the status the live API actually answered
    an arity error with on 2026-09-14; the module docstring says 422. Both are
    outside ``retry_on_status``, so both raise on the first response.
    """
    _route().mock(return_value=httpx.Response(status, text=ARITY_ERROR))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    message = str(excinfo.value)
    assert str(status) in message
    assert GBR_CPI_KEY in message


@respx.mock
def test_a_no_records_404_is_not_retried(source: OecdSource) -> None:
    """Repeating a key the API has no records for cannot help."""
    route = _route().mock(return_value=httpx.Response(404, text="NoRecordsFound"))
    with pytest.raises(SourceError):
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert route.call_count == 1


@respx.mock
def test_a_body_that_is_not_csv_raises(source: OecdSource) -> None:
    _route().mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(SourceError):
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)


@respx.mock
def test_a_csv_without_the_value_column_raises(source: OecdSource) -> None:
    """A shape change must not read as a series that holds nothing."""
    _route().mock(
        return_value=httpx.Response(200, text="REF_AREA,TIME_PERIOD\nGBR,2026-06\n")
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert "OBS_VALUE" in str(excinfo.value)


@respx.mock
def test_the_columns_are_found_by_name_not_by_position(source: OecdSource) -> None:
    """The capture has sixteen columns. Their order is the API's to change."""
    body = "OBS_VALUE,TIME_PERIOD,REF_AREA\n2.8,2026-06,GBR\n"
    _route().mock(return_value=httpx.Response(200, text=body))
    assert source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END) == [("2026-06", 2.8)]


@respx.mock
def test_a_row_with_a_blank_value_is_dropped_not_zeroed(source: OecdSource) -> None:
    body = (
        "REF_AREA,TIME_PERIOD,OBS_VALUE\n"
        "GBR,2026-05,3.0\n"
        "GBR,2026-06,\n"
        "GBR,2026-07,3.1\n"
    )
    _route().mock(return_value=httpx.Response(200, text=body))
    returned = dict(source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END))
    assert "2026-06" not in returned
    assert 0.0 not in returned.values()


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
@respx.mock
def test_a_non_finite_value_raises(source: OecdSource, raw: str) -> None:
    """``float()`` accepts these, and a pillar does not survive them.

    A nan reaching a cross-sectional pillar makes the mean and the standard
    deviation nan for the whole universe, so all eight currencies score nan and
    every threshold comparison quietly reads False. An inf clips to the top of
    the band and hands one currency maximum conviction from nothing. Neither
    shows up as a coverage gap.
    """
    body = f"REF_AREA,TIME_PERIOD,OBS_VALUE\nGBR,2026-06,{raw}\n"
    _route().mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)
    assert "non-finite" in str(excinfo.value)


@respx.mock
def test_an_unreadable_value_raises(source: OecdSource) -> None:
    body = "REF_AREA,TIME_PERIOD,OBS_VALUE\nGBR,2026-06,n/a\n"
    _route().mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError):
        source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END)


# ---------------------------------------------------------------------------
# parse_period, adopting the #27 ruling: the first day of the span
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "frequency", "expected"),
    [
        ("2026-07", "M", date(2026, 7, 1)),
        ("2026-01", "M", date(2026, 1, 1)),
        ("2026-12", "M", date(2026, 12, 1)),
        ("2026-Q1", "Q", date(2026, 1, 1)),
        ("2026-Q2", "Q", date(2026, 4, 1)),
        ("2026-Q3", "Q", date(2026, 7, 1)),
        ("2026-Q4", "Q", date(2026, 10, 1)),
        ("2026", "A", date(2026, 1, 1)),
    ],
)
def test_parse_period_returns_the_first_day_of_the_span(
    source: OecdSource, period: str, frequency: str, expected: date
) -> None:
    """The #27 ruling: first day, so OECD and FRED stamps of a period sort together."""
    assert source.parse_period(period, frequency) == expected


@pytest.mark.parametrize(
    ("period", "frequency"),
    [
        ("2026-Q2", "M"),
        ("2026-07", "Q"),
        ("2026-Q5", "Q"),
        ("2026-13", "M"),
        ("not-a-period", "M"),
        ("2026-07", "W"),
        ("2026-07", "A"),
        ("2026-Q2", "A"),
        ("", "M"),
    ],
)
def test_a_period_that_does_not_match_its_frequency_raises(
    source: OecdSource, period: str, frequency: str
) -> None:
    """Guessing here silently files a quarter under a month."""
    with pytest.raises(ValueError):
        source.parse_period(period, frequency)


# ---------------------------------------------------------------------------
# cpi_key and finmark_key
# ---------------------------------------------------------------------------


def test_cpi_key_routes_a_coicop_1999_currency(source: OecdSource) -> None:
    flow, _key = source.cpi_key("GBP")
    assert flow == CPI_FLOW["GBP"] == "DSD_PRICES@DF_PRICES_ALL"


def test_cpi_key_routes_a_coicop_2018_currency(source: OecdSource) -> None:
    flow, _key = source.cpi_key("JPY")
    assert flow == CPI_FLOW["JPY"] == "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL"


def test_the_swiss_core_series_uses_the_dedicated_core_flow(
    source: OecdSource,
) -> None:
    """The only free Swiss core series, and it is in neither general flow."""
    flow, _key = source.cpi_key("CHF", core=True)
    assert flow == CORE_CPI_FLOW["CHF"]
    assert flow == "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG"
    assert flow != CPI_FLOW["CHF"]


def test_core_and_headline_differ_where_the_registry_says_they_do(
    source: OecdSource,
) -> None:
    """Canada's core is in the 2018 flow while its headline is not."""
    assert source.cpi_key("CAD")[0] == "DSD_PRICES@DF_PRICES_ALL"
    assert source.cpi_key("CAD", core=True)[0] == (
        "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL"
    )


def test_the_euro_has_no_oecd_cpi_and_says_so(source: OecdSource) -> None:
    """The OECD serves member states, not the bloc, so EUR CPI comes from FRED."""
    with pytest.raises(KeyError):
        source.cpi_key("EUR")


def test_cpi_key_agrees_with_every_registry_cpi_ref(source: OecdSource) -> None:
    """The strongest guard here: a key this builds must match the registry's own.

    Compared with the frequency segment blanked on both sides, because the
    builder is not told a frequency and the registry holds quarterly refs for
    Australia and New Zealand and monthly ones for everyone else.
    """
    for indicator, currency in _oecd_pairs():
        if indicator not in ("cpi_yoy", "core_cpi_yoy"):
            continue
        ref = registry.INDICATORS[indicator].series[currency]
        flow, key = source.cpi_key(currency, core=indicator == "core_cpi_yoy")
        ref_flow, ref_key = source.split_series_id(ref.series_id)
        assert flow == ref_flow, f"{indicator}/{currency}"
        built = key.split(".")
        expected = ref_key.split(".")
        built[1] = expected[1] = ""
        assert built == expected, f"{indicator}/{currency}"


def test_a_cpi_key_carries_one_segment_per_dimension(source: OecdSource) -> None:
    """Too few segments is a 4xx; too many has been seen to return data silently."""
    _flow, key = source.cpi_key("GBP")
    assert len(key.split(".")) == 8


def _without_trailing_blanks(segments: list[str]) -> list[str]:
    """Drop empty segments from the end of a dimension key.

    The registry's financial-market keys carry one more trailing empty segment
    than `DIMENSIONS` says the structure has. See
    ``test_the_finmark_key_arity_discrepancy_is_visible`` for the detail; the
    two forms request the same series, so the comparison below ignores it
    rather than pretending it is not there.
    """
    trimmed = list(segments)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return trimmed


def test_finmark_key_agrees_with_every_registry_finmark_ref(
    source: OecdSource,
) -> None:
    measure_for = {
        "policy_rate": "IRSTCI",
        "yield_10y": "IRLT",
        "equity_index": "SHARE",
    }
    for indicator, currency in _oecd_pairs():
        if indicator not in measure_for:
            continue
        ref = registry.INDICATORS[indicator].series[currency]
        flow, key = source.finmark_key(currency, measure_for[indicator])
        ref_flow, ref_key = source.split_series_id(ref.series_id)
        assert flow == ref_flow, f"{indicator}/{currency}"
        built = key.split(".")
        expected = ref_key.split(".")
        built[1] = expected[1] = ""
        assert _without_trailing_blanks(built) == _without_trailing_blanks(expected), (
            f"{indicator}/{currency}"
        )


def test_a_finmark_key_carries_one_segment_per_dimension(source: OecdSource) -> None:
    """Read off DIMENSIONS rather than a literal, so the table stays the authority."""
    _flow, key = source.finmark_key("JPY", "IRSTCI")
    assert len(key.split(".")) == len(DIMENSIONS["DSD_STES"])


def test_the_finmark_key_arity_discrepancy_is_visible(source: OecdSource) -> None:
    """The registry and DIMENSIONS disagree by one trailing segment. Pin it.

    All eighteen financial-market refs carry ten segments while
    ``DIMENSIONS["DSD_STES"]`` names nine dimensions, and every one of those
    refs is marked verified against the live API. A trailing empty is tolerated
    by the endpoint, so both forms fetch the same series, which is why this
    went unnoticed. `finmark_key` builds to `DIMENSIONS`, because that is what
    `fetch_key`'s docstring names as the rule, while `fetch` sends the
    registry's own string untouched.

    This test exists so the disagreement fails loudly the day someone corrects
    one side of it, rather than being silently absorbed by the comparison
    above. Raised on #57.

    Selected on `FINMARK_FLOW` rather than on the ``DSD_STES@`` prefix. The
    prefix stood in for the flow while finmark was the only one built on that
    structure; `BTS_FLOW` is the second, its refs are built to `DIMENSIONS`,
    and the prefix form would have read its eight refs as more instances of a
    defect they do not have. The BTS side is asserted below so that narrowing
    the selector does not narrow what this test protects.

    One thing is given up: the prefix form would also have caught a future
    third ``DSD_STES@`` flow written at a third arity, and the explicit
    two-flow tuple will ignore one. Adding a flow means adding it here.
    """
    by_arity: dict[str, set[int]] = {}
    for indicator, currency in _oecd_pairs():
        ref = registry.INDICATORS[indicator].series[currency]
        flow, key = ref.series_id.split("/")
        if flow in (FINMARK_FLOW, BTS_FLOW):
            by_arity.setdefault(flow, set()).add(len(key.split(".")))
    assert by_arity[FINMARK_FLOW] == {10}
    assert by_arity[BTS_FLOW] == {len(DIMENSIONS["DSD_STES"])}
    assert len(DIMENSIONS["DSD_STES"]) == 9
    assert "DSD_PRICES_COICOP2018" not in DIMENSIONS


def test_an_unknown_currency_or_measure_raises(source: OecdSource) -> None:
    with pytest.raises(KeyError):
        source.finmark_key("ZAR", "IRSTCI")
    with pytest.raises(KeyError):
        source.finmark_key("JPY", "NOT_A_MEASURE")


# ---------------------------------------------------------------------------
# refs and fetch
# ---------------------------------------------------------------------------


def test_refs_matches_the_registry_exactly(source: OecdSource) -> None:
    assert dict(source.refs()) == {
        pair: registry.INDICATORS[pair[0]].series[pair[1]] for pair in _oecd_pairs()
    }


def test_refs_is_not_empty_and_names_only_oecd(source: OecdSource) -> None:
    refs = source.refs()
    assert refs
    assert {ref.source for ref in refs.values()} == {"oecd"}


@respx.mock
def test_fetch_emits_canonical_keys_and_registry_units(source: OecdSource) -> None:
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    ref = registry.INDICATORS["cpi_yoy"].series["GBP"]
    emitted = source.fetch(["cpi_yoy"], ["GBP"], START, END)
    assert {o.indicator for o in emitted} == {"cpi_yoy"}
    assert {o.currency for o in emitted} == {"GBP"}
    assert {o.source for o in emitted} == {"oecd"}
    assert {o.unit for o in emitted} == {ref.unit}
    assert {o.series_id for o in emitted} == {ref.series_id}


@respx.mock
def test_fetch_stamps_the_first_day_of_each_period(source: OecdSource) -> None:
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    emitted = source.fetch(["cpi_yoy"], ["GBP"], START, END)
    assert {o.period for o in emitted} == {
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    }


@respx.mock
def test_fetch_stamps_a_quarterly_period_on_its_first_day(
    source: OecdSource,
) -> None:
    """2026-Q2 is 2026-04-01, which is the whole point of the #27 ruling."""
    _route().mock(return_value=httpx.Response(200, text=AUS_QUARTERLY))
    emitted = source.fetch(["cpi_yoy"], ["AUD"], START, END)
    assert {o.period for o in emitted} == {date(2026, 1, 1), date(2026, 4, 1)}


@respx.mock
def test_fetch_leaves_released_at_unset(source: OecdSource) -> None:
    """The SDMX CSV carries no per-observation release stamp, so it stays absent."""
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    emitted = source.fetch(["cpi_yoy"], ["GBP"], START, END)
    assert emitted
    assert all(o.released_at is None for o in emitted)


@respx.mock
def test_fetch_skips_pairs_belonging_to_another_source(source: OecdSource) -> None:
    """USD CPI is FRED's. Asking for it must not produce a request."""
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    assert source.fetch(["cpi_yoy"], ["USD"], START, END) == []
    assert route.call_count == 0


@respx.mock
def test_fetch_ignores_an_indicator_this_source_does_not_back(
    source: OecdSource,
) -> None:
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    assert source.fetch(["unemployment_rate"], ["GBP"], START, END) == []
    assert route.call_count == 0


@respx.mock
def test_every_request_names_one_area_and_one_measure(source: OecdSource) -> None:
    """The broad wildcard query is what earned a 429 during verification."""
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    source.fetch(["cpi_yoy", "core_cpi_yoy"], ["GBP", "JPY"], START, END)
    assert route.call_count == 4
    for call in route.calls:
        key = str(call.request.url).split("/")[-1].split("?")[0]
        segments = key.split(".")
        assert segments[0], f"reference area wildcarded in {key}"
        assert segments[3], f"measure wildcarded in {key}"


@respx.mock
def test_one_request_per_pair_rather_than_one_per_country(
    source: OecdSource,
) -> None:
    route = _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    source.fetch(["cpi_yoy", "core_cpi_yoy"], ["GBP"], START, END)
    assert route.call_count == 2


@respx.mock
def test_a_failure_on_one_pair_is_not_swallowed(source: OecdSource) -> None:
    """A throttle mid-fetch must not come back as a short but successful result."""
    _route().mock(return_value=httpx.Response(200, text=THROTTLE_MARKER + " ..."))
    with pytest.raises(SourceError):
        source.fetch(["cpi_yoy"], ["GBP"], START, END)


# ---------------------------------------------------------------------------
# The spot check, on a byte-exact capture
# ---------------------------------------------------------------------------


@respx.mock
def test_the_spot_check_pins_a_published_value_and_its_unit(
    source: OecdSource,
) -> None:
    """A recorded body, so a unit or scale change on the OECD's side breaks this.

    UK headline CPI for June 2026 came back as 2.8 percent per annum, captured
    2026-09-14. The unit assertion is the one that matters: `CPI_UNIT_PERCENT`
    paired with `CPI_TRANSFORM_YOY` means the API has already computed the
    year-on-year rate, so a switch to an index level would show up here as a
    number near 100 rather than near 3.
    """
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))
    emitted = source.fetch(["cpi_yoy"], ["GBP"], START, END)
    by_period = {o.period: o for o in emitted}
    assert by_period[date(2026, 6, 1)].value == 2.8
    assert by_period[date(2026, 6, 1)].unit == "percent"
    assert by_period[date(2026, 5, 1)].value == 3.0
    assert len(emitted) == 3


def test_the_fixture_provenance_is_recorded(source: OecdSource) -> None:
    """A fixture nobody can trace is indistinguishable from one somebody typed."""
    readme = (FIXTURES / "README.md").read_text()
    assert "oecd_gbr_cpi_monthly.csv" in readme
    assert "oecd_aus_cpi_quarterly.csv" in readme
    assert "2026-09-14" in readme


# ---------------------------------------------------------------------------
# bts_key, the business tendency surveys
# ---------------------------------------------------------------------------


def test_bts_key_pins_the_dimensions_the_fixture_came_back_with(
    source: OecdSource,
) -> None:
    """Checked against the captured response, not against the constants.

    Restating `BTS_MEASURE` and friends as literals would only assert that the
    diff agrees with itself. The captured body carries `MEASURE`,
    `UNIT_MEASURE`, `ACTIVITY` and `ADJUSTMENT` columns holding what the API
    actually served, so read the expectation out of those.

    This module already refuses to build a key from an unverified unit, because
    a wrong `UNIT_MEASURE` returns either nothing or a different series and the
    second is indistinguishable from success. The same reasoning covers
    `ACTIVITY` and `ADJUSTMENT`: wildcard either and the flow may return more
    than one row per period, which `fetch` would emit as duplicate
    `Observation`s with nothing raising.
    """
    row = next(csv.DictReader(io.StringIO(DEU_BTS_MONTHLY)))
    _flow, key = source.bts_key("EUR")
    segments = key.split(".")
    assert segments[0] == row["REF_AREA"]
    assert segments[2] == row["MEASURE"] == BTS_MEASURE
    assert segments[3] == row["UNIT_MEASURE"] == BTS_UNIT_BALANCE
    assert segments[4] == row["ACTIVITY"] == BTS_ACTIVITY_MANUFACTURING
    assert segments[5] == row["ADJUSTMENT"] == BTS_ADJUSTMENT


def test_a_bts_key_carries_one_segment_per_dimension(source: OecdSource) -> None:
    """Read off DIMENSIONS rather than a literal, so the table stays the authority."""
    _flow, key = source.bts_key("JPY")
    assert len(key.split(".")) == len(DIMENSIONS["DSD_STES"])


def test_bts_key_pins_the_frequency_rather_than_wildcarding_it(
    source: OecdSource,
) -> None:
    """The one place this key differs from `finmark_key`, and it is deliberate.

    Half these countries survey monthly and half quarterly. A wildcarded
    frequency would return both period shapes in one body where a country
    publishes both, and `_key_frequency` would have nothing to read. Pinning it
    is what makes the period unambiguous at parse time.
    """
    assert source.bts_key("GBP")[1].split(".")[1] == "M"
    assert source.bts_key("AUD")[1].split(".")[1] == "Q"


def test_bts_key_uses_germany_for_the_euro(source: OecdSource) -> None:
    """The euro-area proxy convention this module already documents on REF_AREA."""
    assert source.bts_key("EUR")[1].split(".")[0] == "DEU"


def test_bts_key_refuses_a_currency_it_has_no_frequency_for(
    source: OecdSource,
) -> None:
    """Guessing a cadence files a quarter under a month, silently.

    Matched on the message rather than on the exception type. `REF_AREA` and
    `BTS_FREQUENCY` hold the same eight currencies, so a bare
    ``pytest.raises(KeyError)`` passes on whichever lookup happens to run first
    and would keep passing with the guard deleted. It did, until the guard was
    moved ahead of the `REF_AREA` lookup.
    """
    with pytest.raises(KeyError, match="BTS_FREQUENCY"):
        source.bts_key("ZAR")


def test_bts_key_agrees_with_every_registry_business_confidence_ref(
    source: OecdSource,
) -> None:
    """The builder and the registry must not be able to drift apart."""
    spec = registry.INDICATORS["business_confidence_mfg"]
    for currency, ref in spec.series.items():
        flow, key = source.bts_key(currency)
        ref_flow, ref_key = source.split_series_id(ref.series_id)
        assert (flow, key) == (ref_flow, ref_key), currency


def test_the_bts_flow_version_and_agency_match_the_captured_body(
    source: OecdSource,
) -> None:
    """A flow missing from either table cannot be turned into a URL at all.

    The expectation comes from the fixture's own `DATAFLOW` column, which reads
    ``OECD.SDD.STES:DSD_STES@DF_BTS(4.0)``. Asserting the two constants against
    literals would restate the diff; asserting them against what the API
    answered is an independent check, and it is the one that breaks if the OECD
    republishes this flow at a new version.
    """
    row = next(csv.DictReader(io.StringIO(DEU_BTS_MONTHLY)))
    agency, _, rest = row["DATAFLOW"].partition(":")
    flow, _, version = rest.rstrip(")").partition("(")
    assert flow == BTS_FLOW
    assert FLOW_VERSIONS[BTS_FLOW] == version
    assert FLOW_AGENCIES[BTS_FLOW] == agency


@respx.mock
def test_a_monthly_bts_body_parses_to_first_day_stamped_periods(
    source: OecdSource,
) -> None:
    """German manufacturing confidence, captured 2026-09-14.

    The values are negative, which is the point of the unit assertion: a
    percentage balance is neutral at zero and routinely below it, where a
    diffusion index is neutral at 50 and never negative. A body parsed under the
    wrong unit would be obvious here and nowhere downstream.

    2026-08 is in the window on purpose. It is the `last_observed` the registry
    claims for all four monthly legs and the -11.0 that ``docs/answers/data.md``
    publishes, so the doc's worked number is a fixture rather than prose.
    """
    _route().mock(return_value=httpx.Response(200, text=DEU_BTS_MONTHLY))
    emitted = source.fetch(
        ["business_confidence_mfg"], ["EUR"], START, date(2026, 8, 31)
    )
    by_period = {o.period: o.value for o in emitted}
    assert by_period == {
        date(2026, 5, 1): -15.4,
        date(2026, 6, 1): -13.3,
        date(2026, 7, 1): -12.9,
        date(2026, 8, 1): -11.0,
    }
    assert len(emitted) == 4
    assert all(o.unit == "percentage_balance" for o in emitted)
    assert all(o.value < 0 for o in emitted)
    assert all(o.indicator == "business_confidence_mfg" for o in emitted)
    assert all(o.currency == "EUR" for o in emitted)


@respx.mock
def test_a_monthly_bts_fetch_composes_the_key_the_registry_holds(
    source: OecdSource,
) -> None:
    """The assertion the parse tests cannot make, and the one that matters most.

    `_route()` matches any URL under `BASE_URL`, so a fetch test that only reads
    the emitted observations passes unchanged if `fetch` asks for the wrong key,
    the wrong flow or the wrong version. That is exactly the failure mode this
    module exists to prevent: too many key segments answers HTTP 404
    ``NoRecordsFound``, which is indistinguishable from a dead series.

    The URL is checked against the registry's own ref rather than a literal, and
    ``tests/fixtures/README.md`` records the capture at this same key, so the
    body above is evidence for the key below rather than for a different one.
    """
    route = _route().mock(return_value=httpx.Response(200, text=DEU_BTS_MONTHLY))
    source.fetch(["business_confidence_mfg"], ["EUR"], START, date(2026, 8, 31))
    ref = registry.INDICATORS["business_confidence_mfg"].series["EUR"]
    flow, key = source.split_series_id(ref.series_id)
    request = route.calls.last.request
    assert str(request.url).startswith(
        f"{BASE_URL}data/{FLOW_AGENCIES[flow]},{flow},{FLOW_VERSIONS[flow]}/{key}?"
    )
    assert len(key.split(".")) == len(DIMENSIONS["DSD_STES"])
    assert request.headers["Accept"] == CSV_ACCEPT


@respx.mock
def test_a_quarterly_bts_body_stamps_the_first_day_of_the_quarter(
    source: OecdSource,
) -> None:
    """Australian manufacturing confidence, captured 2026-09-14.

    `2026-Q2` is `2026-04-01` under the ruling on #27. Filing it on the quarter
    end instead would make the series look three months fresher than it is, and
    the staleness ramp would give it weight it has not earned.

    Six quarters rather than three, and four distinct values among them, so a
    transposition of two adjacent periods is caught. 2025-Q4 and 2026-Q1 happen
    to share a value and a swap of just those two would still pass; every other
    pair would not.
    """
    _route().mock(return_value=httpx.Response(200, text=AUS_BTS_QUARTERLY))
    emitted = source.fetch(
        ["business_confidence_mfg"],
        ["AUD"],
        date(2025, 1, 1),
        date(2026, 6, 30),
    )
    by_period = {o.period: o.value for o in emitted}
    assert by_period == {
        date(2025, 1, 1): 4.666667,
        date(2025, 4, 1): 6.333333,
        date(2025, 7, 1): 5.0,
        date(2025, 10, 1): 9.333333,
        date(2026, 1, 1): 9.333333,
        date(2026, 4, 1): 3.666667,
    }
    assert len(emitted) == 6
    assert all(o.unit == "percentage_balance" for o in emitted)


def test_the_bts_fixture_provenance_is_recorded() -> None:
    """A fixture nobody can trace is indistinguishable from one somebody typed.

    The recorded request must carry the same key the code sends, because the
    fixture is the only evidence that key answers at all.
    """
    readme = (FIXTURES / "README.md").read_text()
    assert "oecd_deu_bts_monthly.csv" in readme
    assert "oecd_aus_bts_quarterly.csv" in readme
    assert "2026-09-14" in readme
    for currency in ("EUR", "AUD"):
        ref = registry.INDICATORS["business_confidence_mfg"].series[currency]
        assert ref.series_id.split("/")[1] in readme


# ---------------------------------------------------------------------------
# available: configuration, never connectivity
# ---------------------------------------------------------------------------


@respx.mock
def test_available_is_true_and_makes_no_request(source: OecdSource) -> None:
    """The endpoint needs no key and no directory, so nothing in the config can
    rule it out, and the answer is not allowed to cost a request."""
    route = _route().mock(return_value=httpx.Response(200, text=""))

    assert source.available() is True
    assert route.call_count == 0


@respx.mock
def test_offline_on_a_cold_cache_is_available_and_fetch_raises(
    tmp_path: Path,
) -> None:
    """Offline with nothing cached is a failed fetch, not an unavailable source.

    Reporting it here would print "unavailable, not configured", which sends
    the operator to look for a credential that does not exist. The cold cache
    is `fetch`'s to name, and it names the request it could not serve.
    """
    route = _route().mock(return_value=httpx.Response(200, text=""))
    offline = OecdSource(DataConfig(cache_dir=tmp_path / "cache", offline=True))

    assert offline.available() is True
    with pytest.raises(SourceError, match="offline"):
        offline.fetch(["cpi_yoy"], ["GBP"], START, END)
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# #275: the unit of failure is the leg, and the unit of a source class is the
# provider. The OECD's legs share one base URL, one rate limit and one
# available(), so one class per leg would be dozens of classes for one
# endpoint. The source declares the scope instead and the collector narrows.
# ---------------------------------------------------------------------------


def test_the_oecd_declares_series_scope() -> None:
    """Read by `fbe.datasources.collect`, which is what decides how widely it
    asks. On 2026-09-24 the JPN leg answered HTTP 500 after 30 seconds and the
    DEU leg answered HTTP 200 in under six, at the same moment."""
    assert OecdSource.failure_scope is FailureScope.SERIES


def test_a_source_declares_source_scope_unless_it_says_otherwise() -> None:
    """The default has to stay put: a provider that fails as one thing gains
    nothing from being asked one series at a time, and its request count would
    rise for every series it serves."""
    assert BaseDataSource.failure_scope is FailureScope.SOURCE


@respx.mock
def test_a_requested_series_with_no_rows_raises_and_names_it(
    source: OecdSource,
) -> None:
    """Rule 3 of ADR 0013 at series scope. A key aimed at the wrong dataflow
    answers exactly like a live series that holds nothing, which is how an
    unrouted currency came to read as uncovered rather than as an error."""
    _route().mock(
        return_value=httpx.Response(200, text="REF_AREA,TIME_PERIOD,OBS_VALUE\n")
    )

    with pytest.raises(SourceError) as raised:
        source.fetch(["cpi_yoy"], ["GBP"], START, END)

    message = str(raised.value)
    assert "cpi_yoy" in message
    assert "GBP" in message
    assert "GBR.M.N.CPI.PA._T.N.GY" in message


@respx.mock
def test_the_decoder_still_reads_an_empty_body_as_an_empty_series(
    source: OecdSource,
) -> None:
    """The raise belongs to `fetch`, which knows a series was asked for, and
    not to the decoder, which is only told what came back. Moving it down would
    make a body with no rows unreadable rather than empty, and the two are
    different findings."""
    _route().mock(
        return_value=httpx.Response(200, text="REF_AREA,TIME_PERIOD,OBS_VALUE\n")
    )

    assert source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END) == []


@respx.mock
def test_a_series_with_rows_is_returned_rather_than_refused(
    source: OecdSource,
) -> None:
    """The guard above must not swallow the ordinary case."""
    _route().mock(return_value=httpx.Response(200, text=GBR_MONTHLY))

    observations = source.fetch(["cpi_yoy"], ["GBP"], START, END)

    assert observations
    assert {o.indicator for o in observations} == {"cpi_yoy"}
