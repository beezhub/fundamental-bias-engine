"""Tests for `PricesSource.spot`.

This is the number a position is sized against in Phase 4 and the number a
trade balance is converted with in EXTERNAL, so the failures worth testing are
the first and third rows of the prime directive table: a pair built against
market convention and answered anyway, and a missing rate answered with
something plausible instead of nothing.

The quoting direction is the whole difficulty. FRED names each series by its
own convention, so ``DEXUSEU`` is dollars per euro while ``DEXJPUS`` is yen per
dollar, and ``FRED_SPOT_SERIES`` is already keyed the way the market writes the
pair. The two fixtures here sit two orders of magnitude apart for that reason:
an inversion applied to either one cannot satisfy both assertions.

No test reaches the network. Every response is mocked with `respx` from a
committed fixture, and the offline case asserts the route was never called.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources.base import SourceError
from fbe.datasources.fred import BASE_URL as FRED_BASE_URL
from fbe.datasources.fred import ENDPOINTS
from fbe.datasources.prices import (
    FRED_SPOT_SERIES,
    MAX_SPOT_STALENESS_DAYS,
    SPOT_LOOKBACK_DAYS,
    PricesSource,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OBSERVATIONS_URL = f"{FRED_BASE_URL}{ENDPOINTS['observations']}"

EURUSD_BODY = json.loads((FIXTURES / "fred_dexuseu_observations.json").read_text())
USDJPY_BODY = json.loads((FIXTURES / "fred_dexjpus_observations.json").read_text())

# The sessions the fixtures publish. 2026-09-10, a Thursday, is a mid-week hole
# rather than a weekend: it carries a "." marker in the euro series and is
# simply absent from the yen one, which are the two shapes a missing session
# arrives in.
LATEST = date(2026, 9, 11)
GAP = date(2026, 9, 10)

# Pinned so nothing here depends on the day it runs. The euro fixture's newest
# fixing is LATEST, so an as-of a few days later is inside the staleness bound.
TODAY = date(2026, 9, 14)


def _body(observations: list[dict[str, str]]) -> dict[str, object]:
    return {"observations": observations}


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache", fred_api_key="k" * 32)


@pytest.fixture
def source(data_config: DataConfig) -> Iterator[PricesSource]:
    built = PricesSource(data_config)
    yield built
    built.close()


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the day `spot` uses when no session is named.

    Without this, every ``on=None`` assertion would pass in the week it was
    written and start failing once the fixtures aged past
    `MAX_SPOT_STALENESS_DAYS`.
    """
    monkeypatch.setattr("fbe.datasources.prices._today", lambda: TODAY)


def _route(body: object) -> respx.Route:
    return respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(200, json=body))


# --- quoting direction ------------------------------------------------------


@respx.mock
def test_eurusd_returns_the_published_value_unchanged(source: PricesSource) -> None:
    """DEXUSEU is dollars per euro, which is what EURUSD means."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=LATEST) == 1.0791


@respx.mock
def test_usdjpy_returns_the_published_value_unchanged(source: PricesSource) -> None:
    """DEXJPUS is yen per dollar, the opposite direction to DEXUSEU, and
    USDJPY is what that means. Nothing is inverted between them."""
    _route(USDJPY_BODY)

    assert source.spot("USDJPY", on=LATEST) == 149.08


@respx.mock
def test_no_single_inversion_can_satisfy_both_pairs(source: PricesSource) -> None:
    """Stated as its own test because it is the assertion that discriminates.

    The two fixtures sit two orders of magnitude apart. A module that inverted
    one direction, or both, could match one of these and never both.
    """
    _route(EURUSD_BODY)
    eurusd = source.spot("EURUSD", on=LATEST)
    respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(200, json=USDJPY_BODY))
    usdjpy = source.spot("USDJPY", on=LATEST)

    assert eurusd is not None and usdjpy is not None
    # 1/1.0791 is 0.927, which is also under 2, so a bare upper bound here
    # would pass under a blanket inversion. The published value is asserted
    # instead.
    assert eurusd == 1.0791
    assert usdjpy == 149.08
    assert eurusd != pytest.approx(1 / 1.0791)
    assert usdjpy != pytest.approx(1 / 149.08)


@respx.mock
@pytest.mark.parametrize("pair", sorted(FRED_SPOT_SERIES))
def test_every_served_pair_returns_its_series_value_untouched(
    source: PricesSource, pair: str
) -> None:
    """One case per key of FRED_SPOT_SERIES, each asserting the rate comes back
    with no arithmetic applied to it."""
    published = 1.2345
    _route(
        _body([{"date": LATEST.isoformat(), "value": str(published)}]),
    )

    assert source.spot(pair, on=LATEST) == published


@respx.mock
@pytest.mark.parametrize("pair", sorted(FRED_SPOT_SERIES))
def test_every_served_pair_requests_the_series_its_key_names(
    source: PricesSource, pair: str
) -> None:
    """Mapped by key, never by series name.

    FRED's own naming is mixed, so a mapping built by reading the series ID
    would invert whichever half is quoted the other way round.
    """
    route = _route(_body([{"date": LATEST.isoformat(), "value": "1.0"}]))

    source.spot(pair, on=LATEST)

    assert route.calls[0].request.url.params["series_id"] == FRED_SPOT_SERIES[pair]


EXPECTED_SERIES = {
    "EURUSD": "DEXUSEU",
    "GBPUSD": "DEXUSUK",
    "AUDUSD": "DEXUSAL",
    "NZDUSD": "DEXUSNZ",
    "USDJPY": "DEXJPUS",
    "USDCHF": "DEXSZUS",
    "USDCAD": "DEXCAUS",
}
"""The mapping spelled out, so the parametrised tests are not both sides of the
same constant.

Only EURUSD and USDJPY have fixtures, so without this the other five have
nothing tying a key to a series ID: transposing USDCHF and USDCAD, or writing
GBPUSD against the inverted-convention name DEXUKUS, would leave the suite
green. That is row one of the prime directive table."""


def test_the_seven_pairs_map_to_the_series_they_are_named_for() -> None:
    assert dict(FRED_SPOT_SERIES) == EXPECTED_SERIES


def test_the_series_names_follow_freds_own_numerator_rule() -> None:
    """FRED writes ``DEX<A><B>`` for A per B, so the pair it serves is B then A.

    Checked as a rule rather than a list, because it is what makes a
    transposition visible: DEXJPUS is yen per dollar and therefore USDJPY, and
    a key that paired it with JPYUSD would be inverted at source.
    """
    codes = {
        "US": "USD",
        "EU": "EUR",
        "UK": "GBP",
        "AL": "AUD",
        "NZ": "NZD",
        "JP": "JPY",
        "SZ": "CHF",
        "CA": "CAD",
    }

    for pair, series_id in FRED_SPOT_SERIES.items():
        body = series_id.removeprefix("DEX")
        numerator, denominator = codes[body[:2]], codes[body[2:]]
        assert pair == f"{denominator}{numerator}", (
            f"{series_id} is {numerator} per {denominator}, so it serves "
            f"{denominator}{numerator}, not {pair}"
        )


# --- a pair with no source --------------------------------------------------


@respx.mock
def test_a_pair_with_no_source_raises_rather_than_returning_none(
    source: PricesSource,
) -> None:
    """None has one meaning here, "no fixing exists for that session". An
    unserved pair answered with None too would make a typo and a data gap
    indistinguishable at the call site."""
    route = _route(EURUSD_BODY)

    with pytest.raises(SourceError):
        source.spot("EURGBP", on=LATEST)

    assert route.call_count == 0


@respx.mock
def test_usdeur_is_refused_rather_than_answered(source: PricesSource) -> None:
    """A pair built against convention and silently answered is the first row
    of the prime directive table. USDEUR is not EURUSD spelled backwards."""
    route = _route(EURUSD_BODY)

    with pytest.raises(SourceError) as raised:
        source.spot("USDEUR", on=LATEST)

    assert "USDEUR" in str(raised.value)
    assert route.call_count == 0


@respx.mock
def test_the_refusal_lists_what_is_served(source: PricesSource) -> None:
    """So the caller can see the convention rather than guess at it."""
    route = _route(EURUSD_BODY)

    with pytest.raises(SourceError) as raised:
        source.spot("USDEUR", on=LATEST)

    message = str(raised.value)
    for pair in FRED_SPOT_SERIES:
        assert pair in message
    assert route.call_count == 0


@respx.mock
def test_a_malformed_pair_is_refused(source: PricesSource) -> None:
    """Including the lower-case spelling, which is a plausible typo and must
    not resolve to the pair it looks like."""
    route = _route(EURUSD_BODY)

    for pair in ("", "EUR", "eurusd", "EURUSDX"):
        with pytest.raises(SourceError):
            source.spot(pair, on=LATEST)

    assert route.call_count == 0


@respx.mock
def test_a_datetime_is_refused_rather_than_silently_never_matching(
    source: PricesSource,
) -> None:
    """datetime subclasses date, so this type-checks. It would then match no
    session and send a timestamp FRED answers with an opaque 400."""
    route = _route(EURUSD_BODY)

    with pytest.raises(SourceError):
        source.spot("EURUSD", on=datetime(2026, 9, 11, 12, 0))

    assert route.call_count == 0


@respx.mock
def test_a_missing_credential_is_named_rather_than_left_as_a_400(
    tmp_path: Path,
) -> None:
    """FRED answers a keyless request with a bare 400 naming no cause, and an
    operator reading that goes looking at the network."""
    route = _route(EURUSD_BODY)
    source = PricesSource(DataConfig(cache_dir=tmp_path / "cache"))

    with pytest.raises(SourceError) as raised:
        source.spot("EURUSD", on=LATEST)

    assert "FRED_API_KEY" in str(raised.value)
    assert route.call_count == 0


# --- a session with no fixing -----------------------------------------------


@respx.mock
def test_a_session_with_no_fixing_returns_none(source: PricesSource) -> None:
    """Not the previous session's rate. A stale rate presented as the
    session's own is how a position gets sized against a number nobody
    looked at."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=GAP) is None


@respx.mock
def test_the_previous_session_is_not_carried_forward(source: PricesSource) -> None:
    """Stated separately, because returning 1.0843 here would look entirely
    reasonable and be wrong."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=GAP) != 1.0843
    assert source.spot("EURUSD", on=date(2026, 9, 9)) == 1.0843


@respx.mock
def test_a_holiday_gap_in_the_yen_series_returns_none(source: PricesSource) -> None:
    """The yen fixture simply has no row for that date, rather than a "."
    marker, so this is the other shape a missing session arrives in."""
    _route(USDJPY_BODY)

    assert source.spot("USDJPY", on=GAP) is None


@respx.mock
def test_a_session_the_series_does_not_reach_returns_none(
    source: PricesSource,
) -> None:
    """Not a window test: respx matches on URL alone, so the fixture comes back
    whatever window was asked for. What this pins is that a date the body does
    not carry yields None rather than the nearest row to it."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=date(2026, 9, 1)) is None


@respx.mock
def test_a_future_session_returns_none(source: PricesSource) -> None:
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=date(2030, 1, 1)) is None


@respx.mock
def test_a_weekend_session_returns_none(source: PricesSource) -> None:
    """2026-09-12 is a Saturday. The market was shut, which is a fact about the
    day and exactly what None is for."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=date(2026, 9, 12)) is None


# --- the most recent fixing -------------------------------------------------


@respx.mock
def test_with_no_session_the_most_recent_fixing_comes_back(
    source: PricesSource,
) -> None:
    """Asserted by which session it came from, not only by its value, so a
    module returning the first row rather than the last cannot pass."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD") == 1.0791
    assert source.spot("EURUSD", on=LATEST) == 1.0791
    assert source.spot("EURUSD", on=date(2026, 9, 8)) == 1.0812


@respx.mock
def test_the_most_recent_fixing_is_the_latest_date_not_the_last_row(
    source: PricesSource,
) -> None:
    """FRED returns rows in ascending order, but that is its choice and not a
    promise this module should lean on."""
    _route(
        _body(
            [
                {"date": "2026-09-11", "value": "1.0791"},
                {"date": "2026-09-08", "value": "1.0812"},
                {"date": "2026-09-09", "value": "1.0843"},
            ]
        )
    )

    assert source.spot("EURUSD") == 1.0791


@respx.mock
def test_a_window_holding_no_fixing_returns_none(source: PricesSource) -> None:
    """A source that published nothing in the window has no fixing to give,
    which is the documented meaning of None."""
    _route(_body([]))

    assert source.spot("EURUSD") is None


# --- failures that are not a missing fixing ---------------------------------


@respx.mock
def test_an_offline_run_with_a_cold_cache_raises_rather_than_returning_none(
    data_config: DataConfig,
) -> None:
    """A cold cache is not the fact that no fixing exists for that session.
    Answering None would hand a caller a missing rate for a reason that has
    nothing to do with the market."""
    route = _route(EURUSD_BODY)
    offline = PricesSource(replace(data_config, offline=True))

    with pytest.raises(SourceError):
        offline.spot("EURUSD", on=LATEST)

    assert route.call_count == 0


@respx.mock
def test_an_offline_run_serves_a_cached_fixing(data_config: DataConfig) -> None:
    route = _route(EURUSD_BODY)
    PricesSource(data_config).spot("EURUSD", on=LATEST)

    offline = PricesSource(replace(data_config, offline=True))

    assert offline.spot("EURUSD", on=LATEST) == 1.0791
    assert route.call_count == 1


@respx.mock
def test_a_repeated_request_failure_raises_rather_than_missing_a_rate(
    data_config: DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead FRED is not a session without a fixing."""
    monkeypatch.setattr("fbe.datasources.base.time.sleep", lambda seconds: None)
    route = respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SourceError):
        PricesSource(data_config).spot("EURUSD", on=LATEST)

    assert route.call_count > 1


@respx.mock
def test_an_unreadable_body_raises_rather_than_missing_a_rate(
    source: PricesSource,
) -> None:
    _route({"nothing": "useful"})

    with pytest.raises(SourceError):
        source.spot("EURUSD", on=LATEST)


@respx.mock
def test_a_missing_value_marker_is_not_read_as_zero(source: PricesSource) -> None:
    """The euro fixture carries a "." row. Zero would be a rate, and a rate of
    zero sizes an infinite position."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=GAP) is None


# --- the shared FRED path ---------------------------------------------------


@respx.mock
def test_the_fixings_go_through_the_shared_fred_path(source: PricesSource) -> None:
    """Not a second HTTP client. One key, one throttle and one cache for FRED,
    as the module docstring requires."""
    route = _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)

    request = route.calls[0].request
    assert str(request.url).startswith(FRED_BASE_URL)
    assert request.url.params["file_type"] == "json"


@respx.mock
def test_the_request_asks_for_the_series_as_published(source: PricesSource) -> None:
    """No server-side arithmetic and no vintage.

    `fetch_series` will happily compute a year-on-year percent change or return
    an old vintage if asked. Either would be arithmetic applied to the rate,
    just on FRED's side of the wire, and neither is visible in the returned
    value.
    """
    route = _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)

    params = route.calls[0].request.url.params
    assert params["units"] == "lin"
    assert "realtime_start" not in params
    assert "realtime_end" not in params


@respx.mock
def test_the_requested_window_is_the_configured_lookback(
    source: PricesSource,
) -> None:
    """Pinned against the constant, not merely asserted to be non-empty.

    A one-day window satisfies "start before end" and is exactly what
    SPOT_LOOKBACK_DAYS exists to prevent, so a bound that loose leaves the
    figure untested.
    """
    route = _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)

    params = route.calls[0].request.url.params
    expected = (LATEST - timedelta(days=SPOT_LOOKBACK_DAYS)).isoformat()
    assert params["observation_start"] == expected


@respx.mock
def test_the_lookback_is_read_from_the_constant(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override it and the requested window has to move with it, or the 30 is a
    literal nobody reads."""
    monkeypatch.setattr("fbe.datasources.prices.SPOT_LOOKBACK_DAYS", 5)
    route = _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)

    params = route.calls[0].request.url.params
    assert params["observation_start"] == (LATEST - timedelta(days=5)).isoformat()


@respx.mock
def test_spot_goes_through_the_one_shared_fred_source(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted at construction, because asserting that `_fred()` memoises does
    not prove `spot` uses it: a `spot` building its own source per call would
    leave `_fred()` memoising perfectly and still open a client each time."""
    import fbe.datasources.prices as prices

    built: list[object] = []
    real = prices.FredSource

    def counting(config: DataConfig) -> object:
        built.append(config)
        return real(config)

    monkeypatch.setattr(prices, "FredSource", counting)
    _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)
    source.spot("EURUSD", on=date(2026, 9, 9))
    source.spot("USDJPY", on=LATEST)

    assert len(built) == 1


@respx.mock
def test_a_series_that_stopped_publishing_is_refused_not_served_as_current(
    source: PricesSource,
) -> None:
    """A FRED series can stop updating while still answering requests.

    Without a bound, the newest row in a thirty day window comes back as though
    it were today's rate. The caller sizing a position against it cannot tell:
    the return is a bare float with no date on it.
    """
    stale = TODAY - timedelta(days=MAX_SPOT_STALENESS_DAYS + 1)
    _route(_body([{"date": stale.isoformat(), "value": "1.0791"}]))

    with pytest.raises(SourceError) as raised:
        source.spot("EURUSD")

    assert stale.isoformat() in str(raised.value)


@respx.mock
def test_a_fixing_inside_the_staleness_bound_is_served(
    source: PricesSource,
) -> None:
    """A weekend or a holiday puts the newest fixing a few days back, and that
    is the market being shut rather than the series having stopped."""
    recent = TODAY - timedelta(days=MAX_SPOT_STALENESS_DAYS)
    _route(_body([{"date": recent.isoformat(), "value": "1.0791"}]))

    assert source.spot("EURUSD") == 1.0791


@respx.mock
def test_the_staleness_bound_is_read_from_the_constant(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fbe.datasources.prices.MAX_SPOT_STALENESS_DAYS", 1)
    two_days_back = TODAY - timedelta(days=2)
    _route(_body([{"date": two_days_back.isoformat(), "value": "1.0791"}]))

    with pytest.raises(SourceError):
        source.spot("EURUSD")


@respx.mock
def test_the_staleness_bound_does_not_apply_to_a_named_session(
    source: PricesSource,
) -> None:
    """Asking about a session in the past is a deliberate question about that
    day, not a request for the current rate, so age is not a defect there."""
    _route(EURUSD_BODY)

    assert source.spot("EURUSD", on=date(2026, 9, 8)) == 1.0812


@respx.mock
def test_one_fred_source_is_reused_across_calls(source: PricesSource) -> None:
    """The cache is on disk and therefore shared however many sources exist, so
    a cache assertion cannot see this. The throttle and the HTTP client are per
    instance, and a fresh source per call is the second client the issue says
    not to build: it would count separately against the same endpoint and open
    a new connection each time.
    """
    _route(EURUSD_BODY)

    source.spot("EURUSD", on=LATEST)
    first = source._fred()
    source.spot("EURUSD", on=date(2026, 9, 9))

    assert source._fred() is first


@respx.mock
def test_close_releases_the_fred_client_as_well_as_its_own(
    data_config: DataConfig,
) -> None:
    """`BaseDataSource.close` only knows about the client it opened itself.

    `spot` reads through a second `FredSource`, which opens a client of its
    own, so a `close` that only calls up the chain leaks a live connection pool
    for the life of the process. Nothing else in this file would notice: every
    other assertion is about a returned value.
    """
    _route(EURUSD_BODY)
    built = PricesSource(data_config)
    built.spot("EURUSD", on=LATEST)
    fred_client = built._fred()._client
    assert fred_client is not None
    assert not fred_client.is_closed

    built.close()

    assert fred_client.is_closed


@respx.mock
def test_close_is_safe_on_a_source_that_never_fetched(
    data_config: DataConfig,
) -> None:
    """The `close` override has to survive a source that opened no FRED client,
    which is every source the CLI builds and then finds nothing to ask."""
    PricesSource(data_config).close()


@respx.mock
def test_a_repeated_call_reads_the_cache(data_config: DataConfig) -> None:
    route = _route(EURUSD_BODY)
    source = PricesSource(data_config)

    source.spot("EURUSD", on=LATEST)
    source.spot("EURUSD", on=LATEST)

    assert route.call_count == 1


@respx.mock
def test_the_credential_does_not_reach_the_cache(data_config: DataConfig) -> None:
    _route(EURUSD_BODY)
    PricesSource(data_config).spot("EURUSD", on=LATEST)

    written = [path for path in data_config.cache_dir.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert data_config.fred_api_key is not None
        assert data_config.fred_api_key.encode() not in path.read_bytes()


@respx.mock
def test_the_requested_window_ends_at_the_session_asked_for(
    source: PricesSource,
) -> None:
    """So a caller asking for a past session is not served a window that runs
    past it, which in Phase 6 would be a rate the model could not have had."""
    route = _route(EURUSD_BODY)

    source.spot("EURUSD", on=date(2026, 9, 9))

    params = route.calls[0].request.url.params
    assert params["observation_end"] == "2026-09-09"
    assert params["observation_start"] < params["observation_end"]


# --- fixture provenance ------------------------------------------------------


def test_the_fixture_provenance_is_recorded() -> None:
    """A fixture nobody can trace is indistinguishable from one somebody typed.

    The README's own opening says so. Both spot bodies are named there with the
    series they stand in for and the day they were written, so a later reader
    can tell what would have to change for them to become real captures.
    """
    readme = (FIXTURES / "README.md").read_text()
    assert "fred_dexuseu_observations.json" in readme
    assert "fred_dexjpus_observations.json" in readme
    assert "DEXUSEU" in readme
    assert "DEXJPUS" in readme
    assert "2026-09-14" in readme


@pytest.mark.parametrize(
    ("filename", "series_id"),
    [
        ("fred_dexuseu_observations.json", "DEXUSEU"),
        ("fred_dexjpus_observations.json", "DEXJPUS"),
    ],
)
def test_the_fixtures_record_that_they_are_not_live_captures(
    filename: str, series_id: str
) -> None:
    """Guards the honesty of the two bodies every assertion in this file reads.

    Neither was recorded from FRED, so neither can catch a unit or scale change
    on FRED's side. This is meant to fail the day somebody drops a real capture
    in place of one, because at that point the caveat in the README and in this
    file's own docstring stops being true and should be deleted, not left to
    rot.
    """
    body = json.loads((FIXTURES / filename).read_text())
    assert "NOT a live capture" in body["_fixture_note"]
    assert body["_fixture_written_on"]
    assert body["_fixture_series_id"] == series_id


def test_the_fixtures_stand_in_for_the_series_the_mapping_names() -> None:
    """The pair is load-bearing: two series quoted in opposite directions.

    If a later edit points one fixture at a series quoted the same way as the
    other, an inversion bug would satisfy both assertions at once and the
    direction tests above would stop testing direction.
    """
    euro = json.loads((FIXTURES / "fred_dexuseu_observations.json").read_text())
    yen = json.loads((FIXTURES / "fred_dexjpus_observations.json").read_text())
    assert FRED_SPOT_SERIES["EURUSD"] == euro["_fixture_series_id"]
    assert FRED_SPOT_SERIES["USDJPY"] == yen["_fixture_series_id"]
