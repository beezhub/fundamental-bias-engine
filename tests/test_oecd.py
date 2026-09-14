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

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import registry
from fbe.datasources.base import SourceError
from fbe.datasources.oecd import (
    BASE_URL,
    CORE_CPI_FLOW,
    CPI_FLOW,
    CSV_ACCEPT,
    DIMENSIONS,
    FLOW_AGENCIES,
    FLOW_VERSIONS,
    THROTTLE_MARKER,
    OecdSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
GBR_MONTHLY = (FIXTURES / "oecd_gbr_cpi_monthly.csv").read_text()
AUS_QUARTERLY = (FIXTURES / "oecd_aus_cpi_quarterly.csv").read_text()
ARITY_ERROR = (FIXTURES / "oecd_arity_error.txt").read_text()

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


@respx.mock
def test_a_header_with_no_rows_is_an_empty_series_not_an_error(
    source: OecdSource,
) -> None:
    """A window that held no observations is data, and differs from a failure."""
    _route().mock(
        return_value=httpx.Response(200, text="REF_AREA,TIME_PERIOD,OBS_VALUE\n")
    )
    assert source.fetch_key(PRICES_FLOW, GBR_CPI_KEY, START, END) == []


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
    """
    finmark_refs = [
        registry.INDICATORS[indicator].series[currency]
        for indicator, currency in _oecd_pairs()
        if registry.INDICATORS[indicator]
        .series[currency]
        .series_id.startswith("DSD_STES@")
    ]
    assert finmark_refs
    registry_arity = {
        len(ref.series_id.split("/")[1].split(".")) for ref in finmark_refs
    }
    assert registry_arity == {10}
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
