"""Tests for the Stooq half of `PricesSource`.

Stooq answers a blocked request with HTTP 200 and an HTML proof-of-work
challenge, so the failure this file exists to prevent is the parser reading that
page as a session history with no rows. Empty and blocked are different facts:
empty reads downstream as a currency with no equity data, which is a coverage
gap an operator lives with, and blocked is a source that needs fixing.

The registry routes nothing to ``stooq`` today, so most of what is asserted here
is what happens when nothing is routed, plus one test that injects a ref to
prove the emitted observation would be built correctly the day one is.

No test reaches the network. Every response is mocked with `respx` from a
committed fixture.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import registry
from fbe.datasources.base import SourceError
from fbe.datasources.prices import (
    STOOQ_CSV_URL,
    STOOQ_SYMBOLS,
    PricesSource,
)
from fbe.datasources.registry import SeriesRef
from fbe.types import Frequency

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CSV_BODY = (FIXTURES / "stooq_spx_daily.csv").read_text()
CHALLENGE_BODY = (FIXTURES / "stooq_challenge.html").read_text()

HEADER = "Date,Open,High,Low,Close,Volume"

START = date(2026, 1, 1)
END = date(2026, 1, 31)


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache")


@pytest.fixture
def source(data_config: DataConfig) -> PricesSource:
    return PricesSource(data_config)


# --- the request ------------------------------------------------------------


@respx.mock
def test_the_composed_url_is_exactly_the_documented_shape(
    source: PricesSource,
) -> None:
    """Asserted as a whole URL rather than parameter by parameter, because the
    endpoint takes its dates in a form nothing else here uses and a silently
    reformatted one returns a different window rather than an error."""
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    source.fetch_stooq("^spx", START, END)

    assert str(route.calls[0].request.url) == (
        "https://stooq.com/q/d/l/?s=%5Espx&d1=20260101&d2=20260131&i=d"
    )


@respx.mock
def test_the_interval_is_passed_through(source: PricesSource) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    source.fetch_stooq("^spx", START, END, interval="w")

    assert route.calls[0].request.url.params["i"] == "w"


@respx.mock
def test_the_dates_are_yyyymmdd_with_no_separators(source: PricesSource) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    source.fetch_stooq("^spx", date(2026, 3, 7), date(2026, 11, 12))

    params = route.calls[0].request.url.params
    assert params["d1"] == "20260307"
    assert params["d2"] == "20261112"


# --- parsing ----------------------------------------------------------------


@respx.mock
def test_a_csv_body_parses_into_session_close_pairs(source: PricesSource) -> None:
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CSV_BODY))

    rows = source.fetch_stooq("^spx", START, END)

    assert list(rows) == [
        (date(2026, 1, 2), 4756.50),
        (date(2026, 1, 5), 4770.18),
        (date(2026, 1, 6), 4783.35),
    ]


@respx.mock
def test_rows_come_back_oldest_first(source: PricesSource) -> None:
    """Served newest first on purpose.

    The committed fixture is already ascending, so asserting against it proves
    the input was sorted and not that the code sorts. Callers difference these
    values, so a series left in the served order would flip the sign of every
    change it feeds.
    """
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                f"{HEADER}\n"
                "2026-01-06,1,2,3,4783.35,10\n"
                "2026-01-05,1,2,3,4770.18,10\n"
                "2026-01-02,1,2,3,4756.50,10\n"
            ),
        )
    )

    rows = list(source.fetch_stooq("^spx", START, END))

    assert [session for session, _ in rows] == [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]
    assert rows[0][1] == 4756.50


@respx.mock
def test_the_close_column_is_the_one_read(source: PricesSource) -> None:
    """Open, high and low are all present and all plausible. Reading the wrong
    column produces a number nobody would question."""
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CSV_BODY))

    rows = dict(source.fetch_stooq("^spx", START, END))

    assert rows[date(2026, 1, 2)] == 4756.50
    assert rows[date(2026, 1, 2)] != 4742.83
    assert rows[date(2026, 1, 2)] != 4764.54
    assert rows[date(2026, 1, 2)] != 4730.11


@respx.mock
def test_a_header_only_body_is_an_empty_series_not_an_error(
    source: PricesSource,
) -> None:
    """A window with no sessions in it is data. Only a body that is not the
    documented CSV at all is a failure."""
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text="Date,Open,High,Low,Close,Volume\n")
    )

    assert list(source.fetch_stooq("^spx", START, END)) == []


@respx.mock
def test_a_crlf_body_is_read_as_data(source: PricesSource) -> None:
    """CSV downloads commonly arrive CRLF. Without the trim on the header line
    a genuine body is refused as a challenge page."""
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200, text=f"{HEADER}\r\n2026-01-02,1,2,3,4756.50,10\r\n"
        )
    )

    assert list(source.fetch_stooq("^spx", START, END)) == [(date(2026, 1, 2), 4756.50)]


@respx.mock
def test_a_header_line_with_stray_padding_is_read_as_data(
    source: PricesSource,
) -> None:
    """`splitlines` already removes a CRLF terminator, so the trim on the
    header is not what makes a CRLF body work. What it covers is padding around
    the header itself, which is why it is pinned separately rather than assumed
    to be exercised by the line-ending case above."""
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200, text=f"  {HEADER}  \n2026-01-02,1,2,3,4756.50,10\n"
        )
    )

    assert list(source.fetch_stooq("^spx", START, END)) == [(date(2026, 1, 2), 4756.50)]


@respx.mock
def test_a_byte_order_mark_is_read_as_data(source: PricesSource) -> None:
    """A BOM is not whitespace, so trimming does not remove it and a plain
    utf-8 decode leaves it sitting in front of the header."""
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200,
            content=f"{HEADER}\n2026-01-02,1,2,3,4756.50,10\n".encode("utf-8-sig"),
        )
    )

    assert list(source.fetch_stooq("^spx", START, END)) == [(date(2026, 1, 2), 4756.50)]


# --- the challenge page -----------------------------------------------------


@respx.mock
def test_the_challenge_page_raises_rather_than_reading_as_no_rows(
    source: PricesSource,
) -> None:
    """The defect this module exists to prevent. Served at HTTP 200, so the
    shared request path sees a success and hands the body straight over."""
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CHALLENGE_BODY)
    )

    with pytest.raises(SourceError) as raised:
        source.fetch_stooq("^spx", START, END)

    # Without these the test passes against a source that was never contacted:
    # a dead network raises SourceError from the request path too.
    assert route.call_count == 1
    assert HEADER in str(raised.value)


@respx.mock
def test_the_challenge_error_names_the_symbol(source: PricesSource) -> None:
    """An operator reading a log needs to know which symbol was refused, since
    the fix is per symbol and starts with fetching it in a browser."""
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CHALLENGE_BODY)
    )

    with pytest.raises(SourceError) as raised:
        source.fetch_stooq("^dax", START, END)

    assert route.call_count == 1
    assert "^dax" in str(raised.value)
    assert HEADER in str(raised.value)


@respx.mock
def test_the_challenge_body_never_yields_an_empty_sequence(
    source: PricesSource,
) -> None:
    """Stated separately from the raise, because returning an empty sequence is
    the specific wrong answer: it is indistinguishable from a currency that has
    no equity data, and the two need different responses from the operator."""
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CHALLENGE_BODY))

    try:
        result = source.fetch_stooq("^spx", START, END)
    except SourceError:
        return
    pytest.fail(f"the challenge page was read as data: {list(result)!r}")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Date,Open,High,Low,Close\n2026-01-02,1,2,3,4\n",
        "Symbol,Date,Close\nSPX,2026-01-02,4756.50\n",
        "No data\n",
        "<html><body>429 Too Many Requests</body></html>",
    ],
    ids=["empty", "short-header", "wrong-header", "prose", "html-error"],
)
@respx.mock
def test_any_body_that_is_not_the_documented_header_raises(
    source: PricesSource, body: str
) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(SourceError) as raised:
        source.fetch_stooq("^spx", START, END)

    assert route.call_count == 1
    assert HEADER in str(raised.value)


@respx.mock
@pytest.mark.parametrize(
    "token", ["nan", "NaN", "inf", "-inf", "Infinity"], ids=lambda t: t.lower()
)
def test_a_close_that_is_not_a_finite_number_raises(
    source: PricesSource, token: str
) -> None:
    """`float()` accepts every one of these without complaint.

    A nan reaching a cross-sectional pillar makes the mean and the standard
    deviation nan for the whole universe, so all eight currencies score nan and
    every comparison against a threshold reads False. An inf clips to the top
    of the band and hands one currency maximum conviction from nothing. Neither
    shows up as a coverage gap, which is what makes them worse than a gap.
    """
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200,
            text=f"{HEADER}\n2026-01-02,1.0,2.0,0.5,{token},10\n",
        )
    )

    with pytest.raises(SourceError):
        source.fetch_stooq("^spx", START, END)


@respx.mock
def test_a_duplicate_session_date_raises(source: PricesSource) -> None:
    """Two closes for one session is a concatenated or replayed body. Keeping
    both gives a pillar two contradictory values for one period and lets
    iteration order decide; keeping one is a guess about which."""
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200,
            text=(f"{HEADER}\n2026-01-02,1,2,3,100.0,10\n2026-01-02,1,2,3,200.0,10\n"),
        )
    )

    with pytest.raises(SourceError):
        source.fetch_stooq("^spx", START, END)


@respx.mock
def test_a_short_row_raises(source: PricesSource) -> None:
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=f"{HEADER}\n2026-01-02,1,2\n")
    )

    with pytest.raises(SourceError):
        source.fetch_stooq("^spx", START, END)


@respx.mock
def test_rows_outside_the_requested_window_are_not_returned(
    source: PricesSource,
) -> None:
    """The window goes out as d1 and d2 and is enforced again on the way back.

    This module's premise is that the endpoint cannot be relied on to return
    what was asked for. In Phase 6 `end` is the as-of date of a backtest bar,
    so a session after it is a price the model could not have had.
    """
    respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                f"{HEADER}\n"
                "2025-06-02,1,2,3,100.0,10\n"
                "2026-01-05,1,2,3,4770.18,10\n"
                "2026-02-20,1,2,3,999.0,10\n"
            ),
        )
    )

    rows = source.fetch_stooq("^spx", START, END)

    assert list(rows) == [(date(2026, 1, 5), 4770.18)]


@respx.mock
def test_a_row_parse_failure_is_not_left_in_the_cache(
    data_config: DataConfig,
) -> None:
    """The header alone passing decode would let a truncated body be cached and
    fail identically for the whole TTL with no network traffic, and offline
    expires nothing. The whole parse runs before the cache write."""
    route = respx.get(STOOQ_CSV_URL).mock(
        side_effect=[
            httpx.Response(200, text=f"{HEADER}\n2026-01-02,1,2,3,abc,10\n"),
            httpx.Response(200, text=CSV_BODY),
        ]
    )
    source = PricesSource(data_config)

    with pytest.raises(SourceError):
        source.fetch_stooq("^spx", START, END)

    assert list(source.fetch_stooq("^spx", START, END))
    assert route.call_count == 2


@respx.mock
def test_a_request_failure_is_not_diagnosed_as_the_challenge_page(
    data_config: DataConfig,
) -> None:
    """An operator told to open a browser and check a symbol, when the real
    cause is a cold offline cache, checks a symbol that works and concludes the
    wrong thing. A confident wrong diagnosis is worse than none."""
    offline = PricesSource(replace(data_config, offline=True))

    with pytest.raises(SourceError) as raised:
        offline.fetch_stooq("^spx", START, END)

    message = str(raised.value)
    assert "^spx" in message
    assert "offline" in message.lower()
    assert "browser" not in message.lower()
    assert "anti-bot" not in message.lower()


@respx.mock
def test_an_exhausted_retry_is_not_diagnosed_as_the_challenge_page(
    data_config: DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fbe.datasources.base.time.sleep", lambda seconds: None)
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SourceError) as raised:
        PricesSource(data_config).fetch_stooq("^spx", START, END)

    message = str(raised.value)
    assert "^spx" in message
    assert "503" in message
    assert "browser" not in message.lower()


@respx.mock
def test_the_challenge_page_is_still_diagnosed_as_the_challenge_page(
    source: PricesSource,
) -> None:
    """The one case the browser advice is for."""
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CHALLENGE_BODY))

    with pytest.raises(SourceError) as raised:
        source.fetch_stooq("^spx", START, END)

    assert "browser" in str(raised.value).lower()


# --- refs -------------------------------------------------------------------


def test_refs_is_derived_from_the_registry(source: PricesSource) -> None:
    """Equality with the registry-derived set, not merely emptiness, so the
    assertion still holds the day a ref is routed here."""
    expected = {
        (spec.key, currency): ref
        for spec in registry.INDICATORS.values()
        for currency, ref in spec.series.items()
        if ref.source == registry.SOURCE_STOOQ
    }

    assert dict(source.refs()) == expected


def test_the_registry_routes_nothing_here_today(source: PricesSource) -> None:
    """Recorded as a fact of the current registry rather than assumed. If this
    starts failing, a ref was routed to stooq and the tests below that assume
    zero requests need rereading."""
    assert dict(source.refs()) == {}


def test_refs_picks_up_a_ref_the_registry_routes_here(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry routes nothing to stooq today, so comparing against an
    empty expected set cannot tell a registry-derived `refs` from one that
    returns nothing whatever the registry says. This injects a routed ref and
    asserts it comes back, which is the behaviour the day one is added for
    real.
    """
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^dax",
        unit="index",
        frequency=Frequency.DAILY,
    )
    spec = registry.INDICATORS["equity_index"]
    monkeypatch.setitem(
        registry.INDICATORS,
        "equity_index",
        replace(spec, series={**spec.series, "EUR": ref}),
    )

    assert source.refs()[("equity_index", "EUR")] == ref


def test_the_source_name_matches_the_registry_constant(
    source: PricesSource,
) -> None:
    assert source.name == registry.SOURCE_STOOQ


# --- fetch ------------------------------------------------------------------


@respx.mock
def test_fetch_requests_nothing_when_the_registry_routes_nothing(
    source: PricesSource,
) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    observations = source.fetch(
        ["equity_index", "vol_index", "commodity_price"],
        ["USD", "EUR", "GLOBAL"],
        START,
        END,
    )

    assert list(observations) == []
    assert route.call_count == 0


@respx.mock
def test_fetch_does_not_fall_back_to_the_unverified_symbol_table(
    source: PricesSource,
) -> None:
    """`STOOQ_SYMBOLS` is eight unverified guesses. Fetching one because the
    registry did not ask for it is how an unverified number reaches a pillar."""
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    source.fetch(["equity_index"], list(STOOQ_SYMBOLS), START, END)

    assert route.call_count == 0


@respx.mock
def test_fetch_ignores_an_indicator_this_source_does_not_back(
    source: PricesSource,
) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    assert list(source.fetch(["cpi_yoy"], ["USD"], START, END)) == []
    assert route.call_count == 0


@respx.mock
def test_a_routed_ref_becomes_an_observation_built_from_the_ref(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a stooq-routed ref and check what comes out.

    The registry routes nothing here today, so this is the only test that can
    see an emitted observation. It asserts the four provenance fields come from
    the ref, which is what going through `BaseDataSource._observation` buys and
    what re-typing them per source would quietly get wrong.
    """
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^spx",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "USD"): ref}
    )
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CSV_BODY))

    observations = list(source.fetch(["equity_index"], ["USD"], START, END))

    assert len(observations) == 3
    first = observations[0]
    assert first.indicator == "equity_index"
    assert first.currency == "USD"
    assert first.source == registry.SOURCE_STOOQ
    assert first.series_id == "^spx"
    assert first.unit == "index"
    assert first.frequency is Frequency.DAILY
    assert first.period == date(2026, 1, 2)
    assert first.value == 4756.50


@respx.mock
def test_fetch_requests_the_symbol_from_the_ref_and_the_window_asked_for(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the outgoing request, not on the observation.

    `_observation` copies `series_id` from the ref, so an observation looks
    correct whatever symbol was actually fetched. A source that requested
    `STOOQ_SYMBOLS["USD"]` and labelled the result from the ref would carry
    provenance that does not describe the number in it. The ref below uses a
    symbol the table does not, so the two cannot be confused.
    """
    assert STOOQ_SYMBOLS["USD"] != "^oex"
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^oex",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "USD"): ref}
    )
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    source.fetch(["equity_index"], ["USD"], START, END)

    params = route.calls[0].request.url.params
    assert params["s"] == "^oex"
    assert params["d1"] == "20260101"
    assert params["d2"] == "20260131"


@respx.mock
def test_a_routed_ref_leaves_released_at_unset(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stooq publishes no release timestamp, and the session date is not one.
    Phase 6 needs to know the difference between absent and inferred."""
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^spx",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "USD"): ref}
    )
    respx.get(STOOQ_CSV_URL).mock(return_value=httpx.Response(200, text=CSV_BODY))

    for observation in source.fetch(["equity_index"], ["USD"], START, END):
        assert observation.released_at is None


@respx.mock
def test_a_routed_ref_whose_currency_was_not_asked_for_is_skipped(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^dax",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "EUR"): ref}
    )
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    assert list(source.fetch(["equity_index"], ["USD"], START, END)) == []
    assert route.call_count == 0


@respx.mock
def test_a_routed_ref_whose_indicator_was_not_asked_for_is_skipped(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collector fans one request out to every source, so a source that
    ignored the indicator filter would fetch for requests meant elsewhere."""
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^spx",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "USD"): ref}
    )
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    assert list(source.fetch(["cpi_yoy"], ["USD"], START, END)) == []
    assert route.call_count == 0


@respx.mock
def test_fetch_propagates_the_challenge_failure(
    source: PricesSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked source must not reach the collector as an empty result."""
    ref = SeriesRef(
        source=registry.SOURCE_STOOQ,
        series_id="^spx",
        unit="index",
        frequency=Frequency.DAILY,
    )
    monkeypatch.setattr(
        PricesSource, "refs", lambda self: {("equity_index", "USD"): ref}
    )
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CHALLENGE_BODY)
    )

    with pytest.raises(SourceError) as raised:
        source.fetch(["equity_index"], ["USD"], START, END)

    assert route.call_count == 1
    assert HEADER in str(raised.value)


# --- the shared request path ------------------------------------------------


@respx.mock
def test_a_second_identical_request_reads_the_cache(
    data_config: DataConfig,
) -> None:
    """Inherited from `BaseDataSource`, and worth pinning here because Stooq
    throttles by IP and a repeated fetch is what gets an address blocked."""
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )
    source = PricesSource(data_config)

    source.fetch_stooq("^spx", START, END)
    source.fetch_stooq("^spx", START, END)

    assert route.call_count == 1


@respx.mock
def test_an_offline_run_with_a_cold_cache_raises_and_makes_no_call(
    data_config: DataConfig,
) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )
    offline = PricesSource(replace(data_config, offline=True))

    with pytest.raises(SourceError):
        offline.fetch_stooq("^spx", START, END)

    assert route.call_count == 0


@respx.mock
def test_an_offline_run_serves_a_cached_body(data_config: DataConfig) -> None:
    route = respx.get(STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )
    PricesSource(data_config).fetch_stooq("^spx", START, END)

    offline = PricesSource(replace(data_config, offline=True))
    rows = offline.fetch_stooq("^spx", START, END)

    assert list(rows)[0] == (date(2026, 1, 2), 4756.50)
    assert route.call_count == 1


@respx.mock
def test_a_challenge_body_is_not_left_in_the_cache(
    data_config: DataConfig,
) -> None:
    """`BaseDataSource` decodes before it writes, so a body this source refuses
    is never stored. Without that, one blocked morning would serve the same
    refusal back for the whole TTL, and offline expires nothing."""
    route = respx.get(STOOQ_CSV_URL).mock(
        side_effect=[
            httpx.Response(200, text=CHALLENGE_BODY),
            httpx.Response(200, text=CSV_BODY),
        ]
    )
    source = PricesSource(data_config)

    with pytest.raises(SourceError):
        source.fetch_stooq("^spx", START, END)

    assert list(source.fetch_stooq("^spx", START, END))
    assert route.call_count == 2


# --- what stays unverified --------------------------------------------------


def test_the_symbol_table_is_still_documented_as_unverified() -> None:
    """The table is eight guesses that could not be confirmed from here.

    Read out of the module source with `ast` rather than at runtime, because
    Python discards a docstring that follows an assignment. Anchored to this
    table's own docstring and not to the word appearing anywhere in the module
    or in docs/data-sources.md, where "unverified" already occurs about other
    things: a test satisfied by those cannot notice this claim being dropped.
    """
    import ast

    import fbe.datasources.prices as prices

    tree = ast.parse(Path(prices.__file__).read_text())
    docstring = None
    for index, node in enumerate(tree.body):
        targets = getattr(node, "targets", []) or (
            [node.target] if hasattr(node, "target") else []
        )
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if "STOOQ_SYMBOLS" not in names:
            continue
        following = tree.body[index + 1]
        assert isinstance(following, ast.Expr)
        assert isinstance(following.value, ast.Constant)
        docstring = following.value.value

    assert docstring is not None, "STOOQ_SYMBOLS has lost its docstring"
    assert "UNVERIFIED" in docstring
    assert "Check each in a browser before trusting it" in docstring


def test_the_symbol_table_still_covers_the_eight_currencies() -> None:
    from fbe.universe import G10

    assert set(STOOQ_SYMBOLS) == set(G10)


# --- available: configuration, never connectivity ---------------------------


@respx.mock
def test_available_is_true_and_makes_no_request(source: PricesSource) -> None:
    """The endpoint needs no key, so nothing in the config can rule it out.

    The challenge page is deliberately not consulted here. Whether Stooq is
    serving CSV today is a question about connectivity, and it belongs to
    `probe_request`, which doctor reads, and to `fetch_stooq`, which raises
    on it. Answering it here would cost a request on every run.
    """
    route = respx.route().mock(return_value=httpx.Response(200, text=""))

    assert source.available() is True
    assert route.call_count == 0


@respx.mock
def test_offline_on_a_cold_cache_is_available_and_fetch_raises(
    data_config: DataConfig,
) -> None:
    """Offline with nothing cached is a failed fetch, not an unavailable source.

    Reporting it here would print "unavailable, not configured", which sends
    the operator to look for a credential that does not exist.
    """
    route = respx.route().mock(return_value=httpx.Response(200, text=""))
    offline = PricesSource(replace(data_config, offline=True))

    assert offline.available() is True
    with pytest.raises(SourceError, match="offline"):
        offline.fetch_stooq("eurusd", START, END)
    assert route.call_count == 0
