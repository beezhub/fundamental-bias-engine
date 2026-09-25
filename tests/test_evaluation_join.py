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
from fbe.evaluation import ForwardRow, join_report, join_reports
from fbe.report import load_report, write_report
from fbe.risk import MissingRateError
from fbe.types import BiasReport, Conviction, Direction

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"

ASOF = date(2026, 6, 30)
"""The committed fixture's as-of date, and the line every test is about."""

MAJORS = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD")
"""The seven pairs FRED publishes. Every cross is derived from these."""

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
    assert any("AUDCAD" in problem for problem in joined.problems)


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


def test_coverage_is_the_weaker_of_the_two_legs(report: BiasReport) -> None:
    """A pair is only as well evidenced as its thinner side, and GBP in this
    fixture is scored on 60% of its pillars."""
    rates = rates_on(range(1, 15), drift=0.01)

    row = rows_by_pair(join_report(report, rates).rows)["EURGBP"]
    coverage = {score.currency: score.coverage for score in report.currencies}

    assert row.coverage == pytest.approx(min(coverage["EUR"], coverage["GBP"]))


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
