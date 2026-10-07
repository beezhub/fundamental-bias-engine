"""`EurostatSource` serves the euro-area employment level and its change (#354).

EMPLOYMENT needs both a change in employment and the level it is a change of,
and until this source the euro had neither, so the pillar was absent for it.
What this source can get quietly wrong:

* The changing-composition ``EA`` series steps by about 3.6 million at 2026-Q1
  when Bulgaria joined. Differenced, that is a hiring boom. The registry must
  name the fixed ``EA20``.
* A request that matches nothing answers HTTP 200 with an empty ``value``. Read
  as no rows, a live series is reported dead with no cause.
* A hole in the level must leave a hole in the change, not a change over two
  quarters.

No test reaches the network. Both bodies are live captures recorded in
``tests/fixtures/README.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES, registry
from fbe.datasources.base import SourceError, WindowTooNarrow
from fbe.datasources.eurostat import BASE_URL, EurostatSource

FIXTURES = Path(__file__).parent / "fixtures"
EA20_BODY = (FIXTURES / "eurostat_ea20_employment.json").read_text()
NO_MATCH_BODY = (FIXTURES / "eurostat_no_match.json").read_text()

START = date(2024, 1, 1)
END = date(2026, 10, 7)

# Read off the capture by hand rather than from the parser.
FIRST_LEVEL = (date(2024, 1, 1), 170429.5)
SECOND_LEVEL = (date(2024, 4, 1), 170734.97)
LAST_LEVEL = (date(2026, 4, 1), 172849.01)
BEFORE_LAST_LEVEL = (date(2026, 1, 1), 172639.7)


@pytest.fixture
def source(tmp_path: Path) -> Iterator[EurostatSource]:
    eurostat = EurostatSource(DataConfig(cache_dir=tmp_path / "cache"))
    yield eurostat
    eurostat.close()


def _route() -> respx.Route:
    return respx.get(url__startswith=BASE_URL)


# --- the registry -------------------------------------------------------------


def test_the_euro_s_employment_legs_route_here() -> None:
    change = registry.INDICATORS["employment_chg"].series["EUR"]
    level = registry.INDICATORS["employment_level"].series["EUR"]
    assert change.source == level.source == "eurostat"
    assert change.series_id == level.series_id
    assert (change.transform, level.transform) == ("diff", "level")
    assert change.fetchable and level.fetchable


def test_the_registry_names_the_fixed_composition() -> None:
    """``EA`` would difference Bulgaria's accession into a hiring boom."""
    ref = registry.INDICATORS["employment_chg"].series["EUR"]
    _dataset, filters = EurostatSource(DataConfig()).split_series_id(ref.series_id)
    assert filters["geo"] == "EA20"
    assert filters == {
        "geo": "EA20",
        "unit": "THS_PER",
        "na_item": "EMP_DC",
        "s_adj": "SCA",
    }


def test_the_source_is_in_every_run() -> None:
    assert EurostatSource in ALL_SOURCES


def test_every_currency_now_has_an_employment_level() -> None:
    assert len(registry.INDICATORS["employment_level"].series) == 8


# --- fetch --------------------------------------------------------------------


@respx.mock
def test_the_level_is_served_as_published(source: EurostatSource) -> None:
    _route().mock(return_value=httpx.Response(200, text=EA20_BODY))
    emitted = source.fetch(["employment_level"], ["EUR"], START, END)

    by_period = {o.period: o.value for o in emitted}
    assert len(emitted) == 10
    assert by_period[FIRST_LEVEL[0]] == FIRST_LEVEL[1]
    assert by_period[LAST_LEVEL[0]] == LAST_LEVEL[1]
    assert {o.unit for o in emitted} == {"thousands_of_persons"}


@respx.mock
def test_the_change_is_the_quarter_on_quarter_difference(
    source: EurostatSource,
) -> None:
    _route().mock(return_value=httpx.Response(200, text=EA20_BODY))
    emitted = source.fetch(["employment_chg"], ["EUR"], date(2024, 4, 1), END)

    by_period = {o.period: o.value for o in emitted}
    assert len(emitted) == 9
    assert by_period[SECOND_LEVEL[0]] == pytest.approx(SECOND_LEVEL[1] - FIRST_LEVEL[1])
    assert by_period[LAST_LEVEL[0]] == pytest.approx(
        LAST_LEVEL[1] - BEFORE_LAST_LEVEL[1]
    )
    assert FIRST_LEVEL[0] not in by_period


@respx.mock
def test_a_change_request_reaches_back_a_quarter(source: EurostatSource) -> None:
    """The first quarter in the window needs the one before it."""
    route = _route().mock(return_value=httpx.Response(200, text=EA20_BODY))
    source.fetch(["employment_chg"], ["EUR"], date(2024, 4, 1), END)

    params = route.calls.last.request.url.params
    assert params["sinceTimePeriod"] < "2024-Q2"
    assert params["untilTimePeriod"] == "2026-Q4"


@respx.mock
def test_the_request_is_the_registry_s_own(source: EurostatSource) -> None:
    route = _route().mock(return_value=httpx.Response(200, text=EA20_BODY))
    source.fetch(["employment_level"], ["EUR"], START, END)

    url = route.calls.last.request.url
    assert str(url).startswith(f"{BASE_URL}namq_10_pe?")
    for name, code in (
        ("geo", "EA20"),
        ("unit", "THS_PER"),
        ("na_item", "EMP_DC"),
        ("s_adj", "SCA"),
        ("sinceTimePeriod", "2024-Q1"),
    ):
        assert url.params[name] == code


@respx.mock
def test_a_missing_quarter_leaves_a_hole_in_the_change(
    source: EurostatSource,
) -> None:
    """Without the gap check, 2025-Q3 would carry two quarters of hiring."""
    body = json.loads(EA20_BODY)
    q2_2025 = body["dimension"]["time"]["category"]["index"]["2025-Q2"]
    del body["value"][str(q2_2025)]
    _route().mock(return_value=httpx.Response(200, json=body))

    emitted = source.fetch(["employment_chg"], ["EUR"], date(2024, 4, 1), END)

    periods = {o.period for o in emitted}
    assert date(2025, 4, 1) not in periods
    assert date(2025, 7, 1) not in periods
    assert date(2025, 10, 1) in periods


@respx.mock
def test_a_request_that_matches_nothing_is_an_error_naming_the_series(
    source: EurostatSource,
) -> None:
    """Eurostat answers 200 with an empty ``value`` for an unknown code."""
    _route().mock(return_value=httpx.Response(200, text=NO_MATCH_BODY))
    with pytest.raises(SourceError, match="namq_10_pe") as excinfo:
        source.fetch(["employment_level"], ["EUR"], START, END)
    assert not isinstance(excinfo.value, WindowTooNarrow)


@respx.mock
def test_an_empty_narrow_window_judges_nothing(source: EurostatSource) -> None:
    _route().mock(return_value=httpx.Response(200, text=NO_MATCH_BODY))
    with pytest.raises(WindowTooNarrow):
        source.fetch(["employment_level"], ["EUR"], date(2026, 9, 1), END)


@respx.mock
def test_a_body_of_several_series_is_refused(source: EurostatSource) -> None:
    """Filters that pick two areas would otherwise be read as one series."""
    body = json.loads(EA20_BODY)
    body["size"][body["id"].index("geo")] = 2
    _route().mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(SourceError, match="geo"):
        source.fetch(["employment_level"], ["EUR"], START, END)


def test_the_probe_accepts_the_capture_and_refuses_a_web_page(
    source: EurostatSource,
) -> None:
    probe = source.probe_request()
    assert probe.path == "namq_10_pe"
    assert probe.params["geo"] == "EA20"
    probe.verify(EA20_BODY.encode())
    with pytest.raises(SourceError):
        probe.verify(b"<html>maintenance</html>")


def test_the_fixture_provenance_is_recorded() -> None:
    readme = (FIXTURES / "README.md").read_text()
    assert "eurostat_ea20_employment.json" in readme
    assert "eurostat_no_match.json" in readme
