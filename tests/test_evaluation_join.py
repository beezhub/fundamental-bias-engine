"""Joining a past report to the move that followed it.

This is the piece that makes the model measurable without waiting for trades.
Twenty-eight pairs are recorded every morning and perhaps five a week are
traded, so the bias record carries roughly forty times the evidence the journal
does, and it is the only route to a conclusion this side of years.

It is also the piece where a mistake is invisible. Four ways it could be wrong
would each produce a full set of plausible rows:

- Reading a price dated on or before the as-of. The report was built from
  observations released by then, so a same-day close is a price the run could
  have been looking at. A backtest that sees the future flatters, and nothing
  downstream can tell.
- Inverting the move. Every row would still be a number in the right range, and
  the evaluation built on it would report the model backwards.
- Returning zero for a pair whose prices do not reach past the horizon yet. A
  zero move is a finding; an unmeasurable one is not, and pooling them makes
  the record read flatter than it is.
- Dropping a report that will not decode. The record then understates itself
  and everything computed from it is flattered.

Every test here builds its own rates and reads the committed report fixture.
Nothing reaches the network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from fbe.config import ScoringConfig
from fbe.datasources.prices import FRED_SPOT_SERIES
from fbe.evaluation import ForwardRow, join_report, join_reports
from fbe.report import load_report, write_report
from fbe.risk import MissingRateError
from fbe.types import BiasReport, Conviction, Direction

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"

ASOF = date(2026, 6, 30)
"""The committed fixture's as-of date, and the line every test is about."""

MAJORS = tuple(sorted(FRED_SPOT_SERIES))
"""The pairs FRED publishes, taken from the source layer rather than retyped.

Written out here, a rename or a re-keying on that side would leave this suite
green while the real wiring inverted, which is the defect this whole file is
about.
"""

BASE_RATES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "AUDUSD": 0.6600,
    "NZDUSD": 0.6100,
    "USDJPY": 155.00,
    "USDCHF": 0.8900,
    "USDCAD": 1.3600,
}
"""One session's fixings, in market convention, as `FRED_SPOT_SERIES` keys them."""


def test_the_fixture_prices_are_the_pairs_the_source_layer_serves() -> None:
    """Otherwise this file tests a join against prices nothing produces."""
    assert set(BASE_RATES) == set(FRED_SPOT_SERIES)


@pytest.fixture(scope="module")
def report() -> BiasReport:
    return load_report(FIXTURE)


def rates_on(
    days: range | tuple[int, ...],
    *,
    drift: float = 0.0,
    only: tuple[str, ...] = MAJORS,
    start: date = ASOF,
) -> dict[date, dict[str, float]]:
    """Fixings for each offset in ``days``, measured from ``start``.

    ``drift`` moves every dollar pair by that fraction per day in the direction
    that strengthens the dollar, so the answer for any pair is arithmetic
    rather than a number read off a table.
    """
    table: dict[date, dict[str, float]] = {}
    for offset in days:
        session = start + timedelta(days=offset)
        table[session] = {
            pair: BASE_RATES[pair]
            * (
                (1.0 - drift * offset)
                if pair.startswith(("EUR", "GBP", "AUD", "NZD"))
                else (1.0 + drift * offset)
            )
            for pair in only
        }
    return table


def rows_by_pair(rows: tuple[ForwardRow, ...]) -> dict[str, ForwardRow]:
    return {row.pair: row for row in rows}


# --- the as-of line -----------------------------------------------------------


def test_no_price_at_or_before_the_asof_takes_part(report: BiasReport) -> None:
    """The line the whole join is about.

    The report was built from observations released on or before its as-of, so
    a fixing dated that day is one the run could have been looking at. Reading
    it makes the recorded bias and the move that "followed" it share a price,
    and every hit rate computed afterwards is flattered by it.
    """
    before = rates_on(range(-5, 1), drift=0.01)
    after = rates_on(range(1, 12), drift=0.01)
    wrong = {**before, **after}

    without = join_report(report, after)
    with_history = join_report(report, wrong)

    assert with_history.rows
    assert {row.pair: row.move for row in with_history.rows} == {
        row.pair: row.move for row in without.rows
    }
    assert all(row.opened_at > ASOF for row in with_history.rows)


def test_the_move_opens_on_the_first_session_after_the_asof(
    report: BiasReport,
) -> None:
    """Not on the as-of, and not on an arbitrary later one.

    The bias is published in the morning and acted on from the next session, so
    that session is where the record starts. Opening later would quietly skip
    the first day's move, which is the one the bias was most about.
    """
    rates = rates_on((3, 5, 9, 12), drift=0.01)

    rows = join_report(report, rates).rows

    assert {row.opened_at for row in rows} == {ASOF + timedelta(days=3)}


def test_the_move_closes_on_the_last_session_inside_the_horizon(
    report: BiasReport,
) -> None:
    """The horizon is a window, and the market is not open every day in it.

    Requiring a fixing dated exactly on the closing day would drop every
    horizon that lands on a weekend, which is two days in seven.
    """
    rates = rates_on((1, 5, 9, 11, 14), drift=0.01)
    scoring = replace(ScoringConfig(), horizon_days=10)

    rows = join_report(report, rates, scoring=scoring).rows

    assert {row.closed_at for row in rows} == {ASOF + timedelta(days=9)}


def test_sessions_given_out_of_order_are_read_in_time_order(
    report: BiasReport,
) -> None:
    """Insertion order is not time order, and most price APIs answer newest first.

    Taken as given, the open and the close swap and every move on the page is
    inverted, which is a plausible number in the right range and the exact
    failure this module's docstring is about.
    """
    ascending = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=10): {**BASE_RATES, "EURUSD": 1.1000},
    }
    descending = dict(reversed(list(ascending.items())))

    row = rows_by_pair(join_report(report, descending).rows)["EURUSD"]

    assert list(descending) != sorted(descending)
    assert row.opened_at < row.closed_at
    assert row.open_rate == pytest.approx(1.0850)
    assert row.move == pytest.approx((1.1000 / 1.0850) - 1.0)
    assert row.move > 0


def test_a_move_the_size_of_a_real_week_survives_to_the_row(
    report: BiasReport,
) -> None:
    """Forty pips on EURUSD is an ordinary ten days, and it is 0.4 per cent.

    Rounded anywhere on the way through, every ordinary move collapses to a
    flat result, which is the same defect criterion four is about arriving
    through a different door: an unremarkable fortnight would enter the sample
    as twenty-eight pairs that did not move.
    """
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=10): {**BASE_RATES, "EURUSD": 1.0890},
    }

    row = rows_by_pair(join_report(report, rates).rows)["EURUSD"]

    assert row.move == pytest.approx((1.0890 / 1.0850) - 1.0)
    assert row.move != 0.0
    assert abs(row.move) < 0.005


# --- the horizon --------------------------------------------------------------


def test_the_horizon_is_read_from_config(report: BiasReport) -> None:
    """A literal here would measure one window while the engine scored another."""
    rates = rates_on(range(1, 30), drift=0.005)

    short = join_report(report, rates, scoring=replace(ScoringConfig(), horizon_days=3))
    long = join_report(report, rates, scoring=replace(ScoringConfig(), horizon_days=20))

    assert {row.closed_at for row in short.rows} == {ASOF + timedelta(days=3)}
    assert {row.closed_at for row in long.rows} == {ASOF + timedelta(days=20)}
    assert rows_by_pair(short.rows)["EURUSD"].move != pytest.approx(
        rows_by_pair(long.rows)["EURUSD"].move
    )


def test_the_default_horizon_is_the_configured_one(report: BiasReport) -> None:
    rates = rates_on(range(1, 30), drift=0.005)

    rows = join_report(report, rates).rows

    assert {row.closed_at for row in rows} == {
        ASOF + timedelta(days=ScoringConfig().horizon_days)
    }


def test_the_reach_test_reads_the_same_horizon_as_the_window(
    report: BiasReport,
) -> None:
    """A number in two places will disagree, and here the disagreement is silent.

    With the horizon at twenty days and prices stopping on day twelve, the
    answer is that nothing can be said yet. Half the function reading the
    packaged ten instead publishes an eleven-day move stamped as the twenty-day
    one, and every figure downstream is computed over the wrong window.
    """
    rates = rates_on(range(1, 13), drift=0.005)
    scoring = replace(ScoringConfig(), horizon_days=20)

    joined = join_report(report, rates, scoring=scoring)

    assert joined.rows == ()
    assert any("20-day horizon" in problem for problem in joined.problems)


def test_prices_stopping_a_day_short_of_the_horizon_are_not_enough(
    report: BiasReport,
) -> None:
    """The boundary between "the market was shut" and "we cannot say yet".

    A grace window of even a day or two here publishes a nine-day move as the
    ten-day result, and the reader has nothing on the row to tell them.
    """
    rates = rates_on((1, 5, 9), drift=0.005)
    scoring = replace(ScoringConfig(), horizon_days=10)

    joined = join_report(report, rates, scoring=scoring)

    assert joined.rows == ()
    assert any("horizon" in problem for problem in joined.problems)


def test_prices_that_miss_the_window_entirely_produce_no_row(
    report: BiasReport,
) -> None:
    """A table reaching past the horizon with nothing inside it.

    Falling back to whatever the table does hold would open on a price dated
    before the as-of, which is the one thing this module exists to refuse.
    """
    rates = {
        ASOF - timedelta(days=5): dict(BASE_RATES),
        ASOF + timedelta(days=40): {**BASE_RATES, "EURUSD": 1.2000},
    }

    joined = join_report(report, rates)

    assert joined.rows == ()
    assert joined.problems


def test_a_pair_that_stops_being_quoted_is_not_carried_forward(
    report: BiasReport,
) -> None:
    """A hole in one pair's series is not a session where it did not move.

    Measured to its last quote while the row still says it closed on the
    horizon, the move is real and the window it is labelled with is not.
    """
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=4): {**BASE_RATES, "EURUSD": 1.2000},
        ASOF + timedelta(days=10): {
            pair: rate for pair, rate in BASE_RATES.items() if pair != "EURUSD"
        },
    }

    joined = join_report(report, rates)
    named = {problem.split(":")[0].split()[-1] for problem in joined.problems}

    assert "EURUSD" not in rows_by_pair(joined.rows)
    assert "EURUSD" in named


# --- the sign -----------------------------------------------------------------


def test_a_strengthening_base_is_a_positive_move(report: BiasReport) -> None:
    """Stated in the docstring and asserted here, because an inverted move is
    a plausible number in the right range and the evaluation built on it would
    report the model exactly backwards.

    EURUSD rising from 1.0850 to 1.1934 is the euro strengthening by ten per
    cent against the dollar, and the pair is quoted dollars per euro.
    """
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=10): {**BASE_RATES, "EURUSD": 1.1935},
    }

    row = rows_by_pair(join_report(report, rates).rows)["EURUSD"]

    assert row.move == pytest.approx((1.1935 / 1.0850) - 1.0)
    assert row.move > 0


def test_the_sign_is_the_same_one_the_spread_carries(report: BiasReport) -> None:
    """The move has to be comparable to the recorded bias without a translation.

    USDJPY rising is the dollar strengthening against the yen, and the dollar
    is the base. A join that read the yen leg as the base would give this the
    opposite sign from the spread that was recorded for the same pair.
    """
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=10): {**BASE_RATES, "USDJPY": 170.50},
    }

    row = rows_by_pair(join_report(report, rates).rows)["USDJPY"]

    assert row.move == pytest.approx((170.50 / 155.00) - 1.0)


def test_a_cross_is_built_from_its_two_dollar_legs(report: BiasReport) -> None:
    """Twenty-one of the twenty-eight pairs have no fixing of their own.

    The dollar legs are what FRED publishes, and `risk.convert_rate` already
    owns the resolution rule, pivot included. Re-deriving it here is how a
    cross ends up inverted while every number still looks like a rate.
    """
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=10): {**BASE_RATES, "EURUSD": 1.1935},
    }

    row = rows_by_pair(join_report(report, rates).rows)["EURGBP"]
    opened = 1.0850 / 1.2700
    closed = 1.1935 / 1.2700

    assert row.open_rate == pytest.approx(opened)
    assert row.close_rate == pytest.approx(closed)
    assert row.move == pytest.approx((closed / opened) - 1.0)


# --- absence ------------------------------------------------------------------


def test_a_history_too_short_for_the_horizon_yields_no_row(
    report: BiasReport,
) -> None:
    """A zero move is a finding and an unmeasurable one is not.

    Reported as zero, an outcome nobody can see yet would enter the sample as a
    flat result, and the record would read as more neutral than it is.
    """
    rates = rates_on(range(1, 6), drift=0.01)

    joined = join_report(
        report, rates, scoring=replace(ScoringConfig(), horizon_days=10)
    )

    assert joined.rows == ()
    assert any("horizon" in problem for problem in joined.problems)


def test_a_pair_with_no_derivable_rate_is_named_rather_than_dropped(
    report: BiasReport,
) -> None:
    """A gap that is not reported is a gap that flatters the coverage figure."""
    rates = rates_on(range(1, 15), drift=0.01, only=("EURUSD", "GBPUSD"))

    joined = join_report(report, rates)

    assert rows_by_pair(joined.rows).keys() == {"EURGBP", "EURUSD", "GBPUSD"}
    assert len(joined.problems) == len(report.pairs) - 3
    # Each line opens with the day and the pair it is about. Leaving the pair
    # to whatever the underlying error happens to mention makes the gap
    # unreadable at a glance and unsortable by anything downstream.
    named = {problem.split(":")[0].split()[-1] for problem in joined.problems}
    assert named == {
        bias.pair
        for bias in report.pairs
        if bias.pair not in {"EURGBP", "EURUSD", "GBPUSD"}
    }


def test_a_missing_session_is_not_treated_as_an_unchanged_price(
    report: BiasReport,
) -> None:
    """The last fixing inside the window is used, not the base rate carried
    forward from before it."""
    rates = {
        ASOF + timedelta(days=1): dict(BASE_RATES),
        ASOF + timedelta(days=4): {**BASE_RATES, "EURUSD": 1.2000},
        ASOF + timedelta(days=11): {**BASE_RATES, "EURUSD": 1.3000},
    }

    row = rows_by_pair(
        join_report(
            report, rates, scoring=replace(ScoringConfig(), horizon_days=10)
        ).rows
    )["EURUSD"]

    assert row.close_rate == pytest.approx(1.2000)


# --- what a row carries -------------------------------------------------------


def test_every_row_carries_the_digest_that_produced_the_bias(
    report: BiasReport,
) -> None:
    """Cross-sectional scores depend on the weights that made them, so a later
    reader has to be able to refuse to pool two runs that are not comparable."""
    rates = rates_on(range(1, 15), drift=0.01)

    rows = join_report(report, rates).rows

    assert {row.config_digest for row in rows} == {report.config_digest}


def test_the_pairs_the_engine_declined_to_back_get_rows_too(
    report: BiasReport,
) -> None:
    """They are the control group.

    If the pairs at no conviction perform like the backed ones, the conviction
    model is not separating anything, and that is the finding. Dropping them
    would leave only the cases the model liked.
    """
    rates = rates_on(range(1, 15), drift=0.01)

    rows = join_report(report, rates).rows
    declined = [row for row in rows if row.conviction is Conviction.NONE]

    assert len(declined) == sum(
        1 for pair in report.pairs if pair.conviction is Conviction.NONE
    )
    assert any(row.direction is Direction.NEUTRAL for row in rows)


def test_a_row_carries_the_bias_as_it_was_recorded(report: BiasReport) -> None:
    """Copied from the report rather than recomputed. The point of the join is
    to evaluate what was actually published."""
    rates = rates_on(range(1, 15), drift=0.01)

    row = rows_by_pair(join_report(report, rates).rows)["EURUSD"]
    recorded = next(pair for pair in report.pairs if pair.pair == "EURUSD")

    assert (row.direction, row.conviction) == (recorded.direction, recorded.conviction)
    assert row.spread == pytest.approx(recorded.spread)
    assert row.agreement == pytest.approx(recorded.agreement)
    assert row.asof == report.asof

    # Across every pair, not just this one. EURUSD's agreement is 1.0 in this
    # fixture and USDJPY's is 0.0, so a row hardcoding either would pass a
    # single-pair check.
    recorded_all = {pair.pair: pair for pair in report.pairs}
    for other in join_report(report, rates).rows:
        assert other.agreement == pytest.approx(recorded_all[other.pair].agreement)
        assert other.spread == pytest.approx(recorded_all[other.pair].spread)
        assert other.direction is recorded_all[other.pair].direction
        assert other.conviction is recorded_all[other.pair].conviction


@pytest.mark.parametrize("pair", ["EURGBP", "GBPUSD", "AUDUSD"])
def test_coverage_is_the_weaker_of_the_two_legs(report: BiasReport, pair: str) -> None:
    """A pair is only as well evidenced as its thinner side.

    Both sides are tested because in this fixture EURGBP's thin leg is the
    quote and GBPUSD's is the base, so a join reading one leg rather than the
    minimum passes on whichever of the two it happens to match. Four GBP pairs
    scored on 60% of their pillars would then be published at full coverage,
    and a reader filtering on evidence would admit them.
    """
    rates = rates_on(range(1, 15), drift=0.01)

    row = rows_by_pair(join_report(report, rates).rows)[pair]
    coverage = {score.currency: score.coverage for score in report.currencies}

    assert row.coverage == pytest.approx(
        min(coverage[row.pair[:3]], coverage[row.pair[3:]])
    )


def test_a_leg_the_report_did_not_score_yields_no_row(report: BiasReport) -> None:
    """Absent coverage is not full coverage.

    Defaulting it to 1.0 is the missing-conversion-rate defect in the standards
    table, one module along: a pair whose leg lost every pillar would publish
    as the best evidenced row on the page.
    """
    thinned = replace(
        report,
        currencies=tuple(
            score for score in report.currencies if score.currency != "GBP"
        ),
    )
    rates = rates_on(range(1, 15), drift=0.01)

    joined = join_report(thinned, rates)
    named = {problem.split(":")[0].split()[-1] for problem in joined.problems}

    assert not [row for row in joined.rows if "GBP" in row.pair]
    assert {pair.pair for pair in report.pairs if "GBP" in pair.pair} <= named


# --- a directory of reports ---------------------------------------------------


def written(directory: Path, report: BiasReport, asof: date) -> Path:
    return write_report(replace(report, asof=asof), directory)


def test_a_directory_joins_in_date_order(report: BiasReport, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    for day in (date(2026, 6, 24), date(2026, 6, 22), date(2026, 6, 26)):
        written(reports, report, day)
    rates = rates_on(range(-10, 20), drift=0.005, start=date(2026, 6, 22))

    rows = join_reports(reports, rates).rows

    assert [row.asof for row in rows] == sorted(row.asof for row in rows)
    assert {row.asof for row in rows} == {
        date(2026, 6, 22),
        date(2026, 6, 24),
        date(2026, 6, 26),
    }


def test_the_order_is_the_as_of_in_the_file_rather_than_the_name_on_it(
    report: BiasReport, tmp_path: Path
) -> None:
    """`load_report` reads the as-of from the body and nothing checks the name.

    `data/reports/` is committed and hand-editable, and the glob matches a name
    without zero padding, which sorts after a padded one from a later month. So
    file order and record order can disagree, and rows ordered by the directory
    listing would hand the evaluation a record that runs backwards in places.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 26))
    later = written(reports, report, date(2026, 6, 28))
    misnamed = reports / "bias-2026-06-20.json"
    misnamed.write_text(later.with_suffix(".json").read_text(encoding="utf-8"))
    later.with_suffix(".json").unlink()
    later.unlink()
    rates = rates_on(range(-10, 20), drift=0.005, start=date(2026, 6, 20))

    rows = join_reports(reports, rates).rows

    assert sorted(path.name for path in reports.glob("bias-*.json")) == [
        "bias-2026-06-20.json",
        "bias-2026-06-26.json",
    ]
    assert [row.asof for row in rows] == sorted(row.asof for row in rows)
    assert rows[0].asof == date(2026, 6, 26)


def test_rows_come_back_in_pair_order_within_a_day(
    report: BiasReport, tmp_path: Path
) -> None:
    """The other half of the ordering contract.

    `ALL_PAIRS` is alphabetical today and `CLAUDE.md` calls its ordering
    load-bearing, which is a reason to assert the contract rather than to lean
    on the coincidence.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    shuffled = replace(report, pairs=tuple(reversed(list(report.pairs))))
    write_report(replace(shuffled, asof=date(2026, 6, 24)), reports)
    rates = rates_on(range(-2, 20), drift=0.005, start=date(2026, 6, 24))

    rows = join_reports(reports, rates).rows

    assert [row.pair for row in rows] == sorted(row.pair for row in rows)


def test_the_horizon_reaches_every_report_in_a_directory(
    report: BiasReport, tmp_path: Path
) -> None:
    """The config has to reach the reports, not only the single-report call.

    An operator passing their own scoring config to the directory join and
    silently getting the packaged ten-day window on every row is the same
    defect as the literal, one function further out.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 24))
    rates = rates_on(range(1, 30), drift=0.005, start=date(2026, 6, 24))

    short = join_reports(
        reports, rates, scoring=replace(ScoringConfig(), horizon_days=3)
    )
    long = join_reports(
        reports, rates, scoring=replace(ScoringConfig(), horizon_days=20)
    )

    assert {row.closed_at for row in short.rows} == {date(2026, 6, 27)}
    assert {row.closed_at for row in long.rows} == {date(2026, 7, 14)}


def test_a_gap_inside_a_report_reaches_the_directory_level(
    report: BiasReport, tmp_path: Path
) -> None:
    """Otherwise a thin cross vanishes from the record once a day, for years.

    On a 250-day record that is 250 missing lines while every count downstream
    reads as complete, which is exactly what reporting a gap is for.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 24))
    rates = rates_on(
        range(-2, 20), drift=0.005, only=("EURUSD", "GBPUSD"), start=date(2026, 6, 24)
    )

    joined = join_reports(reports, rates)
    named = {problem.split(":")[0].split()[-1] for problem in joined.problems}

    assert "AUDCAD" in named
    assert len(joined.problems) == len(report.pairs) - 3


def test_each_report_is_joined_under_its_own_digest(
    report: BiasReport, tmp_path: Path
) -> None:
    """Two runs under different weights are not poolable, and the row is where
    a later reader finds that out."""
    reports = tmp_path / "reports"
    reports.mkdir()
    write_report(replace(report, asof=date(2026, 6, 24)), reports)
    write_report(
        replace(report, asof=date(2026, 6, 26), config_digest="ffffffffffff"), reports
    )
    rates = rates_on(range(-2, 20), drift=0.005, start=date(2026, 6, 24))

    rows = join_reports(reports, rates).rows
    digests = {row.asof: row.config_digest for row in rows}

    assert digests[date(2026, 6, 24)] == report.config_digest
    assert digests[date(2026, 6, 26)] == "ffffffffffff"


def test_a_file_the_sidecar_glob_does_not_name_is_left_alone(
    report: BiasReport, tmp_path: Path
) -> None:
    """`data/reports/` is committed and shared with whatever else lands there.

    A wider glob turns every stray JSON into a decode problem, which trains the
    reader to ignore the one list that exists to be read.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 24))
    (reports / "notes.json").write_text('{"note": "not a report"}')
    rates = rates_on(range(-2, 20), drift=0.005, start=date(2026, 6, 24))

    joined = join_reports(reports, rates)

    assert joined.rows
    assert not any("notes.json" in problem for problem in joined.problems)


def test_a_sidecar_that_cannot_be_opened_is_reported(
    report: BiasReport, tmp_path: Path
) -> None:
    """Unreadable and undecodable are the same answer to the record: that day
    is missing from it rather than absent from it."""
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 24))
    (reports / "bias-2026-06-25.json").mkdir()
    rates = rates_on(range(-2, 20), drift=0.005, start=date(2026, 6, 24))

    joined = join_reports(reports, rates)

    assert {row.asof for row in joined.rows} == {date(2026, 6, 24)}
    assert any("bias-2026-06-25.json" in problem for problem in joined.problems)


def test_a_report_that_cannot_be_decoded_is_reported_rather_than_skipped(
    report: BiasReport, tmp_path: Path
) -> None:
    """Silently dropping a day understates the record and flatters everything
    computed from it, and the day it drops is the day something went wrong."""
    reports = tmp_path / "reports"
    reports.mkdir()
    written(reports, report, date(2026, 6, 22))
    (reports / "bias-2026-06-23.json").write_text('{"asof": "2026-06-23"}')
    rates = rates_on(range(-10, 20), drift=0.005, start=date(2026, 6, 22))

    joined = join_reports(reports, rates)

    assert {row.asof for row in joined.rows} == {date(2026, 6, 22)}
    assert any("bias-2026-06-23.json" in problem for problem in joined.problems)


def test_an_empty_directory_is_an_empty_join_and_says_so(tmp_path: Path) -> None:
    """No reports yet is a fact about the record, not a failure."""
    reports = tmp_path / "reports"
    reports.mkdir()

    joined = join_reports(reports, {})

    assert joined.rows == ()
    assert any("no reports" in problem.lower() for problem in joined.problems)


def test_a_directory_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """Absent and empty are different answers, and a caller pointed at the
    wrong path should hear about it rather than read a clean empty record."""
    with pytest.raises((FileNotFoundError, NotADirectoryError, ValueError)):
        join_reports(tmp_path / "gone", {})


# --- what this is not ---------------------------------------------------------


def test_nothing_here_computes_a_hit_rate(report: BiasReport) -> None:
    """The join produces rows and stops. Averaging them is #286's, and the
    split exists so that a wrong join is visible rather than buried inside a
    statistic."""
    import fbe.evaluation as evaluation

    exported = set(evaluation.__all__)

    assert not {name for name in exported if "rate" in name.lower()}
    assert not {name for name in exported if "hit" in name.lower()}


def test_no_unmeasured_claim_appears_in_the_module(report: BiasReport) -> None:
    """These are inputs to an analysis, not an answer."""
    import fbe.evaluation as evaluation

    source = Path(evaluation.__file__).read_text(encoding="utf-8").lower()

    for word in ("backtested", "proven", "win rate", "edge over"):
        assert word not in source, word


def test_the_rates_given_are_not_altered(report: BiasReport) -> None:
    """It reads. A join that wrote back into its inputs would change what the
    next call sees."""
    rates = rates_on(range(1, 15), drift=0.01)
    before = {session: dict(row) for session, row in rates.items()}

    join_report(report, rates)

    assert rates == before


def test_a_rate_table_missing_the_window_entirely_is_not_an_exception(
    report: BiasReport,
) -> None:
    """A pair with no usable rate is a gap, not a crash: one unserved pair must
    not cost the other twenty-seven their rows."""
    rates = rates_on(range(1, 15), drift=0.01)

    joined = join_report(report, rates)

    assert joined.rows
    assert not isinstance(joined.problems, MissingRateError)
