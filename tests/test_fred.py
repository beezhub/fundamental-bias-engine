"""Tests for the FRED source.

The properties worth testing here are the ones this source could quietly get
wrong on its own, rather than anything `BaseDataSource` already guarantees:
reading FRED's ``"."`` as a zero, letting a FRED series ID reach the indicator
field, asking the server for the wrong ``units`` and so silently scoring a
level as a growth rate, and stamping ``released_at`` with something that is not
a release date.

No test reaches the network. Every route is mocked with `respx`, every cache
lives under ``tmp_path``, and no test requires a key in the environment.
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
from fbe.datasources import registry
from fbe.datasources.base import SourceError
from fbe.datasources.fred import BASE_URL, MISSING_VALUE, UNITS, FredSource

KEY = "abcdef0123456789abcdef0123456789"
OBSERVATIONS_URL = f"{BASE_URL}series/observations"
SERIES_URL = f"{BASE_URL}series"
VINTAGE_URL = f"{BASE_URL}series/vintagedates"

START = date(2026, 6, 1)
END = date(2026, 6, 30)

FIXTURE = Path(__file__).parent / "fixtures" / "fred_dgs2_observations.json"


@pytest.fixture
def config(tmp_path: Path) -> DataConfig:
    """A configured, online source pointed at a cache under tmp_path."""
    return DataConfig(fred_api_key=KEY, cache_dir=tmp_path / "cache")


@pytest.fixture
def source(config: DataConfig) -> Iterator[FredSource]:
    fred = FredSource(config)
    yield fred
    fred.close()


def _observations(*rows: tuple[str, str]) -> dict[str, object]:
    """A series/observations body carrying the given (date, value) rows."""
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


def _fred_pairs() -> list[tuple[str, str]]:
    """Every (indicator, currency) the registry routes to FRED."""
    return [
        (key, currency)
        for key, spec in registry.INDICATORS.items()
        for currency, ref in spec.series.items()
        if ref.source == "fred"
    ]


# ---------------------------------------------------------------------------
# refs() and availability, which need no network at all
# ---------------------------------------------------------------------------


def test_refs_matches_the_registry_exactly(source: FredSource) -> None:
    """Compared against the registry, so a registry edit cannot drift silently."""
    assert dict(source.refs()) == {
        pair: registry.INDICATORS[pair[0]].series[pair[1]] for pair in _fred_pairs()
    }


def test_refs_is_not_empty_and_names_only_fred(source: FredSource) -> None:
    refs = source.refs()
    assert refs
    assert {ref.source for ref in refs.values()} == {"fred"}


def test_available_is_false_online_without_a_key(tmp_path: Path) -> None:
    fred = FredSource(DataConfig(fred_api_key=None, cache_dir=tmp_path / "c"))
    assert fred.available() is False


def test_available_is_true_online_with_a_key(source: FredSource) -> None:
    assert source.available() is True


def test_available_is_true_offline_without_a_key(tmp_path: Path) -> None:
    """A cached run needs no key, which is what makes it reproducible."""
    fred = FredSource(
        DataConfig(fred_api_key=None, offline=True, cache_dir=tmp_path / "c")
    )
    assert fred.available() is True


def test_available_reaches_no_network(source: FredSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        source.available()
        assert route.call_count == 0


# ---------------------------------------------------------------------------
# fetch: canonical keys, the missing marker, and the units switch
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_emits_canonical_keys_not_fred_vocabulary(source: FredSource) -> None:
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    assert [o.indicator for o in emitted] == ["yield_2y"]
    assert [o.series_id for o in emitted] == ["DGS2"]
    assert [o.source for o in emitted] == ["fred"]


@respx.mock
def test_fetch_skips_pairs_belonging_to_another_source(source: FredSource) -> None:
    """EUR yield_2y is the ECB's. Asking for it must not produce a request."""
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "1.95")))
    )
    emitted = source.fetch(["yield_2y"], ["EUR"], START, END)
    assert emitted == []
    assert route.call_count == 0


@respx.mock
def test_a_missing_value_row_yields_no_observation(source: FredSource) -> None:
    """FRED's null is the string ".". Read as a number it becomes a zero."""
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_observations(
                ("2026-06-29", "3.55"),
                ("2026-06-30", MISSING_VALUE),
            ),
        )
    )
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    periods = [o.period for o in emitted]
    assert date(2026, 6, 30) not in periods
    assert date(2026, 6, 29) in periods
    assert 0.0 not in [o.value for o in emitted]


@respx.mock
def test_a_yoy_ref_asks_the_server_for_pc1(source: FredSource) -> None:
    """The registry's yoy transform is FRED's pc1, computed server-side."""
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "2.9")))
    )
    source.fetch(["cpi_yoy"], ["USD"], START, END)
    assert route.calls.last.request.url.params["units"] == "pc1"


@respx.mock
def test_a_level_ref_asks_the_server_for_lin(source: FredSource) -> None:
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    source.fetch(["yield_2y"], ["USD"], START, END)
    assert route.calls.last.request.url.params["units"] == "lin"


@respx.mock
def test_the_units_mapping_is_read_rather_than_hardcoded(
    source: FredSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override the map and the request must follow it, or it is a literal."""
    monkeypatch.setitem(UNITS, "level", "log")
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    source.fetch(["yield_2y"], ["USD"], START, END)
    assert route.calls.last.request.url.params["units"] == "log"


@respx.mock
def test_the_period_window_is_passed_to_the_server(source: FredSource) -> None:
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    source.fetch(["yield_2y"], ["USD"], START, END)
    params = route.calls.last.request.url.params
    assert params["observation_start"] == START.isoformat()
    assert params["observation_end"] == END.isoformat()
    assert params["file_type"] == "json"


@respx.mock
def test_a_transform_fred_cannot_express_raises_rather_than_guessing(
    source: FredSource,
) -> None:
    """chg_1m has no FRED units equivalent. Guessing one scores a wrong number.

    The registry defines it as a month-end resample then a difference then a
    rescale into basis points. FRED's ``chg`` is the change from the previous
    observation, which on a daily series is a one-day change, so mapping the
    two would emit a number roughly thirty times too small under a label
    saying otherwise.
    """
    with pytest.raises(SourceError) as excinfo:
        source.fetch(["yield_2y_chg_1m"], ["USD"], START, END)
    message = str(excinfo.value)
    assert "chg_1m" in message
    assert "yield_2y_chg_1m" in message


@respx.mock
def test_the_unexpressable_transform_does_not_reach_the_network(
    source: FredSource,
) -> None:
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    with pytest.raises(SourceError):
        source.fetch(["yield_2y_chg_3m"], ["USD"], START, END)
    assert route.call_count == 0


@respx.mock
def test_unit_and_frequency_come_from_the_registry(source: FredSource) -> None:
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    ref = registry.INDICATORS["yield_2y"].series["USD"]
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    assert emitted[0].unit == ref.unit
    assert emitted[0].frequency == ref.frequency


# ---------------------------------------------------------------------------
# released_at, which is the field a backtest lives or dies on
# ---------------------------------------------------------------------------


@respx.mock
def test_released_at_is_none_and_never_the_period(source: FredSource) -> None:
    """The default realtime window makes realtime_start the query date.

    It is the date the request asked about, not the date the figure was
    published, so copying it onto released_at would stamp today onto a print
    from years ago. Left None, which is the marked absence.
    """
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    assert emitted[0].released_at is None


@respx.mock
def test_released_at_is_not_taken_from_realtime_start(source: FredSource) -> None:
    """A body whose realtime_start is a real date must still leave it None."""
    body = {
        "observations": [
            {
                "realtime_start": "2026-09-12",
                "realtime_end": "9999-12-31",
                "date": "2026-06-30",
                "value": "3.55",
            }
        ]
    }
    respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(200, json=body))
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    assert emitted[0].released_at is None
    assert emitted[0].period == date(2026, 6, 30)


# ---------------------------------------------------------------------------
# fetch_series and the ALFRED vintage switch
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_series_returns_period_value_pairs(source: FredSource) -> None:
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_observations(("2026-06-29", "3.55"), ("2026-06-30", "3.61")),
        )
    )
    assert source.fetch_series("DGS2", START, END) == [
        (date(2026, 6, 29), 3.55),
        (date(2026, 6, 30), 3.61),
    ]


@respx.mock
def test_fetch_series_drops_missing_rows(source: FredSource) -> None:
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_observations(("2026-06-29", MISSING_VALUE), ("2026-06-30", "3.61")),
        )
    )
    assert source.fetch_series("DGS2", START, END) == [(date(2026, 6, 30), 3.61)]


@respx.mock
def test_a_vintage_sets_both_realtime_bounds(source: FredSource) -> None:
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.10")))
    )
    vintage = date(2024, 3, 15)
    source.fetch_series("DGS2", START, END, vintage=vintage)
    params = route.calls.last.request.url.params
    assert params["realtime_start"] == vintage.isoformat()
    assert params["realtime_end"] == vintage.isoformat()


@respx.mock
def test_no_vintage_sends_no_realtime_bounds(source: FredSource) -> None:
    """Live scoring wants the latest vintage, which is FRED's default."""
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    source.fetch_series("DGS2", START, END)
    params = route.calls.last.request.url.params
    assert "realtime_start" not in params
    assert "realtime_end" not in params


@respx.mock
def test_the_vintage_call_returns_the_older_number(source: FredSource) -> None:
    """Two bodies, current and vintage. The revision is what a backtest must see."""

    def responder(request: httpx.Request) -> httpx.Response:
        if "realtime_start" in request.url.params:
            return httpx.Response(200, json=_observations(("2026-06-30", "3.10")))
        return httpx.Response(200, json=_observations(("2026-06-30", "3.55")))

    respx.get(OBSERVATIONS_URL).mock(side_effect=responder)
    current = source.fetch_series("DGS2", START, END)
    vintage = source.fetch_series("DGS2", START, END, vintage=date(2024, 3, 15))
    assert current == [(date(2026, 6, 30), 3.55)]
    assert vintage == [(date(2026, 6, 30), 3.10)]
    assert current != vintage


@respx.mock
def test_fetch_series_passes_the_units_it_was_given(source: FredSource) -> None:
    route = respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "2.9")))
    )
    source.fetch_series("CPIAUCSL", START, END, units="pc1")
    assert route.calls.last.request.url.params["units"] == "pc1"


@respx.mock
def test_an_unparseable_observations_body_raises(source: FredSource) -> None:
    """A body with no observations key is a failure, not an empty series."""
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json={"error_message": "Bad Request"})
    )
    with pytest.raises(SourceError):
        source.fetch_series("DGS2", START, END)


@respx.mock
def test_an_unreadable_value_raises_rather_than_being_skipped(
    source: FredSource,
) -> None:
    """A "." is a documented hole. Anything else unreadable is a shape change."""
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "n/a")))
    )
    with pytest.raises(SourceError):
        source.fetch_series("DGS2", START, END)


# ---------------------------------------------------------------------------
# vintage_dates and last_updated
# ---------------------------------------------------------------------------


@respx.mock
def test_vintage_dates_returns_ascending_dates(source: FredSource) -> None:
    respx.get(VINTAGE_URL).mock(
        return_value=httpx.Response(
            200, json={"vintage_dates": ["2024-03-15", "2023-01-02", "2025-07-31"]}
        )
    )
    assert source.vintage_dates("DGS2") == [
        date(2023, 1, 2),
        date(2024, 3, 15),
        date(2025, 7, 31),
    ]


@respx.mock
def test_vintage_dates_raises_on_an_unparseable_body(source: FredSource) -> None:
    """An empty sequence would read as 'never revised', which is a real answer."""
    respx.get(VINTAGE_URL).mock(
        return_value=httpx.Response(200, json={"error_message": "Bad Request"})
    )
    with pytest.raises(SourceError):
        source.vintage_dates("DGS2")


@respx.mock
def test_last_updated_returns_the_newest_observation_date(source: FredSource) -> None:
    respx.get(SERIES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"seriess": [{"id": "DGS2", "observation_end": "2026-09-10"}]},
        )
    )
    assert source.last_updated("DGS2") == date(2026, 9, 10)


@respx.mock
def test_last_updated_is_none_when_the_series_holds_nothing(
    source: FredSource,
) -> None:
    """None means 'holds no observations', which is different from an error."""
    respx.get(SERIES_URL).mock(
        return_value=httpx.Response(200, json={"seriess": [{"id": "DGS2"}]})
    )
    assert source.last_updated("DGS2") is None


@respx.mock
def test_last_updated_raises_when_the_series_does_not_exist(
    source: FredSource,
) -> None:
    """An unknown series is a broken registry entry, not an empty one."""
    respx.get(SERIES_URL).mock(return_value=httpx.Response(200, json={"seriess": []}))
    with pytest.raises(SourceError):
        source.last_updated("NOSUCHSERIES")


# ---------------------------------------------------------------------------
# The credential, and the spot check
# ---------------------------------------------------------------------------


@respx.mock
def test_the_key_is_sent_but_reaches_no_cache_file(
    source: FredSource, tmp_path: Path
) -> None:
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    source.fetch(["yield_2y"], ["USD"], START, END)
    assert respx.calls.last.request.url.params["api_key"] == KEY
    for path in (tmp_path / "cache").rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="ignore")
            assert KEY not in str(path)


@respx.mock
def test_no_key_configured_still_sends_no_api_key_parameter(tmp_path: Path) -> None:
    fred = FredSource(DataConfig(fred_api_key=None, cache_dir=tmp_path / "c"))
    respx.get(OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=_observations(("2026-06-30", "3.55")))
    )
    fred.fetch(["yield_2y"], ["USD"], START, END)
    assert "api_key" not in respx.calls.last.request.url.params
    fred.close()


@respx.mock
def test_the_spot_check_pins_value_and_unit_for_a_named_series(
    source: FredSource,
) -> None:
    """A committed body, so a parser change that moves a number breaks the build.

    The fixture is constructed rather than recorded: no FRED key was available
    where this was written. That limits what this catches. It catches a change
    on our side of the wire, which is what the rest of this file is about; it
    cannot catch a change on FRED's side, which is what a recorded body would
    add. The fixture header says so, and so does #56.
    """
    body = json.loads(FIXTURE.read_text())
    respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(200, json=body))
    emitted = source.fetch(["yield_2y"], ["USD"], START, END)
    by_period = {o.period: o for o in emitted}
    assert by_period[date(2026, 6, 2)].value == 3.61
    assert by_period[date(2026, 6, 2)].unit == "percent"
    assert date(2026, 6, 3) not in by_period
    assert len(emitted) == 4


def test_the_fixture_records_that_it_is_not_a_live_capture() -> None:
    """Guards the honesty of the file above, which is easy to quietly forget.

    This test is meant to fail the day somebody drops a recorded response in
    its place, because at that point the caveat on the spot check above stops
    being true and should be deleted rather than left to rot.
    """
    body = json.loads(FIXTURE.read_text())
    assert "NOT a live capture" in body["_fixture_note"]
    assert body["_fixture_written_on"]
