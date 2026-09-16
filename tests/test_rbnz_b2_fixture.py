"""What the RBNZ published for the New Zealand 2-year, pinned before it is used.

`yield_2y` was 6/8 and `docs/data-sources.md` called that gap the largest
single risk in the data layer. NZD was one of the two missing legs, and issue
#91 established why: the RBNZ blocks this project's egress, so no unattended run
can see inside the file, and for a long time nobody knew whether the series even
existed. The owner retrieved the workbook from their own connection.

This module exists so that fact does not have to be established twice. The
fixture is the evidence, and the assertions below are what ``fetch_rbnz``
must reproduce. Nothing here parses the workbook the way the
source layer eventually will; these read it directly, so they pin the file
rather than the implementation, and a parser written later can be checked
against them rather than against itself.

There is no `fbe` import in this module on purpose. These tests pin the
publication; `tests/test_curves_rbnz.py` checks the parser against it. Keeping
them apart means the parser is checked against the RBNZ and not against itself.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "rbnz_hb2_daily_close.xlsx"

DATA_SHEET = "Data"
SERIES_ID_ROW = 5
UNIT_ROW = 4
"""Row numbers, 1-based, as the RBNZ lays the sheet out. Row 1 is the group,
row 2 the tenor, row 3 notes, row 4 the unit and row 5 the series ID. Data
begins at row 6."""

TWO_YEAR_ID = "INM.DG102.NZZCF"
"""Secondary market government bond closing yields, 2 year.

The ID is what a parser must locate the column by. The workbook carries four
government bond tenors whose headers differ only in the word before "year", and
the 1 and 5 year columns sit either side of this one, so a parser that found the
column by position would read a plausible yield from the wrong tenor and nothing
downstream would notice."""

EXPECTED_UNIT = "%pa"
"""Percent per annum, which is already the registry's canonical `percent` for
`yield_2y`. No conversion, and no scale to get wrong."""

PUBLISHED_CLOSES: dict[date, float] = {
    date(2026, 9, 8): 3.59,
    date(2026, 9, 9): 3.62,
    date(2026, 9, 10): 3.69,
    date(2026, 9, 11): 3.93,
    date(2026, 9, 14): 3.91,
}
"""The five most recent sessions in the fixture, as published.

Taken from the full 450 KB workbook rather than typed, and verified to match it
for every overlapping date. A parser that reads the wrong column, applies a
scale, or reads the swap rate instead will not reproduce these."""

GAP_SESSION = date(2020, 5, 15)
"""A session the RBNZ published with no 2-year value.

286 of 2178 rows in the full file are blank in this column and they are not
scattered: 224 fall in 2020, the longest run is 255 consecutive sessions ending
2020-11-19, and there are none after 2021. That reads as a period with no bond
near the 2-year point rather than a feed failure.

It is kept in the fixture because it is the case most likely to be got wrong.
A blank here means the RBNZ published a session and had no yield to put in it,
which is absence. Reading it as 0.0 would hand the monetary pillar a rate of
zero on a day New Zealand had no 2-year, and the number would look entirely
plausible."""


@pytest.fixture
def rows() -> list[tuple[object, ...]]:
    book = openpyxl.load_workbook(FIXTURE, read_only=True, data_only=True)
    try:
        return list(book[DATA_SHEET].iter_rows(values_only=True))
    finally:
        book.close()


def _column(rows: list[tuple[object, ...]], series_id: str) -> int:
    ids = rows[SERIES_ID_ROW - 1]
    assert series_id in ids, f"{series_id!r} is not a column of this workbook"
    return ids.index(series_id)


def test_the_two_year_series_is_present(rows: list[tuple[object, ...]]) -> None:
    """The question #91 could not answer for itself: does this series exist."""
    assert TWO_YEAR_ID in rows[SERIES_ID_ROW - 1]


def test_the_series_is_published_in_percent_per_annum(
    rows: list[tuple[object, ...]],
) -> None:
    """The unit, checked rather than assumed, because it decides the conversion.

    A yield published in basis points would be a hundred times the registry's
    `percent` and would still look like a number a 2-year could take.
    """
    assert rows[UNIT_ROW - 1][_column(rows, TWO_YEAR_ID)] == EXPECTED_UNIT


def test_the_published_closes_are_what_the_rbnz_served(
    rows: list[tuple[object, ...]],
) -> None:
    """The values themselves, so a parser can be checked against the source."""
    column = _column(rows, TWO_YEAR_ID)
    served = {
        row[0].date(): row[column]
        for row in rows[SERIES_ID_ROW:]
        if isinstance(row[0], datetime) and row[column] is not None
    }
    assert served == PUBLISHED_CLOSES


def test_a_session_with_no_yield_is_blank_rather_than_zero(
    rows: list[tuple[object, ...]],
) -> None:
    """Absence and zero are different answers, and the file says absence."""
    column = _column(rows, TWO_YEAR_ID)
    blank = [
        row
        for row in rows[SERIES_ID_ROW:]
        if isinstance(row[0], datetime) and row[0].date() == GAP_SESSION
    ]
    assert len(blank) == 1, f"{GAP_SESSION} is not in the fixture"
    assert blank[0][column] is None
    assert blank[0][column] != 0.0


def test_the_neighbouring_tenors_would_be_read_by_a_positional_parser(
    rows: list[tuple[object, ...]],
) -> None:
    """Why the ID matters, stated as a test rather than as a comment.

    The 1 and 5 year columns sit either side of the 2 year and carry yields in
    the same unit and a similar range. Locating by position and being one out
    returns a number that passes every sanity check the engine applies.
    """
    column = _column(rows, TWO_YEAR_ID)
    ids = rows[SERIES_ID_ROW - 1]
    assert ids[column - 1] == "INM.DG101.NZZCF"
    assert ids[column + 1] == "INM.DG105.NZZCF"
    for neighbour in (column - 1, column + 1):
        assert rows[UNIT_ROW - 1][neighbour] == EXPECTED_UNIT


def test_the_fixture_provenance_is_recorded() -> None:
    """A fixture nobody can trace is indistinguishable from one somebody typed."""
    readme = (FIXTURE.parent / "README.md").read_text()
    assert FIXTURE.name in readme
    assert TWO_YEAR_ID in readme
