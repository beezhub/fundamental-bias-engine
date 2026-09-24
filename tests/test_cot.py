"""Tests for the CFTC source, where the trap is which column the number comes from.

Three things here can produce a plausible wrong number and none of them raises.

**The numerator.** TFF splits open interest into dealer, asset manager,
leveraged funds and other reportables. The ruling on #174 settled that
`cot_net_pct_oi` is leveraged funds alone, because that is the money whose
crowding mean-reverts, and ADR 0011 records it. Netting asset managers in as
well reconstructs the Legacy non-commercial bucket out of TFF's parts, which
pays for the TFF integration and then discards the only thing it buys. On the
committed capture the two readings differ on every currency and disagree in sign
on four, so the tests assert the columns rather than only the arithmetic: a test
that checks one row's division passes under either reading.

**The date.** Positions are snapped at Tuesday's close and published on Friday.
Stamping the Friday on `period` would make a ten-day-old number look three days
old, which is the standard way to misuse this dataset. `period` is the Tuesday
and `released_at` is the Friday, and the two differ by three days.

**The dollar.** There is no liquid dollar contract, so the dollar reading is the
sign-flipped net of the other seven. Each leg has to be normalised by its own
open interest before the seven are added: raw contract counts differ by more
than an order of magnitude between the euro and the New Zealand dollar, so a raw
sum is a euro reading wearing a dollar label.

Nothing here reaches the network. The live shape is pinned by one committed
capture, recorded in `tests/fixtures/README.md` with its capture date.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources.base import SourceError
from fbe.datasources.cot import (
    BASE_URL,
    CONTRACT_CODES,
    DATASETS,
    DEFAULT_DATASET,
    ROW_LIMIT,
    SNAPSHOT_WEEKDAYS,
    TFF_FIELDS,
    CotSource,
)
from fbe.datasources.registry import INDICATORS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIVE_ROWS = json.loads((FIXTURES / "cot_tff_futures_only_2026_09_08.json").read_text())
"""Seven rows, one per contract, captured live on 2026-09-21 for report date
2026-09-08. Every value is a string, as Socrata returns them."""

TUESDAY = date(2026, 9, 8)
FRIDAY = date(2026, 9, 11)
"""The report date and its publication date. Three days apart, by the CFTC's own
release schedule."""

START = date(2026, 1, 1)
END = date(2026, 12, 31)

PUBLISHED_PERCENTS = {
    "EUR": -3.531699884557925,
    "GBP": 10.868214231908803,
    "JPY": -9.826773544687622,
    "CHF": -8.745274363462451,
    "CAD": -16.55851233795515,
    "AUD": 10.92919809953718,
    "NZD": -13.772245947705155,
}
"""``(lev_money_long - lev_money_short) / open_interest_all * 100`` on the
committed capture, recomputed here from the published integers rather than read
from the source under test. The hundred is the scale the key is named for, and
these are written out rather than multiplied at the assertion so that a source
emitting the ratio fails on the value it emits."""

IMPLIED_USD = 30.63709374692232
"""The sign-flipped sum of the seven readings above. The ruling on #174 quotes
+0.3064 as a ratio for this capture, which is this figure over a hundred to four
places."""


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache")


@pytest.fixture
def source(data_config: DataConfig) -> CotSource:
    return CotSource(data_config)


def row(
    code: str,
    *,
    report_date: date = TUESDAY,
    open_interest: str = "100000",
    lev_long: str = "30000",
    lev_short: str = "10000",
    asset_long: str = "500000",
    asset_short: str = "100000",
    dealer_long: str = "1000",
    dealer_short: str = "400000",
    other_long: str = "7000",
    other_short: str = "3000",
) -> dict[str, str]:
    """One TFF row in the feed's own shape, every value a string.

    The asset manager and dealer columns are deliberately large and lopsided,
    so an implementation that nets them in cannot land on the same answer as one
    that reads leveraged funds alone.
    """
    return {
        TFF_FIELDS["report_date"]: f"{report_date.isoformat()}T00:00:00.000",
        TFF_FIELDS["contract_code"]: code,
        TFF_FIELDS["market_name"]: "TEST CONTRACT - CHICAGO MERCANTILE EXCHANGE",
        TFF_FIELDS["open_interest"]: open_interest,
        TFF_FIELDS["lev_money_long"]: lev_long,
        TFF_FIELDS["lev_money_short"]: lev_short,
        TFF_FIELDS["asset_mgr_long"]: asset_long,
        TFF_FIELDS["asset_mgr_short"]: asset_short,
        TFF_FIELDS["dealer_long"]: dealer_long,
        TFF_FIELDS["dealer_short"]: dealer_short,
        TFF_FIELDS["other_rept_long"]: other_long,
        TFF_FIELDS["other_rept_short"]: other_short,
    }


def route_every_contract(rows_for: dict[str, list[dict[str, str]]]) -> respx.Router:
    """Answer each contract's query with its own rows, keyed by contract code."""
    router = respx.mock(assert_all_called=False)

    def responder(request: httpx.Request) -> httpx.Response:
        where = request.url.params.get("$where", "")
        for currency, code in CONTRACT_CODES.items():
            if f"'{code}'" in where:
                return httpx.Response(200, json=rows_for.get(currency, []))
        return httpx.Response(200, json=[])

    router.get(url__startswith=BASE_URL).mock(side_effect=responder)
    return router


def live_rows_by_currency() -> dict[str, list[dict[str, str]]]:
    """The committed capture, indexed by the currency whose contract it is."""
    by_code = {r[TFF_FIELDS["contract_code"]]: r for r in LIVE_ROWS}
    return {
        currency: [by_code[code]]
        for currency, code in CONTRACT_CODES.items()
        if code in by_code
    }


# --- what the module declares ------------------------------------------------


def test_the_seven_contract_codes_are_the_ones_that_were_verified() -> None:
    """Guards every routing test below, which would narrow silently with this map.

    A mis-routed code returns a real position for the wrong currency, which no
    later check would catch: the number is in range, the currency exists, and
    nothing downstream knows which contract it came from.
    """
    assert CONTRACT_CODES == {
        "EUR": "099741",
        "GBP": "096742",
        "JPY": "097741",
        "CHF": "092741",
        "CAD": "090741",
        "AUD": "232741",
        "NZD": "112741",
    }


def test_the_default_dataset_is_the_futures_only_tff_report() -> None:
    """The Legacy report lumps every financial participant into one bucket.

    Its commercial and non-commercial split was designed for grain markets, and
    options positions in the combined series are delta-ambiguous, so neither the
    Legacy nor the combined dataset is the directional read this pillar wants.
    """
    assert DEFAULT_DATASET == DATASETS["tff_futures_only"] == "gpe5-46if"


# --- the raw fetch -----------------------------------------------------------


def test_the_query_is_composed_exactly(source: CotSource) -> None:
    """`$where`, `$order` and an explicit `$limit`, asserted as one URL.

    The default limit of 1000 is not relied on: a five-year weekly window is
    about 260 rows for one contract, which is inside it today and would silently
    truncate if the horizon ever widened.
    """
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[row(CONTRACT_CODES["EUR"])])
        )

        source.fetch_contract(CONTRACT_CODES["EUR"], TUESDAY, TUESDAY)

    request = route.calls[0].request
    assert request.url.path.endswith(f"/{DEFAULT_DATASET}.json")
    assert request.url.params["$where"] == (
        "cftc_contract_market_code='099741' "
        "AND report_date_as_yyyy_mm_dd>='2026-09-08T00:00:00.000' "
        "AND report_date_as_yyyy_mm_dd<='2026-09-08T00:00:00.000'"
    )
    assert request.url.params["$order"] == TFF_FIELDS["report_date"]
    assert request.url.params["$limit"] == "2000"


def test_each_currency_is_requested_by_its_own_contract_code(
    source: CotSource,
) -> None:
    """Asserted per currency, because a swap gives a real number under the wrong one.

    Driven through `fetch` rather than `fetch_contract`. The mapping from a
    currency to a contract code lives in `fetch`; handing `fetch_contract` a
    code and then finding that code in the `$where` tests string
    interpolation, and stays green under a `fetch` that routes every currency
    to its neighbour's contract.
    """
    asked: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        where = str(request.url.params["$where"])
        code = where.split("'")[1]
        asked[code] = where
        return httpx.Response(200, json=[])

    for currency, code in CONTRACT_CODES.items():
        asked.clear()
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=BASE_URL).mock(side_effect=responder)
            source.fetch(["cot_net_pct_oi"], [currency], START, END)
        assert list(asked) == [code], currency


def test_a_non_default_dataset_is_honoured(source: CotSource) -> None:
    """The parameter exists so a cross-check against Legacy is one argument away."""
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[])
        )

        source.fetch_contract(
            CONTRACT_CODES["EUR"],
            START,
            END,
            dataset=DATASETS["legacy_futures_only"],
        )

    assert route.calls[0].request.url.path.endswith("/6dca-aqww.json")


def test_a_row_for_another_contract_is_refused(source: CotSource) -> None:
    """The contract code and the window are one `$where`, so both are checked.

    `fetch` re-checks the report date window on the rows it got back, on the
    stated grounds that a provider might have ignored `$where`. The contract
    code sits in the same clause. Trusting one half and not the other leaves
    the half whose failure is undetectable: a mis-routed code returns a real
    position under the wrong currency, and every value is in range.
    """
    with (
        respx.mock(assert_all_called=True) as router,
        pytest.raises(SourceError, match="got a row for"),
    ):
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[row(CONTRACT_CODES["JPY"])])
        )

        source.fetch_contract(CONTRACT_CODES["EUR"], START, END)


def test_a_response_at_the_row_limit_is_refused_as_truncated(
    source: CotSource,
) -> None:
    """`$limit` moves the cliff, it does not remove it.

    Sending 2000 instead of relying on 1000 changes nothing on its own: no code
    compares the row count to the limit, so a longer horizon truncates exactly
    as silently. The rows are ordered oldest first, so what a truncated answer
    drops is the newest weeks, and a history that ends months ago reads as a
    quiet market rather than as a short answer. The Legacy datasets this
    method's `dataset` argument exposes carry about 2,080 weeks, which is
    already over the limit.
    """
    full = [
        row(CONTRACT_CODES["EUR"], report_date=date(2026, 1, 6) + timedelta(weeks=week))
        for week in range(ROW_LIMIT)
    ]

    with (
        respx.mock(assert_all_called=True) as router,
        pytest.raises(SourceError, match="truncated"),
    ):
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=full)
        )

        source.fetch_contract(CONTRACT_CODES["EUR"], START, END)


def test_a_repeated_failure_raises_rather_than_returning_nothing(
    source: CotSource,
) -> None:
    """An empty week and a source that is down are different facts.

    Downstream, an empty sequence reads as a currency with no positioning, which
    is a reading. A failed fetch is not.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(503, text="upstream unavailable")
        )

        with pytest.raises(SourceError):
            source.fetch_contract(CONTRACT_CODES["EUR"], START, END)


def test_an_empty_dataset_is_an_empty_sequence_and_not_a_failure(
    source: CotSource,
) -> None:
    """The converse. A week the CFTC genuinely did not publish is data."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[])
        )

        assert source.fetch_contract(CONTRACT_CODES["EUR"], START, END) == ()


# --- the reading, and which column it comes from -----------------------------


def test_the_reading_is_leveraged_funds_over_open_interest_in_percent(
    source: CotSource,
) -> None:
    """Hand-computed on one written row: (30000 - 10000) / 100000, as a percent.

    The literal is 20.0 rather than 0.2 because the key is named for a percent
    and `PositioningPillar` documents receiving one. A ratio here would read as
    a book a hundred times flatter than it is.
    """
    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert len(observations) == 1
    assert observations[0].value == pytest.approx(20.0)


def test_the_asset_manager_and_dealer_columns_are_not_netted_in(
    source: CotSource,
) -> None:
    """The ruling on #174, asserted on the columns rather than on the arithmetic.

    The written row carries an asset manager net of +400000 and a dealer net of
    -399000 against a leveraged-funds net of +20000, so every candidate reading
    lands on a different number. Asset manager plus leveraged funds plus other
    reportables is what Legacy calls non-commercial, and reassembling it out of
    TFF's parts discards the only thing the TFF dataset buys.
    """
    written = row(CONTRACT_CODES["EUR"])
    open_interest = float(written[TFF_FIELDS["open_interest"]])
    leveraged = 30000.0 - 10000.0
    non_commercial = leveraged + (500000.0 - 100000.0) + (7000.0 - 3000.0)

    with route_every_contract({"EUR": [written]}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert observations[0].value == pytest.approx(leveraged / open_interest * 100.0)
    assert observations[0].value != pytest.approx(
        non_commercial / open_interest * 100.0
    )


def test_a_zero_open_interest_is_refused_rather_than_divided(
    source: CotSource,
) -> None:
    """A contract with no open interest has no reading, and that is not zero.

    A zero would read as a flat book, which is a position. Dividing raises.
    """
    with (
        route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"], open_interest="0")]}),
        pytest.raises(SourceError, match="open interest"),
    ):
        source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)


def test_a_row_missing_a_column_is_refused(source: CotSource) -> None:
    """A renamed Socrata column must stop the run, not skip a week silently."""
    incomplete = row(CONTRACT_CODES["EUR"])
    del incomplete[TFF_FIELDS["lev_money_long"]]

    with route_every_contract({"EUR": [incomplete]}), pytest.raises(SourceError):
        source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)


@pytest.mark.parametrize("written", ["NaN", "nan", "Infinity", "-inf"])
def test_a_non_finite_column_is_refused_rather_than_compared(
    source: CotSource, written: str
) -> None:
    """`float` reads ``"NaN"`` happily, and then no comparison against it is true.

    A NaN open interest passes any positivity check, because every comparison
    with NaN is false, and then divides into a NaN reading that the pillar
    z-scores into a NaN score with nothing raising. An infinity divides into
    zero, which reads as a flat book. Both are caught where the string becomes
    a number.
    """
    with (
        route_every_contract(
            {"EUR": [row(CONTRACT_CODES["EUR"], open_interest=written)]}
        ),
        pytest.raises(SourceError, match="non-finite"),
    ):
        source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)


# --- the dates ---------------------------------------------------------------


def test_the_period_is_the_tuesday_and_the_release_is_the_friday(
    source: CotSource,
) -> None:
    """Three days apart, and `period` is never the release date.

    Scoring a COT reading as though it were current is the standard misuse of
    this dataset, and the staleness ramp downstream reads `period`.
    """
    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    published = observations[0].released_at
    assert observations[0].period == TUESDAY
    assert published is not None
    assert published.date() == FRIDAY
    assert (published.date() - observations[0].period).days == 3
    assert observations[0].period != published.date()


@pytest.mark.parametrize(
    ("snapped", "instant"),
    [
        (date(2026, 9, 8), "2026-09-11T19:30:00+00:00"),
        (date(2026, 12, 8), "2026-12-11T20:30:00+00:00"),
    ],
)
def test_the_release_instant_is_1530_eastern_in_both_halves_of_the_year(
    source: CotSource, snapped: date, instant: str
) -> None:
    """15:30 Eastern is 19:30 UTC in summer and 20:30 UTC in winter.

    A fixed offset, or Eastern read as UTC, is four or five hours out. Nothing
    reads the hour today: the visibility rule in `fbe.pillars.base` compares
    ``released_at.date()`` against the run date, so a Friday-dated run sees the
    reading whatever the hour. This pins the instant anyway, because it is a
    published fact and the first consumer to compare times would inherit the
    error silently. Both halves of the year are asserted because a fixed offset
    passes in one of them.
    """
    with route_every_contract(
        {"EUR": [row(CONTRACT_CODES["EUR"], report_date=snapped)]}
    ):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    published = observations[0].released_at
    assert published is not None
    # Compared as instants, so the assertion is about the moment rather than
    # about which offset the source chose to express it in.
    assert published == datetime.fromisoformat(instant)


def test_a_monday_snapshot_in_a_holiday_week_is_released_that_friday(
    source: CotSource,
) -> None:
    """Veterans Day 2025 fell on a Tuesday, so the CFTC snapped on the Monday.

    The live dataset carries 2025-11-10 as a report date, one of twelve Mondays
    since 2007 and every one the day before a Tuesday federal holiday (#274).
    Before that issue the source refused any non-Tuesday, and because the
    default lookback reaches 2023-07-03, the refusal fired on every refresh.

    The period is the Monday, because that is when the positions were snapped.
    The release is the Friday of the same week, four days on rather than three,
    which is why the stamp is computed from the weekday rather than by adding a
    lag. The instant is pinned in UTC so a fixed offset cannot pass.
    """
    monday = date(2025, 11, 10)
    assert monday.strftime("%A") == "Monday"
    assert monday.weekday() in SNAPSHOT_WEEKDAYS

    with route_every_contract(
        {"EUR": [row(CONTRACT_CODES["EUR"], report_date=monday)]}
    ):
        observations = source.fetch(
            ["cot_net_pct_oi"], ["EUR"], date(2025, 1, 1), date(2025, 12, 31)
        )

    assert len(observations) == 1
    assert observations[0].period == monday
    published = observations[0].released_at
    assert published is not None
    assert published.date() == date(2025, 11, 14)
    assert published.date().strftime("%A") == "Friday"
    assert published == datetime.fromisoformat("2025-11-14T20:30:00+00:00")


@pytest.mark.parametrize(
    ("report_date", "weekday"),
    [
        (date(2026, 9, 9), "Wednesday"),
        (date(2026, 9, 13), "Sunday"),
    ],
)
def test_a_report_date_on_any_other_weekday_is_refused(
    source: CotSource, report_date: date, weekday: str
) -> None:
    """Monday and Tuesday are the only days the CFTC has ever snapped on.

    The Friday is computed from the weekday, so a Wednesday would be stamped
    two days on and a Sunday five days earlier than its own period, and the
    stamp is what the visibility rule reads. Neither day appears in the
    dataset, so either means a renamed or misread column rather than a week
    snapped on another day. Both sides of the accepted pair are asserted so
    that widening it to a range cannot pass silently.
    """
    assert report_date.strftime("%A") == weekday
    assert report_date.weekday() not in SNAPSHOT_WEEKDAYS

    with (
        route_every_contract(
            {"EUR": [row(CONTRACT_CODES["EUR"], report_date=report_date)]}
        ),
        pytest.raises(SourceError, match=f"a {weekday} rather than a Tuesday"),
    ):
        source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)


def test_the_release_timestamp_is_timezone_aware(source: CotSource) -> None:
    """A naive timestamp compares false against every aware one in the engine."""
    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    published = observations[0].released_at
    assert published is not None
    assert published.tzinfo is not None


def test_a_report_date_outside_the_window_is_not_returned(source: CotSource) -> None:
    """The window is the caller's, and the query carries it.

    Asserted on the returned rows as well as the URL, because a provider that
    ignored `$where` would otherwise widen the answer silently.
    """
    inside = row(CONTRACT_CODES["EUR"], report_date=date(2026, 9, 8))
    outside = row(CONTRACT_CODES["EUR"], report_date=date(2025, 1, 7))

    with route_every_contract({"EUR": [outside, inside]}):
        observations = source.fetch(
            ["cot_net_pct_oi"], ["EUR"], date(2026, 1, 1), date(2026, 12, 31)
        )

    assert [item.period for item in observations] == [date(2026, 9, 8)]


def test_the_weeks_come_back_oldest_first_whatever_order_socrata_sent(
    source: CotSource,
) -> None:
    """The query asks for ascending dates. Relying on that is the defect.

    `latest_report_date` already does not trust `$order`, for the same reason.
    Here it matters downstream rather than upstream: the pillar's time-series
    z-score reads a history, and a history in provider order is a history whose
    ends are wherever the provider put them.
    """
    weeks = [date(2026, 9, 8), date(2026, 8, 25), date(2026, 9, 1)]
    sent = [row(CONTRACT_CODES["EUR"], report_date=week) for week in weeks]

    with route_every_contract({"EUR": sent}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert [item.period for item in observations] == sorted(weeks)


# --- the derived dollar ------------------------------------------------------


def test_the_dollar_is_the_sign_flipped_net_of_the_seven_legs(
    source: CotSource,
) -> None:
    """A long EUR future is a short dollar position, so the sign flips.

    Hand-built: two currencies at +20.0 and one at -50.0 net to -10.0, so the
    dollar reads +10.0.
    """
    rows = {
        "EUR": [row(CONTRACT_CODES["EUR"], lev_long="30000", lev_short="10000")],
        "GBP": [row(CONTRACT_CODES["GBP"], lev_long="30000", lev_short="10000")],
        "JPY": [row(CONTRACT_CODES["JPY"], lev_long="10000", lev_short="60000")],
    }

    derived = source.derive_usd_position(rows)

    assert derived == [(TUESDAY, pytest.approx(10.0))]


def test_the_dollar_is_not_a_raw_sum_of_contract_counts(source: CotSource) -> None:
    """Open interest differs by an order of magnitude across the seven.

    A raw sum is dominated by the euro and says almost nothing about the dollar.
    Here the euro carries ten times the open interest of the yen and half its
    net, so the normalised sum and the count sum have opposite signs.
    """
    rows = {
        "EUR": [
            row(
                CONTRACT_CODES["EUR"],
                open_interest="1000000",
                lev_long="30000",
                lev_short="0",
            )
        ],
        "JPY": [
            row(
                CONTRACT_CODES["JPY"],
                open_interest="100000",
                lev_long="0",
                lev_short="15000",
            )
        ],
    }

    derived = source.derive_usd_position(rows)

    # Percents: +3.0 and -15.0, netting -12.0, so the dollar reads +12.0.
    # Counts: +30000 and -15000, netting +15000, which would flip the sign.
    assert derived[0][1] == pytest.approx(12.0)
    assert derived[0][1] > 0


def test_summing_and_averaging_the_legs_give_the_same_pillar_reading() -> None:
    """A constant factor cancels in a time-series z-score, so the choice is free.

    Averaging is summing divided by seven. The pillar z-scores the dollar series
    against its own history, and dividing every point of a series by the same
    constant leaves every z-score unchanged. So this module sums, and nothing
    downstream can tell.

    The fourteen readings repeat until the window clears `WEEKLY_FLOOR`, which
    since #214 is a year of weekly prints rather than a bare count of twelve.
    Repetition is harmless here because the claim under test is that a constant
    factor cancels, and it cancels whatever the history is, so long as the
    standard deviation is not zero.
    """
    from fbe.datasources.registry import MIN_HISTORY_OBSERVATIONS
    from fbe.pillars.base import BasePillar
    from fbe.types import Frequency, Observation

    WEEKLY_FLOOR = MIN_HISTORY_OBSERVATIONS[Frequency.WEEKLY]

    summed = [
        -0.30,
        -0.10,
        0.05,
        0.20,
        -0.15,
        0.30,
        0.10,
        -0.20,
        0.00,
        0.25,
        -0.05,
        0.15,
        0.35,
        -0.25,
    ]
    repeats = -(-WEEKLY_FLOOR // len(summed))
    summed = summed * repeats
    averaged = [value / len(CONTRACT_CODES) for value in summed]

    def series(values: list[float]) -> list[Observation]:
        return [
            Observation(
                indicator="cot_net_pct_oi",
                currency="USD",
                value=value,
                period=date(2024, 1, 2) + timedelta(weeks=index),
                source="test",
                series_id="derived",
                unit="percent_of_open_interest",
                frequency=Frequency.WEEKLY,
            )
            for index, value in enumerate(values)
        ]

    lookback = 5
    asof = date(2026, 12, 31)
    from_sum = BasePillar.time_series_z(series(summed), lookback, asof)
    from_average = BasePillar.time_series_z(series(averaged), lookback, asof)

    assert from_sum is not None
    assert from_sum == pytest.approx(from_average)


def test_a_week_one_leg_did_not_publish_has_no_dollar_reading(
    source: CotSource,
) -> None:
    """A contract the CFTC did not publish is not a contract at zero.

    And a sum cannot tell the two apart. Leaving the term out and adding a
    zero produce the identical number, so "the missing leg takes no part" is
    not a behaviour, it is a description of adding zero. The reading that comes
    out is smaller in magnitude than the published legs imply and nothing marks
    it, which is this repository's worst outcome rather than a tolerable gap.

    So the date is not derived at all. An absent week is an absence the pillar
    can represent, and `PositioningPillar` scores a currency on the history it
    has.
    """
    rows = {
        "EUR": [row(CONTRACT_CODES["EUR"], lev_long="30000", lev_short="10000")],
        "GBP": [],
    }

    assert source.derive_usd_position(rows) == []

    # And the value that would have been reported, to say what was avoided
    # rather than only that something was.
    complete = dict(rows)
    complete["GBP"] = [row(CONTRACT_CODES["GBP"], lev_long="0", lev_short="0")]
    assert source.derive_usd_position(complete) == [(TUESDAY, pytest.approx(-20.0))]


def test_a_dollar_week_is_dropped_only_for_the_leg_that_is_missing_it(
    source: CotSource,
) -> None:
    """The completeness rule is per date, not per contract.

    A leg that missed one week must not cost the dollar every other week it did
    publish, which would turn one publication gap into a hole in the history.
    """
    earlier = date(2026, 9, 1)
    rows = {
        "EUR": [
            row(CONTRACT_CODES["EUR"], report_date=earlier),
            row(CONTRACT_CODES["EUR"], report_date=TUESDAY),
        ],
        "GBP": [row(CONTRACT_CODES["GBP"], report_date=TUESDAY)],
    }

    derived = source.derive_usd_position(rows)

    assert [when for when, _ in derived] == [TUESDAY]


def test_two_rows_for_one_leg_and_week_are_refused(source: CotSource) -> None:
    """One row per contract per week is this dataset's shape.

    Summing both would double that leg's weight in the dollar, and the dollar
    is the one series here that cannot be checked against a published figure.
    """
    duplicated = {
        "EUR": [
            row(CONTRACT_CODES["EUR"]),
            row(CONTRACT_CODES["EUR"], lev_long="80000"),
        ],
    }

    with pytest.raises(SourceError, match="two rows"):
        source.derive_usd_position(duplicated)


def test_the_dollar_is_derived_per_report_date(source: CotSource) -> None:
    """Two weeks in, two weeks out, each netting only its own legs."""
    earlier = date(2026, 9, 1)
    rows = {
        "EUR": [
            row(
                CONTRACT_CODES["EUR"],
                report_date=earlier,
                lev_long="20000",
                lev_short="10000",
            ),
            row(CONTRACT_CODES["EUR"], lev_long="30000", lev_short="10000"),
        ],
    }

    derived = source.derive_usd_position(rows)

    assert [when for when, _ in derived] == [earlier, TUESDAY]
    assert derived[0][1] == pytest.approx(-10.0)
    assert derived[1][1] == pytest.approx(-20.0)


def test_a_dollar_week_outside_the_window_is_not_returned(source: CotSource) -> None:
    """The dollar leg carries the same window check as the seven, and separately.

    It is a second code path with a second filter, and the seven-currency test
    above passes with this one removed. A provider ignoring `$where` would
    otherwise widen the dollar series alone, which is the one series nobody can
    check against a published figure.
    """
    inside = date(2026, 9, 8)
    outside = date(2025, 1, 7)
    rows = {
        currency: [
            row(code, report_date=outside),
            row(code, report_date=inside),
        ]
        for currency, code in CONTRACT_CODES.items()
    }

    with route_every_contract(rows):
        observations = source.fetch(
            ["cot_net_pct_oi"], ["USD"], date(2026, 1, 1), date(2026, 12, 31)
        )

    assert [item.period for item in observations] == [inside]


def test_a_leg_empty_for_the_whole_window_stops_the_dollar_rather_than_emptying_it(
    source: CotSource,
) -> None:
    """The completeness rule has a failure mode and this is it.

    A date is derived only when every leg reported it, so one contract that
    answers with nothing for the entire window leaves no complete date and the
    dollar series comes back empty. Empty reads downstream as a currency with
    no positioning, which is the distinction the twelfth criterion exists for:
    a contract code that changed underneath us is not a quiet market.
    """
    rows = {
        currency: [row(code)]
        for currency, code in CONTRACT_CODES.items()
        if currency != "NZD"
    }
    rows["NZD"] = []

    with (
        route_every_contract(rows),
        pytest.raises(SourceError, match="no rows at all for NZD"),
    ):
        source.fetch(["cot_net_pct_oi"], ["USD"], START, END)


def test_a_window_no_contract_published_is_an_absence_and_not_a_failure(
    source: CotSource,
) -> None:
    """The converse, and what tells the two apart.

    All seven empty is a window the dataset holds no week for, which is data: a
    caller can ask for a range before the contracts listed. Six populated and
    one empty is that one contract.
    """
    with route_every_contract({currency: [] for currency in CONTRACT_CODES}):
        observations = source.fetch(["cot_net_pct_oi"], ["USD"], START, END)

    assert observations == ()


def test_the_dollar_reaches_fetch_as_its_own_observation(source: CotSource) -> None:
    """`"USD"` triggers the derivation rather than a lookup of the ICE index."""
    with route_every_contract(live_rows_by_currency()):
        observations = source.fetch(["cot_net_pct_oi"], ["USD"], START, END)

    assert [item.currency for item in observations] == ["USD"]
    assert observations[0].value == pytest.approx(IMPLIED_USD)
    assert observations[0].period == TUESDAY


# --- the spot check on the committed capture ---------------------------------


def test_the_committed_capture_reproduces_its_published_figures(
    source: CotSource,
) -> None:
    """One published row, asserted against numbers nobody here chose.

    EURO FX on 2026-09-08: 942,464 contracts of open interest, leveraged funds
    long 94,808 and short 128,093. This is the assertion that breaks the build if
    the CFTC renames a column, changes its date format, or starts returning
    numbers rather than strings.
    """
    eur = next(
        item
        for item in LIVE_ROWS
        if item[TFF_FIELDS["contract_code"]] == CONTRACT_CODES["EUR"]
    )

    assert eur[TFF_FIELDS["market_name"]].startswith("EURO FX")
    assert eur[TFF_FIELDS["open_interest"]] == "942464"
    assert eur[TFF_FIELDS["lev_money_long"]] == "94808"
    assert eur[TFF_FIELDS["lev_money_short"]] == "128093"
    assert eur[TFF_FIELDS["report_date"]].startswith("2026-09-08")

    with route_every_contract(live_rows_by_currency()):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert observations[0].value == pytest.approx((94808 - 128093) / 942464 * 100.0)


def test_every_captured_currency_reproduces_its_published_reading(
    source: CotSource,
) -> None:
    """All seven, each a different number, recomputed from the published integers."""
    with route_every_contract(live_rows_by_currency()):
        observations = source.fetch(
            ["cot_net_pct_oi"], list(CONTRACT_CODES), START, END
        )

    by_currency = {item.currency: item.value for item in observations}
    for currency, expected in PUBLISHED_PERCENTS.items():
        assert by_currency[currency] == pytest.approx(expected), currency


def test_the_capture_gives_the_dollar_reading_the_ruling_quotes(
    source: CotSource,
) -> None:
    """+0.3064 on this capture, which the ruling on #174 arrived at independently.

    The ruling quotes the ratio, so the ratio is what is rounded here. Writing
    the comparison the other way round would restate this module's scale instead
    of checking it against the figure a person computed by hand.
    """
    derived = source.derive_usd_position(live_rows_by_currency())

    assert derived == [(TUESDAY, pytest.approx(IMPLIED_USD))]
    assert round(derived[0][1] / 100.0, 4) == 0.3064


# --- the contract with the rest of the engine --------------------------------


def test_the_observation_carries_the_registrys_unit_and_frequency(
    source: CotSource,
) -> None:
    """Copied from the ref rather than retyped, which is what keeps them honest."""
    spec = INDICATORS["cot_net_pct_oi"]

    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        observations = source.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert observations[0].unit == spec.series["EUR"].unit
    assert observations[0].frequency == spec.series["EUR"].frequency
    assert observations[0].source == "cftc"


def test_the_registry_unit_says_a_percent_of_open_interest() -> None:
    """The source emits a percent, so the registry must say so, not contracts.

    A registry naming one quantity while the code emits another is how a score
    gets built on the wrong number, which is this issue's own reasoning about
    the unit. The unit is echoed onto every `Observation`, so a stale one
    travels with the number rather than staying in the registry.
    """
    spec = INDICATORS["cot_net_pct_oi"]

    assert spec.unit == "percent_of_open_interest"
    assert all(ref.unit == spec.unit for ref in spec.series.values())


def test_the_registry_description_no_longer_says_the_division_is_unimplemented() -> (
    None
):
    """It said so truthfully until this change, and says it falsely afterwards."""
    description = INDICATORS["cot_net_pct_oi"].description

    assert "not yet implemented" not in description
    assert "still yields raw contracts" not in description


def test_only_the_key_this_source_serves_is_fetched(source: CotSource) -> None:
    """Anything else is ignored rather than guessed at."""
    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        observations = source.fetch(["cpi_yoy"], ["EUR"], START, END)

    assert observations == ()


def test_refs_comes_from_the_registry_rather_than_a_list_here(
    source: CotSource,
) -> None:
    """Routing a ref to this source should be a registry edit and nothing else."""
    expected = {
        (spec.key, currency): ref
        for spec in INDICATORS.values()
        for currency, ref in spec.series.items()
        if ref.source == "cftc"
    }

    assert source.refs() == expected
    assert ("cot_net_pct_oi", "EUR") in source.refs()


# --- the newest report date --------------------------------------------------


def test_the_latest_report_date_is_the_newest_tuesday(source: CotSource) -> None:
    """The pillar ages against this rather than assuming the weekly cadence held."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    row(CONTRACT_CODES["EUR"], report_date=date(2026, 9, 1)),
                    row(CONTRACT_CODES["EUR"], report_date=TUESDAY),
                ],
            )
        )

        assert source.latest_report_date() == TUESDAY


def test_the_latest_date_query_reads_more_than_one_row(source: CotSource) -> None:
    """`max` over a window is the guard, and one row makes it trust `$order`.

    The query sorts descending and the code reduces with `max` anyway, so that
    a provider ignoring the sort cannot hand back the oldest week as the newest.
    A `$limit` of 1 would defeat that: the one row returned would be whichever
    the provider chose, and `max` of one row is that row.
    """
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[])
        )

        source.latest_report_date()

    params = route.calls[0].request.url.params
    assert params["$order"] == f"{TFF_FIELDS['report_date']} DESC"
    assert params["$limit"] == "50"


def test_an_empty_dataset_gives_no_latest_date_rather_than_today(
    source: CotSource,
) -> None:
    """Publication has been interrupted before, and today's date would hide it."""
    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[])
        )

        assert source.latest_report_date() is None


def test_rows_without_the_date_column_raise_rather_than_reading_as_empty(
    source: CotSource,
) -> None:
    """`None` is reserved for a dataset that holds no week.

    A renamed date column drops every row from the comprehension and would
    otherwise arrive at the same `None`, which is the collision the method's own
    docstring says it exists to avoid. The pillar ages against this answer.
    """
    with (
        respx.mock(assert_all_called=True) as router,
        pytest.raises(SourceError, match="changed shape"),
    ):
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[{"snapshot_date": "2026-09-08"}])
        )

        source.latest_report_date()


def test_a_failure_while_asking_for_the_latest_date_raises(source: CotSource) -> None:
    """`None` means the dataset is empty, so a failure must not answer `None`."""
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(500, text="boom")
        )

        with pytest.raises(SourceError):
            source.latest_report_date()


# --- availability and the doctor probe ---------------------------------------


def test_availability_answers_on_configuration_and_makes_no_request(
    source: CotSource,
) -> None:
    """The endpoints need no key, so online there is nothing to rule this out.

    `BaseDataSource.available` forbids a request here and
    `tests/test_datasource_base.py` enforces it across every source, so the
    response-shape check the issue asks for lives on `probe_request` instead,
    which is where `fbe doctor` reads it.
    """
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith=BASE_URL).mock(
            return_value=httpx.Response(200, json=[])
        )

        assert source.available() is True

    assert not route.called


def test_the_doctor_probe_verifies_the_body_and_not_the_status(
    source: CotSource,
) -> None:
    """Socrata answers some failures with a 200 and a JSON error object.

    A status-only verdict prints that as healthy, which is the defect #187
    fixed for the other sources.
    """
    probe = source.probe_request()

    assert probe is not None
    probe.verify(json.dumps([row(CONTRACT_CODES["EUR"])]).encode())

    with pytest.raises(SourceError):
        probe.verify(b'{"error": true, "message": "query is invalid"}')
    with pytest.raises(SourceError):
        probe.verify(b"<html>maintenance</html>")


def test_the_probe_names_the_dataset_this_source_actually_reads(
    source: CotSource,
) -> None:
    """A probe against a different dataset proves the wrong endpoint is up."""
    probe = source.probe_request()

    assert probe is not None
    assert DEFAULT_DATASET in probe.path
    # The key set, not a prefix on the values. No real Socrata app token starts
    # with "app", and the token would arrive under a key named `$$app_token`,
    # which a scan of the values cannot see at all.
    assert set(probe.params) == {"$limit", "$order"}


# --- offline and the cache ---------------------------------------------------


def test_an_offline_run_with_a_cold_cache_raises(tmp_path: Path) -> None:
    """Offline with nothing stored is a failure, not an empty week."""
    offline = CotSource(DataConfig(cache_dir=tmp_path / "cache", offline=True))

    with pytest.raises(SourceError):
        offline.fetch_contract(CONTRACT_CODES["EUR"], START, END)


def test_a_cached_week_is_served_offline(data_config: DataConfig) -> None:
    """The weekly cadence makes a stored week genuinely usable."""
    warm = CotSource(data_config)
    with route_every_contract({"EUR": [row(CONTRACT_CODES["EUR"])]}):
        first = warm.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    offline = CotSource(DataConfig(cache_dir=data_config.cache_dir, offline=True))
    second = offline.fetch(["cot_net_pct_oi"], ["EUR"], START, END)

    assert [item.value for item in second] == [item.value for item in first]


def test_the_module_records_that_the_report_is_stale_by_design() -> None:
    """The three-day lag is structural and the docstring has to keep saying so."""
    from fbe.datasources import cot

    assert cot.__doc__ is not None
    assert "Tuesday" in cot.__doc__
    assert "Friday" in cot.__doc__


def test_nothing_in_this_module_reaches_the_network_without_respx() -> None:
    """Every test above routes through `respx`; this pins the import surface.

    A later change fetching through `requests` or `urllib` would bypass the
    shared retry, cache and offline path as well as these mocks.
    """
    source_text = (
        Path(__file__).resolve().parents[1] / "src" / "fbe" / "datasources" / "cot.py"
    ).read_text()

    assert "import requests" not in source_text
    assert "urllib" not in source_text
    assert "httpx.get" not in source_text
