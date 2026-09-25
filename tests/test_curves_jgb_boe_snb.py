"""Tests for the three awkward curve providers: MoF, Bank of England and SNB.

Split from `tests/test_curves.py` the way #59 is split from #58: a Shift-JIS
CSV pair, a workbook inside a ZIP behind a redirect, and a frozen cube are
three different parsing problems that happen to share a module.

What each one can get quietly wrong:

* The MoF publishes a missing tenor as ``-``. Read as a number that is a zero
  yield, and a zero JGB yield is not implausible enough to notice.
* The Bank of England answers an unknown series code with HTTP 200 and an HTML
  page, so a status check passes and the parser sees a web page.
* The yield curve workbook's 2-year header is ``1.999999920000001``, and the
  maturity grid has been re-cut before, so a column found by position or by
  equality returns a different tenor without saying so.
* The SNB cube is frozen. Serving its last value as though it were current is
  the failure `provider_health` exists to make visible.

No test reaches the network. Every body is a live capture whose provenance is
recorded in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import datetime as dt
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
    BOE_GLC_DATE_EPOCH,
    BOE_GLC_MEMBER,
    BOE_GLC_SHEET,
    BOE_IADB_HEADER_START,
    BOE_IADB_URL,
    BOE_YIELD_CURVE_ZIP,
    ECB_BASE_URL,
    MOF_JP_CURRENT_URL,
    MOF_JP_HISTORY_URL,
    RBA_F2_URL,
    RBNZ_B2_URL,
    SERVED_TRANSFORMS,
    SNB_BOND_CUBE,
    SNB_CUBE_URL,
    SNB_TENOR_2Y,
    CurvesSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
JGB_CURRENT = (FIXTURES / "jgbcme.csv").read_bytes()
JGB_HISTORY = (FIXTURES / "jgbcme_all.csv").read_bytes()
IADB_BODY = (FIXTURES / "boe_iadb_bank_rate.csv").read_text()
YIELD_CURVE_ZIP = (FIXTURES / "boe_yield_curve.zip").read_bytes()
SNB_BODY = (FIXTURES / "snb_rendoblid.csv").read_bytes()

SNB_URL = SNB_CUBE_URL.format(cube=SNB_BOND_CUBE)

START = date(2026, 9, 1)
END = date(2026, 9, 30)
WIDE_START = date(1970, 1, 1)

# Read off the captures by hand rather than from the parser.
JGB_CURRENT_FIRST = (date(2026, 9, 1), 1.802)
JGB_CURRENT_LAST = (date(2026, 9, 11), 1.844)
JGB_HISTORY_LAST = (date(2026, 8, 31), 1.743)
JGB_DASH_DAY = date(1978, 5, 22)
IADB_LAST = (date(2026, 9, 11), 3.75)
CURVE_FIRST = (date(2026, 9, 1), 4.385949119079563)
SNB_LAST = (date(2025, 7, 31), -0.083)


@pytest.fixture
def source(tmp_path: Path) -> Iterator[CurvesSource]:
    # manual_dir under tmp_path as well: provider_health asks the RBNZ, which
    # reads any workbook dropped into the repository's data/manual (#283).
    curves = CurvesSource(
        DataConfig(cache_dir=tmp_path / "cache", manual_dir=tmp_path / "manual")
    )
    yield curves
    curves.close()


def _mof_routes() -> None:
    respx.get(MOF_JP_CURRENT_URL).mock(
        return_value=httpx.Response(200, content=JGB_CURRENT)
    )
    respx.get(MOF_JP_HISTORY_URL).mock(
        return_value=httpx.Response(200, content=JGB_HISTORY)
    )


# ---------------------------------------------------------------------------
# fetch_jgb: Shift-JIS, two files, and the dash
# ---------------------------------------------------------------------------


@respx.mock
def test_the_current_month_and_the_history_are_both_read(
    source: CurvesSource,
) -> None:
    """Neither file covers the range alone: the history stops at last month."""
    _mof_routes()
    returned = dict(source.fetch_jgb("2Y", WIDE_START, END))
    assert returned[JGB_CURRENT_LAST[0]] == JGB_CURRENT_LAST[1]
    assert returned[JGB_HISTORY_LAST[0]] == JGB_HISTORY_LAST[1]


@respx.mock
def test_a_dash_tenor_yields_no_observation(source: CurvesSource) -> None:
    """A missing tenor read as a number is a zero JGB yield, which scores."""
    _mof_routes()
    returned = dict(source.fetch_jgb("2Y", WIDE_START, END))
    assert JGB_DASH_DAY not in returned
    assert 0.0 not in returned.values()


@respx.mock
def test_the_japanese_footer_row_is_dropped(source: CurvesSource) -> None:
    """The current-month file ends with a blank row and a Japanese notice."""
    _mof_routes()
    returned = source.fetch_jgb("2Y", WIDE_START, END)
    assert returned
    assert all(isinstance(day, date) for day, _value in returned)


def test_the_current_month_capture_really_is_shift_jis() -> None:
    """Guards the fixture, so the decoder cannot be proved by an ASCII body."""
    with pytest.raises(UnicodeDecodeError):
        JGB_CURRENT.decode("utf-8")
    assert "※" in JGB_CURRENT.decode("shift_jis")


@respx.mock
def test_a_body_that_is_not_shift_jis_raises(source: CurvesSource) -> None:
    respx.get(MOF_JP_CURRENT_URL).mock(
        return_value=httpx.Response(200, content=b"\xff\xfe\x00bad")
    )
    respx.get(MOF_JP_HISTORY_URL).mock(
        return_value=httpx.Response(200, content=JGB_HISTORY)
    )
    with pytest.raises(SourceError):
        source.fetch_jgb("2Y", WIDE_START, END)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026/9/1", date(2026, 9, 1)),
        ("2026/9/11", date(2026, 9, 11)),
        ("1978/5/22", date(1978, 5, 22)),
        ("2026/12/31", date(2026, 12, 31)),
    ],
)
def test_unpadded_dates_parse(source: CurvesSource, raw: str, expected: date) -> None:
    """``YYYY/M/D`` with no zero padding, including single-digit month and day."""
    assert source._parse_jgb_date(raw) == expected


@respx.mock
def test_the_current_month_wins_on_overlap(source: CurvesSource) -> None:
    """Both files can carry a day; the current-month file is the fresher one."""
    overlap = (
        "Interest Rate (September 2026),,\r\nDate,1Y,2Y\r\n2026/9/1,1.0,9.999\r\n"
    ).encode("shift_jis")
    respx.get(MOF_JP_HISTORY_URL).mock(
        return_value=httpx.Response(200, content=overlap)
    )
    respx.get(MOF_JP_CURRENT_URL).mock(
        return_value=httpx.Response(200, content=JGB_CURRENT)
    )
    returned = dict(source.fetch_jgb("2Y", START, END))
    assert returned[date(2026, 9, 1)] == JGB_CURRENT_FIRST[1]
    assert 9.999 not in returned.values()


@respx.mock
def test_an_unknown_tenor_raises(source: CurvesSource) -> None:
    _mof_routes()
    with pytest.raises(SourceError) as excinfo:
        source.fetch_jgb("7Z", WIDE_START, END)
    assert "7Z" in str(excinfo.value)


@respx.mock
def test_the_jgb_window_is_applied(source: CurvesSource) -> None:
    _mof_routes()
    returned = dict(source.fetch_jgb("2Y", date(2026, 9, 2), date(2026, 9, 3)))
    assert set(returned) == {date(2026, 9, 2), date(2026, 9, 3)}


# ---------------------------------------------------------------------------
# fetch_boe_iadb: a redirect that must be followed, HTML that must not be read
# ---------------------------------------------------------------------------


@respx.mock
def test_the_bank_rate_comes_back_per_code(source: CurvesSource) -> None:
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text=IADB_BODY)
    )
    returned = source.fetch_boe_iadb(["IUDBEDR"], START, END)
    assert dict(returned["IUDBEDR"])[IADB_LAST[0]] == IADB_LAST[1]


@respx.mock
def test_the_iadb_query_is_the_documented_shape(source: CurvesSource) -> None:
    route = respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text=IADB_BODY)
    )
    source.fetch_boe_iadb(["IUDBEDR"], date(2026, 9, 1), date(2026, 9, 11))
    params = route.calls.last.request.url.params
    assert params["SeriesCodes"] == "IUDBEDR"
    assert params["Datefrom"] == "01/Sep/2026"
    assert params["Dateto"] == "11/Sep/2026"
    assert params["csv.x"] == "yes"
    assert params["UsingCodes"] == "Y"


@respx.mock
def test_the_redirect_is_followed_rather_than_refused(
    source: CurvesSource,
) -> None:
    """The documented URL answers 302 by design, every time, not on failure.

    The shared request path treats a 3xx as a moved endpoint and raises. This
    source opts into following one, because here the redirect is the endpoint.
    """
    target = "https://www.bankofengland.co.uk/boeapps/database/_iadb.asp"
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(302, headers={"Location": target})
    )
    respx.get(target).mock(return_value=httpx.Response(200, text=IADB_BODY))
    returned = source.fetch_boe_iadb(["IUDBEDR"], START, END)
    assert dict(returned["IUDBEDR"])[IADB_LAST[0]] == IADB_LAST[1]


@respx.mock
def test_an_html_page_raises_rather_than_being_read_as_data(
    source: CurvesSource,
) -> None:
    """An unknown code answers 200 with a web page, so the status proves nothing."""
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(
            200, text='<!DOCTYPE html>\n<html lang="en" class="no-js">\n<head>'
        )
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_boe_iadb(["NOTACODE"], START, END)
    assert "DATE" in str(excinfo.value)


@respx.mock
def test_a_body_that_clears_the_decode_gate_is_still_checked(
    source: CurvesSource,
) -> None:
    """``_decode`` lets anything JSON-shaped through, because that is the BoC's.

    So the IADB parser cannot rely on the gate upstream having done its
    checking, and has to look at its own header. An HTML page is refused
    earlier and never reaches here, which is why that case cannot exercise
    this branch.
    """
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text='{"observations": []}')
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_boe_iadb(["IUDBEDR"], START, END)
    assert BOE_IADB_HEADER_START in str(excinfo.value)


@respx.mock
def test_an_html_page_never_yields_an_empty_series(source: CurvesSource) -> None:
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text="<html><body>nope</body></html>")
    )
    with pytest.raises(SourceError):
        result = source.fetch_boe_iadb(["NOTACODE"], START, END)
        assert result != {}


@respx.mock
def test_a_blank_iadb_reading_is_dropped_not_zeroed(source: CurvesSource) -> None:
    body = "DATE,IUDBEDR\n01 Sep 2026,3.75\n02 Sep 2026,\n"
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text=body)
    )
    returned = dict(source.fetch_boe_iadb(["IUDBEDR"], START, END)["IUDBEDR"])
    assert date(2026, 9, 2) not in returned
    assert 0.0 not in returned.values()


# ---------------------------------------------------------------------------
# fetch_boe_curve: the workbook inside the archive
# ---------------------------------------------------------------------------


@respx.mock
def test_the_two_year_point_comes_off_the_nominal_sheet(
    source: CurvesSource,
) -> None:
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    returned = dict(source.fetch_boe_curve(2.0, START, END))
    assert returned[CURVE_FIRST[0]] == pytest.approx(CURVE_FIRST[1])


@respx.mock
def test_the_column_is_found_by_header_not_by_position(
    source: CurvesSource,
) -> None:
    """The maturity grid has been re-cut before, most recently out to 40 years.

    The real header holds ``1.999999920000001`` for the two-year point, so an
    equality match finds nothing and a fixed column letter finds the wrong
    tenor. Asking for a neighbouring maturity must return a different number.
    """
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    two_year = dict(source.fetch_boe_curve(2.0, START, END))
    shorter = dict(source.fetch_boe_curve(1.5, START, END))
    assert two_year[CURVE_FIRST[0]] != shorter[CURVE_FIRST[0]]


@respx.mock
def test_a_maturity_outside_the_grid_raises(source: CurvesSource) -> None:
    """Silently returning the nearest 40-year point for a 2-year ask is the trap."""
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_boe_curve(30.0, START, END)
    assert "30.0" in str(excinfo.value)


@respx.mock
def test_a_missing_member_raises(source: CurvesSource) -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("something else.xlsx", b"not the member")
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=buffer.getvalue())
    )
    with pytest.raises(SourceError) as excinfo:
        source.fetch_boe_curve(2.0, START, END)
    assert BOE_GLC_MEMBER in str(excinfo.value)


@respx.mock
def test_a_body_that_is_not_an_archive_raises(source: CurvesSource) -> None:
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=b"<html>maintenance</html>")
    )
    with pytest.raises(SourceError):
        source.fetch_boe_curve(2.0, START, END)


@pytest.mark.parametrize(
    ("serial", "expected"),
    [(46266, date(2026, 9, 1)), (1, date(1899, 12, 31)), (46269, date(2026, 9, 4))],
)
def test_an_excel_serial_converts_through_the_epoch(
    source: CurvesSource, serial: int, expected: date
) -> None:
    """Recomputed by hand: the epoch is 1899-12-30, so serial 1 is 1899-12-31."""
    assert source._excel_serial_to_date(serial) == expected
    assert BOE_GLC_DATE_EPOCH + dt.timedelta(days=serial) == expected


@respx.mock
def test_the_sheet_named_in_the_constant_is_the_one_read(
    source: CurvesSource,
) -> None:
    assert BOE_GLC_SHEET == "3. spot, short end"
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    assert source.fetch_boe_curve(2.0, START, END)


# ---------------------------------------------------------------------------
# fetch_snb, and the frozen cube
# ---------------------------------------------------------------------------


@respx.mock
def test_the_snb_cube_parses_past_its_metadata_lines(
    source: CurvesSource,
) -> None:
    """Two metadata lines, a blank, then a semicolon-delimited header."""
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    returned = dict(
        source.fetch_snb(
            SNB_BOND_CUBE, SNB_TENOR_2Y, date(2025, 1, 1), date(2025, 12, 31)
        )
    )
    assert returned[SNB_LAST[0]] == SNB_LAST[1]


@respx.mock
def test_the_swiss_two_year_is_negative_and_stays_negative(
    source: CurvesSource,
) -> None:
    """Swiss front-end yields are below zero. A sign dropped here is a real number."""
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    returned = dict(
        source.fetch_snb(
            SNB_BOND_CUBE, SNB_TENOR_2Y, date(2025, 1, 1), date(2025, 12, 31)
        )
    )
    assert returned[SNB_LAST[0]] < 0
    assert returned[SNB_LAST[0]] == -0.083


@respx.mock
def test_only_the_requested_tenor_is_returned(source: CurvesSource) -> None:
    """The cube carries every tenor and every rating class in one file.

    The capture holds twenty-two dimension values for the two sessions it
    keeps, so a parser that ignores the tenor returns a mixture of maturities
    and credit ratings under one label.
    """
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    returned = source.fetch_snb(
        SNB_BOND_CUBE, SNB_TENOR_2Y, date(1980, 1, 1), date(2025, 12, 31)
    )
    assert len(returned) == 2
    assert dict(returned) == {date(2025, 7, 30): -0.118, date(2025, 7, 31): -0.083}


def test_the_snb_capture_carries_more_than_one_tenor() -> None:
    """Guards the fixture: a single-tenor body cannot test a tenor filter."""
    rows = SNB_BODY.decode("utf-8-sig").splitlines()[4:]
    tenors = {row.split(";")[1].strip('"') for row in rows if row.count(";") >= 2}
    assert len(tenors) > 5
    assert "2J" in tenors


@respx.mock
def test_a_cube_with_no_header_raises(source: CurvesSource) -> None:
    """Two metadata lines and a blank precede the header, so it is found by name."""
    body = '"CubeId";"rendoblid"\n"PublishingDate";"2025-09-01 14:29"\n\n'
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(SourceError) as excinfo:
        source.fetch_snb(SNB_BOND_CUBE, SNB_TENOR_2Y, date(1980, 1, 1), END)
    assert SNB_BOND_CUBE in str(excinfo.value)


@respx.mock
def test_a_blank_snb_value_is_dropped_not_zeroed(source: CurvesSource) -> None:
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    returned = dict(
        source.fetch_snb(
            SNB_BOND_CUBE, SNB_TENOR_2Y, date(1980, 1, 1), date(2025, 12, 31)
        )
    )
    assert date(1988, 1, 1) not in returned
    assert 0.0 not in returned.values()


# ---------------------------------------------------------------------------
# The Swiss franc stays on the manual route, and provider_health says why
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_emits_no_swiss_two_year(source: CurvesSource) -> None:
    """The registry routes CHF yield_2y to manual, and a frozen 2025 number
    competing with an operator's entry is the whole reason it does."""
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    emitted = source.fetch(["yield_2y"], ["CHF"], START, END)
    assert emitted == []


@respx.mock
def test_provider_health_reports_the_frozen_swiss_date(
    source: CurvesSource,
) -> None:
    """Frozen is a date, not an absence: it tells the SNB stopping apart from us."""
    _mof_routes()
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text=IADB_BODY)
    )
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    respx.route().mock(return_value=httpx.Response(503, text="down"))
    health = source.provider_health()
    assert health["snb"] == SNB_LAST[0]
    assert health["snb"] is not None


@respx.mock
def test_provider_health_reports_none_for_a_provider_that_failed(
    source: CurvesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None means unreachable. A frozen provider reports its date instead."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    respx.route().mock(return_value=httpx.Response(503, text="down"))
    health = source.provider_health()
    assert health["snb"] is None
    assert set(health) >= {"ecb", "boc", "rba", "mof_jp", "boe", "snb"}


@respx.mock
def test_provider_health_never_raises(
    source: CurvesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is the tool for diagnosing an outage, so it must survive one."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    respx.route().mock(side_effect=httpx.ConnectError("no route to host"))
    health = source.provider_health()
    assert all(value is None for value in health.values())


# ---------------------------------------------------------------------------
# The documentation correction the ninth criterion asks for
# ---------------------------------------------------------------------------


def test_the_docs_do_not_claim_openpyxl_is_absent() -> None:
    """`openpyxl` is a declared dependency and this module now reads a workbook.

    The first version of this test asserted the absence of a sentence nobody
    had written, so it passed against the stale paragraph it was meant to
    catch. It now asserts the exact claim the document actually made.
    """
    text = (Path(__file__).parents[1] / "docs" / "data-sources.md").read_text()
    assert "`openpyxl` is not a project dependency" not in text
    assert "`openpyxl` is a project dependency" in text


def test_openpyxl_really_is_a_declared_dependency() -> None:
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert "openpyxl" in text


def test_the_fixture_provenance_is_recorded() -> None:
    readme = (FIXTURES / "README.md").read_text()
    for name in (
        "jgbcme.csv",
        "jgbcme_all.csv",
        "boe_iadb_bank_rate.csv",
        "boe_yield_curve.zip",
        "snb_rendoblid.csv",
    ):
        assert name in readme


# ---------------------------------------------------------------------------
# dispatch: the seam between a registry series_id and each fetcher's arguments
# ---------------------------------------------------------------------------


def _serve_every_provider() -> None:
    """Mock all seven institutions from the committed fixtures.

    The direct tests above call each fetcher with hand-typed arguments of the
    right type. Only a request that starts from the registry and goes through
    `fetch` exercises the dispatch, which is where #212 lived: the BoE
    fetchers were handed a series id they do not take, and nothing here or
    in ``tests/test_curves.py`` went through that seam for GBP.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "ecb_2y_spot.csv").read_text()
        )
    )
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "boc_2y_yield.json").read_text()
        )
    )
    respx.get(RBA_F2_URL).mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "rba_f2_2y.csv").read_text(encoding="utf-8-sig")
        )
    )
    respx.get(MOF_JP_CURRENT_URL).mock(
        return_value=httpx.Response(200, content=JGB_CURRENT)
    )
    respx.get(MOF_JP_HISTORY_URL).mock(
        return_value=httpx.Response(200, content=JGB_HISTORY)
    )
    respx.get(BOE_YIELD_CURVE_ZIP).mock(
        return_value=httpx.Response(200, content=YIELD_CURVE_ZIP)
    )
    respx.get(url__startswith=BOE_IADB_URL).mock(
        return_value=httpx.Response(200, text=IADB_BODY)
    )
    respx.get(RBNZ_B2_URL).mock(
        return_value=httpx.Response(
            200, content=(FIXTURES / "rbnz_hb2_daily_close.xlsx").read_bytes()
        )
    )


@respx.mock
def test_fetch_routes_the_gbp_two_year_to_the_curve_workbook(
    source: CurvesSource,
) -> None:
    """Through `fetch`, not `fetch_boe_curve`: the registry's series id is
    ``GLC_NOMINAL_SPOT_SHORT/2.0`` and the maturity has to come out of it."""
    _serve_every_provider()

    emitted = source.fetch(["yield_2y"], ["GBP"], START, END)

    by_period = {o.period: o for o in emitted}
    assert by_period[CURVE_FIRST[0]].value == pytest.approx(CURVE_FIRST[1])
    assert {o.source for o in emitted} == {"boe"}
    assert {o.series_id for o in emitted} == {"GLC_NOMINAL_SPOT_SHORT/2.0"}


@respx.mock
def test_fetch_routes_the_gbp_policy_rate_to_the_iadb(
    source: CurvesSource,
) -> None:
    """Bank Rate and the two-year share a provider key and nothing else. Before
    #212 both went to the workbook reader, so the Bank Rate request failed on a
    maturity that does not exist."""
    _serve_every_provider()

    emitted = source.fetch(["policy_rate"], ["GBP"], START, END)

    by_period = {o.period: o for o in emitted}
    assert by_period[IADB_LAST[0]].value == IADB_LAST[1]
    assert {o.series_id for o in emitted} == {"IUDBEDR"}


@respx.mock
def test_every_registry_curve_ref_round_trips_through_fetch(
    source: CurvesSource,
) -> None:
    """A ref whose series id does not fit its fetcher must fail here, in the
    suite, rather than in the morning refresh where it costs every yield."""
    _serve_every_provider()

    for (indicator, currency), ref in sorted(source.refs().items()):
        emitted = source.fetch([indicator], [currency], WIDE_START, END)
        if ref.transform in SERVED_TRANSFORMS:
            assert emitted, f"{indicator} {currency} via {ref.source} returned nothing"
            assert {o.source for o in emitted} == {ref.source}
        else:
            assert emitted == [], f"{indicator} {currency} is not served yet"


@respx.mock
def test_an_snb_ref_names_cube_and_tenor_in_its_series_id(
    source: CurvesSource,
) -> None:
    """Nothing routes to the SNB today, so the rule is tested on a hand-built
    ref: the dispatcher splits ``cube/tenor`` the way it splits the BoE's
    ``prefix/maturity``, and a ref that does not fit is refused by name."""
    respx.get(SNB_URL).mock(return_value=httpx.Response(200, content=SNB_BODY))
    ref = registry.SeriesRef(
        source="snb",
        series_id=f"{SNB_BOND_CUBE}/{SNB_TENOR_2Y}",
        unit="percent",
        frequency=registry.Frequency.DAILY,
    )

    returned = dict(
        source._from_provider(ref, "CHF", date(2025, 1, 1), date(2025, 12, 31))
    )
    assert returned[SNB_LAST[0]] == SNB_LAST[1]

    with pytest.raises(SourceError, match="snb"):
        source._from_provider(
            registry.SeriesRef(
                source="snb",
                series_id=SNB_BOND_CUBE,
                unit="percent",
                frequency=registry.Frequency.DAILY,
            ),
            "CHF",
            date(2025, 1, 1),
            date(2025, 12, 31),
        )


def test_a_boe_curve_id_without_a_numeric_maturity_is_refused_by_name(
    source: CurvesSource,
) -> None:
    """A maturity guessed off a malformed id would select a different tenor
    and present it as the two-year, so the dispatcher raises instead."""
    ref = registry.SeriesRef(
        source="boe",
        series_id="GLC_NOMINAL_SPOT_SHORT/two",
        unit="percent",
        frequency=registry.Frequency.DAILY,
    )

    with pytest.raises(SourceError, match="GLC_NOMINAL_SPOT_SHORT/two"):
        source._from_provider(ref, "GBP", START, END)
