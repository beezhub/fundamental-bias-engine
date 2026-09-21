"""Tests for the three flat-format curve providers: the ECB, the BoC and the RBA.

Each provider publishes exactly one currency and none can stand in for another,
so the failures worth testing are the ones where a currency quietly gets
somebody else's number, or gets a number at all when its provider is down. The
2-year yield is where MONETARY draws most of its sub-weight, so a wrong one here
is a wrong bias on a real pair.

The RBA parser gets the most attention. Its table carries a metadata block whose
height has changed between releases, so the row labelled ``Series ID`` is found
by its label and the series by its column within that row, never by counting.

No test reaches the network. Every route is mocked with `respx`, every cache
lives under ``tmp_path``, and the three bodies are live captures whose
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
from fbe.datasources.curves import (
    BOC_BASE_URL,
    ECB_BASE_URL,
    ECB_VALUE_COLUMN,
    RBA_F2_URL,
    RBA_SERIES_ID_ROW_LABEL,
    TWO_YEAR_REFS,
    CurvesSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
BOC_BODY = (FIXTURES / "boc_2y_yield.json").read_text()
ECB_BODY = (FIXTURES / "ecb_2y_spot.csv").read_text()
RBA_BODY = (FIXTURES / "rba_f2_2y.csv").read_text(encoding="utf-8-sig")

BOC_SERIES = "BD.CDN.2YR.DQ.YLD"
ECB_KEY = "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
RBA_SERIES = "FCMYGBAG2D"

START = date(2026, 9, 1)
END = date(2026, 9, 10)

# Read off the captures by hand. Asserting against these rather than against
# whatever the parser returns is the point: a parser that reads the wrong
# column would agree with itself.
BOC_EXPECTED = {
    date(2026, 9, 1): 3.01,
    date(2026, 9, 2): 3.11,
    date(2026, 9, 3): 3.10,
    date(2026, 9, 4): 3.08,
}
ECB_EXPECTED_LAST = (date(2026, 9, 4), 2.8766374271)
RBA_EXPECTED_LAST = (date(2026, 9, 9), 4.835)


@pytest.fixture
def source(tmp_path: Path) -> Iterator[CurvesSource]:
    curves = CurvesSource(DataConfig(cache_dir=tmp_path / "cache"))
    yield curves
    curves.close()


def _curve_pairs() -> list[tuple[str, str]]:
    return [
        (key, currency)
        for key, spec in registry.INDICATORS.items()
        for currency, ref in spec.series.items()
        if ref.source in registry.CURVE_SOURCES
    ]


# ---------------------------------------------------------------------------
# fetch_boc: the only non-flat payload here
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_boc_parses_the_nested_valet_shape(source: CurvesSource) -> None:
    """``{"d": date, "<series>": {"v": value}}``, keyed by the series name."""
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    assert dict(source.fetch_boc(BOC_SERIES, START, END)) == BOC_EXPECTED


@respx.mock
def test_fetch_boc_returns_rows_oldest_first(source: CurvesSource) -> None:
    """Served newest first, because the capture is already ascending.

    Asserting order against an ordered body proves the input was ordered, not
    that anything here orders it.
    """
    body = (
        '{"observations": ['
        '{"d": "2026-09-04", "BD.CDN.2YR.DQ.YLD": {"v": "3.08"}},'
        '{"d": "2026-09-01", "BD.CDN.2YR.DQ.YLD": {"v": "3.01"}},'
        '{"d": "2026-09-03", "BD.CDN.2YR.DQ.YLD": {"v": "3.10"}}]}'
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    days = [day for day, _value in source.fetch_boc(BOC_SERIES, START, END)]
    assert days == [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 4)]


@respx.mock
def test_fetch_rba_returns_rows_oldest_first(source: CurvesSource) -> None:
    """Table F2 arrives chronological, so the order is reversed to test it."""
    lines = RBA_BODY.splitlines()
    header_at = next(
        i for i, line in enumerate(lines) if line.startswith(RBA_SERIES_ID_ROW_LABEL)
    )
    reversed_body = "\n".join(
        lines[: header_at + 1] + list(reversed(lines[header_at + 1 :]))
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=reversed_body))
    days = [day for day, _value in source.fetch_rba(RBA_SERIES, START, END)]
    assert days == sorted(days)
    assert days[0] == date(2026, 9, 2)


@respx.mock
def test_fetch_boc_sends_the_documented_window(source: CurvesSource) -> None:
    route = respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    source.fetch_boc(BOC_SERIES, START, END)
    request = route.calls.last.request
    assert f"observations/{BOC_SERIES}/json" in str(request.url)
    assert request.url.params["start_date"] == START.isoformat()
    assert request.url.params["end_date"] == END.isoformat()


@respx.mock
def test_fetch_boc_reads_the_series_it_was_asked_for(source: CurvesSource) -> None:
    """The value hangs off a key named after the series, so a wrong key is a gap."""
    body = '{"observations": [{"d": "2026-09-01", "SOMETHING.ELSE": {"v": "3.01"}}]}'
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_boc(BOC_SERIES, START, END)
    assert BOC_SERIES in str(excinfo.value)


@respx.mock
def test_fetch_boc_drops_a_row_with_no_value(source: CurvesSource) -> None:
    body = (
        '{"observations": ['
        '{"d": "2026-09-01", "BD.CDN.2YR.DQ.YLD": {"v": "3.01"}},'
        '{"d": "2026-09-02", "BD.CDN.2YR.DQ.YLD": {"v": ""}}]}'
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    returned = dict(source.fetch_boc(BOC_SERIES, START, END))
    assert date(2026, 9, 2) not in returned
    assert 0.0 not in returned.values()


# ---------------------------------------------------------------------------
# fetch_ecb
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_ecb_composes_the_documented_url(source: CurvesSource) -> None:
    route = respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    source.fetch_ecb(ECB_KEY, START, END)
    request = route.calls.last.request
    assert str(request.url).startswith(f"{ECB_BASE_URL}{ECB_KEY}?")
    assert request.url.params["format"] == "csvdata"
    assert request.url.params["startPeriod"] == START.isoformat()
    assert request.url.params["endPeriod"] == END.isoformat()


@respx.mock
def test_fetch_ecb_parses_the_capture(source: CurvesSource) -> None:
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    returned = dict(source.fetch_ecb(ECB_KEY, START, END))
    assert len(returned) == 4
    assert returned[ECB_EXPECTED_LAST[0]] == ECB_EXPECTED_LAST[1]


@respx.mock
def test_fetch_ecb_finds_its_columns_by_name(source: CurvesSource) -> None:
    """The capture has forty columns and their order is the ECB's to change."""
    body = "OBS_VALUE,TIME_PERIOD,KEY\n2.5,2026-09-01,X\n"
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    assert source.fetch_ecb(ECB_KEY, START, END) == [(date(2026, 9, 1), 2.5)]


@respx.mock
def test_fetch_ecb_rejects_a_body_without_the_value_column(
    source: CurvesSource,
) -> None:
    """A body that clears ``_decode`` and still has no value column.

    ``_decode`` lets anything JSON-shaped through, because that is the Bank of
    Canada's shape and it cannot tell which provider asked. So the ECB parser
    has to check its own columns rather than assume the gate upstream did it.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text='{"not": "a csvdata table"}')
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_ecb(ECB_KEY, START, END)
    assert ECB_VALUE_COLUMN in str(excinfo.value)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "Infinity"])
@respx.mock
def test_a_non_finite_yield_is_refused(source: CurvesSource, raw: str) -> None:
    """``float()`` accepts these and a pillar does not survive them.

    A nan reaching a cross-sectional pillar makes the mean and the standard
    deviation nan for the whole universe, so all eight currencies score nan and
    every threshold comparison quietly reads False. An inf clips to the top of
    the band and hands one currency maximum conviction from nothing.
    """
    body = f"TIME_PERIOD,OBS_VALUE\n2026-09-01,{raw}\n"
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_ecb(ECB_KEY, START, END)
    assert "non-finite" in str(excinfo.value)


@respx.mock
def test_rows_are_sorted_rather_than_trusted_to_arrive_in_order(
    source: CurvesSource,
) -> None:
    """The captures happen to arrive ascending. That is the provider's choice.

    Asserting order against a body that is already ordered proves the input was
    sorted, not that anything sorts it, so this serves them newest first.
    """
    body = "TIME_PERIOD,OBS_VALUE\n2026-09-04,2.9\n2026-09-01,2.7\n2026-09-03,2.8\n"
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    returned = source.fetch_ecb(ECB_KEY, START, END)
    assert [day for day, _value in returned] == [
        date(2026, 9, 1),
        date(2026, 9, 3),
        date(2026, 9, 4),
    ]


# ---------------------------------------------------------------------------
# fetch_rba, where the metadata block moves
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_rba_parses_the_capture(source: CurvesSource) -> None:
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    returned = dict(source.fetch_rba(RBA_SERIES, START, END))
    assert returned[RBA_EXPECTED_LAST[0]] == RBA_EXPECTED_LAST[1]
    assert returned[date(2026, 9, 4)] == 4.761


@respx.mock
def test_fetch_rba_parses_dd_mon_yyyy_dates(source: CurvesSource) -> None:
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    days = [day for day, _value in source.fetch_rba(RBA_SERIES, START, END)]
    assert date(2026, 9, 2) in days
    assert days == sorted(days)


@respx.mock
def test_fetch_rba_keys_off_the_label_not_a_row_offset(
    source: CurvesSource,
) -> None:
    """The metadata block's height has changed between releases before.

    An extra row above the ``Series ID`` row must not move the answer, which is
    the whole reason the label is the anchor.
    """
    shifted = "Extra metadata row the RBA added,,,,,\n" + RBA_BODY
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=shifted))
    assert dict(source.fetch_rba(RBA_SERIES, START, END))[date(2026, 9, 4)] == 4.761


@respx.mock
def test_fetch_rba_reads_the_column_the_series_id_sits_in(
    source: CurvesSource,
) -> None:
    """Four other series share the table. Reading a neighbour is a wrong yield."""
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    two_year = dict(source.fetch_rba(RBA_SERIES, START, END))
    ten_year = dict(source.fetch_rba("FCMYGBAG10D", START, END))
    assert two_year[date(2026, 9, 4)] == 4.761
    assert ten_year[date(2026, 9, 4)] == 5.165
    assert two_year != ten_year


@respx.mock
def test_fetch_rba_raises_when_the_series_row_is_absent(
    source: CurvesSource,
) -> None:
    body = "F2 CAPITAL MARKET YIELDS\nTitle,Something\n01-Sep-2026,1.0\n"
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_rba(RBA_SERIES, START, END)
    assert RBA_SERIES_ID_ROW_LABEL in str(excinfo.value)


@respx.mock
def test_fetch_rba_raises_for_a_series_not_in_the_table(
    source: CurvesSource,
) -> None:
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_rba("FCMYNOTAREALSERIES", START, END)
    assert "FCMYNOTAREALSERIES" in str(excinfo.value)


@respx.mock
def test_fetch_rba_skips_a_session_with_no_value_for_this_series(
    source: CurvesSource,
) -> None:
    """Early rows of the real table carry only the ten-year column."""
    body = RBA_BODY + "10-Sep-2026,,,,5.20,2.8\n"
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=body))
    returned = dict(source.fetch_rba(RBA_SERIES, START, END))
    assert date(2026, 9, 10) not in returned
    assert 0.0 not in returned.values()


@respx.mock
def test_fetch_rba_bounds_the_window_it_returns(source: CurvesSource) -> None:
    """The whole table comes back whatever is asked, so the window is applied here."""
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    returned = dict(source.fetch_rba(RBA_SERIES, date(2026, 9, 3), date(2026, 9, 4)))
    assert set(returned) == {date(2026, 9, 3), date(2026, 9, 4)}


# ---------------------------------------------------------------------------
# Shared refusals: a body no parser here can read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["<html><body>maintenance</body></html>", "", "not data at all"],
)
@respx.mock
def test_a_body_no_parser_recognises_is_refused(
    source: CurvesSource, body: str
) -> None:
    """Refused in ``_decode``, so it never reaches the cache.

    ``_request`` writes the cache only after decoding succeeds. A maintenance
    page served at 200 and cached would come back for the whole TTL, and an
    offline run expires nothing.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    with pytest.raises(SourceError):
        source.fetch_ecb(ECB_KEY, START, END)


@pytest.mark.parametrize(
    ("body", "host"),
    [
        ("TIME_PERIOD,OBS_VALUE\nnot-a-date,2.5\n", ECB_BASE_URL),
        ('{"observations": [{"d": "31-09-2026", "X": {"v": "1"}}]}', BOC_BASE_URL),
    ],
)
@respx.mock
def test_an_unreadable_session_date_raises_a_source_error(
    source: CurvesSource, body: str, host: str
) -> None:
    """Not the bare ValueError the date parser would otherwise emit.

    The collector in #61 records a `SourceError` as a named coverage gap and
    carries on; any other exception type aborts the whole refresh over one
    malformed row.
    """
    respx.get(url__startswith=host).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError):
        if host == ECB_BASE_URL:
            source.fetch_ecb(ECB_KEY, START, END)
        else:
            source.fetch_boc("X", START, END)


@respx.mock
def test_an_unreadable_rba_session_date_raises_a_source_error(
    source: CurvesSource,
) -> None:
    body = RBA_BODY + "the-32nd-of-never,4.8,4.8,4.8,5.2,2.8\n"
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError):
        source.fetch_rba(RBA_SERIES, START, END)


@respx.mock
def test_a_refused_body_is_not_written_to_the_cache(
    source: CurvesSource, tmp_path: Path
) -> None:
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    with pytest.raises(SourceError):
        source.fetch_ecb(ECB_KEY, START, END)
    cache = tmp_path / "cache"
    assert not any(p.is_file() for p in cache.rglob("*")) or not any(
        "maintenance" in p.read_text(errors="ignore")
        for p in cache.rglob("*")
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# refs and fetch
# ---------------------------------------------------------------------------


def test_refs_matches_the_curve_sources_in_the_registry(
    source: CurvesSource,
) -> None:
    assert dict(source.refs()) == {
        pair: registry.INDICATORS[pair[0]].series[pair[1]] for pair in _curve_pairs()
    }


def test_refs_names_only_curve_providers(source: CurvesSource) -> None:
    refs = source.refs()
    assert refs
    assert {ref.source for ref in refs.values()} <= registry.CURVE_SOURCES


def test_refs_carries_the_three_providers_this_issue_serves(
    source: CurvesSource,
) -> None:
    sources = {ref.source for ref in source.refs().values()}
    assert {"ecb", "boc", "rba"} <= sources


@respx.mock
def test_fetch_returns_yields_for_the_three_currencies(
    source: CurvesSource,
) -> None:
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    emitted = source.fetch(["yield_2y"], ["EUR", "CAD", "AUD"], START, END)
    assert {o.currency for o in emitted} == {"EUR", "CAD", "AUD"}
    assert {o.indicator for o in emitted} == {"yield_2y"}
    assert {o.unit for o in emitted} == {"percent"}


@respx.mock
def test_each_observation_records_the_institution_that_published_it(
    source: CurvesSource,
) -> None:
    """ "curves" would lose the one fact a reader wants when a number looks wrong."""
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    emitted = source.fetch(["yield_2y"], ["EUR", "CAD", "AUD"], START, END)
    by_currency = {o.currency: o.source for o in emitted}
    assert by_currency == {"EUR": "ecb", "CAD": "boc", "AUD": "rba"}
    assert "curves" not in set(by_currency.values())


@respx.mock
def test_fetch_leaves_released_at_unset(source: CurvesSource) -> None:
    """None of these three publishes a per-observation release stamp."""
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    emitted = source.fetch(["yield_2y"], ["CAD"], START, END)
    assert emitted
    assert all(o.released_at is None for o in emitted)


@respx.mock
def test_a_provider_failure_names_the_provider_and_the_currency(
    source: CurvesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(503, text="down")
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch(["yield_2y"], ["CAD"], START, END)
    message = str(excinfo.value)
    assert "boc" in message
    assert "CAD" in message


@pytest.mark.parametrize(
    ("currency", "host"),
    [("EUR", ECB_BASE_URL), ("CAD", BOC_BASE_URL), ("AUD", RBA_F2_URL)],
)
@respx.mock
def test_a_failing_provider_substitutes_nobody_elses_curve(
    source: CurvesSource, monkeypatch: pytest.MonkeyPatch, currency: str, host: str
) -> None:
    """Each provider covers one currency, so a fallback would be another country's."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    respx.get(url__startswith=host).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(SourceError):
        source.fetch(["yield_2y"], [currency], START, END)


@respx.mock
def test_fetch_requests_nothing_for_a_currency_this_issue_does_not_serve(
    source: CurvesSource,
) -> None:
    """CHF routes to manual. Asking for it must not reach a provider."""
    route = respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    assert source.fetch(["yield_2y"], ["CHF"], START, END) == []
    assert route.call_count == 0


@respx.mock
def test_fetch_ignores_an_indicator_this_source_does_not_back(
    source: CurvesSource,
) -> None:
    route = respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    assert source.fetch(["cpi_yoy"], ["CAD"], START, END) == []
    assert route.call_count == 0


@pytest.mark.parametrize("transform", ["chg_1m", "chg_3m"])
@respx.mock
def test_a_transform_this_source_cannot_emit_is_skipped_not_guessed(
    source: CurvesSource, transform: str
) -> None:
    """Same treatment `FredSource` gives these two, for the same reason.

    ADR 0004 settles the derivation but no source computes it yet, and the
    emitted unit would be wrong until one does: the ref says percent and the
    value would be basis points. Nothing is emitted and nothing is requested,
    so the pillar sees an absence rather than a guess on the heaviest
    sub-indicator in the model.
    """
    route = respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    emitted = source.fetch([f"yield_2y_{transform}"], ["CAD"], START, END)
    assert emitted == []
    assert route.call_count == 0


@respx.mock
def test_an_unemittable_transform_does_not_cost_the_level_series(
    source: CurvesSource,
) -> None:
    """Issue #169. Raising here took every other curve down with it."""
    route = respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    emitted = source.fetch(["yield_2y_chg_1m", "yield_2y"], ["CAD"], START, END)
    assert {o.indicator for o in emitted} == {"yield_2y"}
    assert emitted
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# The spot check, on live captures
# ---------------------------------------------------------------------------


@respx.mock
def test_the_spot_check_pins_a_published_yield_and_its_unit(
    source: CurvesSource,
) -> None:
    """One published value per provider, from bodies captured 2026-09-14.

    The three sit close together, between 2.8 and 4.8 percent, which is what a
    2-year government yield looks like. A provider read in basis points, or one
    currency served another's curve, moves one of these and not the others.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))
    emitted = source.fetch(["yield_2y"], ["EUR", "CAD", "AUD"], START, END)
    on_the_fourth = {o.currency: o for o in emitted if o.period == date(2026, 9, 4)}
    assert on_the_fourth["EUR"].value == 2.8766374271
    assert on_the_fourth["CAD"].value == 3.08
    assert on_the_fourth["AUD"].value == 4.761
    assert {o.unit for o in on_the_fourth.values()} == {"percent"}


def test_the_two_year_refs_table_covers_these_providers() -> None:
    assert TWO_YEAR_REFS["EUR"] == ("ecb", ECB_KEY)
    assert TWO_YEAR_REFS["CAD"] == ("boc", BOC_SERIES)
    assert TWO_YEAR_REFS["AUD"] == ("rba", RBA_SERIES)


def test_the_fixture_provenance_is_recorded() -> None:
    """A capture nobody can trace cannot be told from one somebody typed."""
    readme = (FIXTURES / "README.md").read_text()
    for name in ("boc_2y_yield.json", "ecb_2y_spot.csv", "rba_f2_2y.csv"):
        assert name in readme
    assert "truncated" in readme


# ---------------------------------------------------------------------------
# available: configuration, never connectivity
# ---------------------------------------------------------------------------


@respx.mock
def test_available_is_true_and_makes_no_request(source: CurvesSource) -> None:
    """No provider here takes a key, so nothing in the config can rule the
    source out, and the answer is not allowed to cost a request to any of
    them."""
    route = respx.route().mock(return_value=httpx.Response(200, text=""))

    assert source.available() is True
    assert route.call_count == 0


@respx.mock
def test_offline_on_a_cold_cache_is_available_and_fetch_raises(
    tmp_path: Path,
) -> None:
    """Offline with nothing cached is a failed fetch, not an unavailable source.

    Reporting it here would print "unavailable, not configured", which sends
    the operator to look for a credential that does not exist. The cold cache
    is `fetch`'s to name, and it names the provider it could not serve.
    """
    route = respx.route().mock(return_value=httpx.Response(200, text=""))
    offline = CurvesSource(DataConfig(cache_dir=tmp_path / "cache", offline=True))

    assert offline.available() is True
    with pytest.raises(SourceError, match="offline"):
        offline.fetch(["yield_2y"], ["AUD"], date(2026, 8, 1), date(2026, 9, 1))
    assert route.call_count == 0
