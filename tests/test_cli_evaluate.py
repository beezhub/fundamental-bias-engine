"""`fbe evaluate` renders what the record says, including that it says nothing.

The command computes no statistic of its own: `evaluation.evaluate` does that
and `tests/test_evaluation_statistics.py` holds it to the arithmetic. These
tests are about what reaches the reader, which is where the criteria that are
about words rather than numbers live:

* that the output can say plainly that nothing separated from chance,
* that a figure below the evidence threshold is marked in words and not only by
  a count the reader has to notice,
* that a pooled mixture of config digests is named rather than passed over,
* that nothing printed claims an edge, a hit rate as a property of the model, or
  a backtested result,
* and that the three formats carry the same figures.

Nothing here reaches the network. The price source is replaced by a table in
most tests that get as far as pricing, and the ones that drive the real source
answer FRED through `respx`. The journal lives in ``tmp_path`` rather than at
`fbe.journal.JOURNAL_PATH`, which a fresh clone does not have and which is the
owner's private file where it does.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fbe.cli import EXIT_OK, EXIT_UNUSABLE, app
from fbe.datasources.fred import BASE_URL as FRED_BASE_URL
from fbe.datasources.fred import ENDPOINTS
from fbe.datasources.prices import FRED_SPOT_SERIES
from fbe.journal import append
from fbe.report import load_report, write_report

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"
ASOF = date(2026, 6, 30)

BASE_RATES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "AUDUSD": 0.6600,
    "NZDUSD": 0.6100,
    "USDJPY": 155.00,
    "USDCHF": 0.8900,
    "USDCAD": 1.3600,
}
"""One session's fixings, market convention, as `FRED_SPOT_SERIES` keys them."""


def test_the_fixture_prices_are_the_pairs_the_source_layer_serves() -> None:
    """Otherwise the whole file drives a command against prices nothing produces."""
    assert set(BASE_RATES) == set(FRED_SPOT_SERIES)


class _Prices:
    """A stand-in for `PricesSource` that answers from a table.

    Constructed with a drift so the answer for any pair is arithmetic rather
    than a number read off a fixture, and with a set of sessions so a market
    holiday is expressible: a session absent from the table answers ``None``,
    which is the source's own contract for "the market published no fixing".
    """

    drift = 0.0
    sessions: tuple[date, ...] = ()

    def __init__(self, _config: object) -> None:
        self.closed = False

    def spot(self, pair: str, on: date | None = None) -> float | None:
        if on not in self.sessions:
            return None
        offset = (on - ASOF).days
        strengthens_dollar = not pair.startswith(("EUR", "GBP", "AUD", "NZD"))
        factor = 1.0 + _Prices.drift * offset * (1.0 if strengthens_dollar else -1.0)
        return BASE_RATES[pair] * factor

    def spot_history(
        self, pair: str, start: date, end: date
    ) -> list[tuple[date, float]]:
        history = []
        for session in sorted(self.sessions):
            rate = self.spot(pair, on=session)
            if start <= session <= end and rate is not None:
                history.append((session, rate))
        return history

    def close(self) -> None:
        self.closed = True


def priced(monkeypatch: pytest.MonkeyPatch, *, drift: float, days: int = 20) -> None:
    """Replace the price source with one that prices every session after the as-of."""
    _Prices.drift = drift
    _Prices.sessions = tuple(ASOF + timedelta(days=n) for n in range(1, days + 1))
    monkeypatch.setattr("fbe.cli.PricesSource", _Prices)


def reports(tmp_path: Path, *, copies: int = 1, digest: str | None = None) -> Path:
    """Write ``copies`` dated reports into a directory and return it.

    Each copy is the committed fixture moved one day later, so a run of several
    mornings exists without a second fixture to keep in step with the first.
    """
    directory = tmp_path / "reports"
    directory.mkdir(exist_ok=True)
    original = load_report(FIXTURE)
    for offset in range(copies):
        report = original
        if offset or digest is not None:
            from dataclasses import replace

            report = replace(
                original,
                asof=ASOF + timedelta(days=offset),
                config_digest=(
                    digest
                    if digest is not None and offset % 2
                    else original.config_digest
                ),
            )
        write_report(report, directory)
    return directory


def run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    drift: float = 0.01,
    copies: int = 1,
    digest: str | None = None,
) -> object:
    """Drive the command against a written record and a priced window."""
    priced(monkeypatch, drift=drift)
    directory = reports(tmp_path, copies=copies, digest=digest)
    return runner.invoke(
        app,
        [
            "evaluate",
            "--reports",
            str(directory),
            "--journal",
            str(tmp_path / "trades.jsonl"),
            *args,
        ],
    )


# ----------------------------------------------------------------------
# The answer the command exists to be able to give
# ----------------------------------------------------------------------


def test_a_record_that_separates_nothing_says_so_in_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5 at the surface, where the reader meets it.

    One morning of 28 pairs is far under the evidence threshold, so nothing can
    be named as separating whatever the moves did. The output has to say that
    plainly rather than printing tables and leaving the reader to conclude it.
    """
    result = run(tmp_path, monkeypatch)

    assert result.exit_code == EXIT_OK
    assert "No group separates from chance" in result.stdout
    assert "That is a result, not a missing answer." in result.stdout


def test_the_figures_are_marked_as_a_record_rather_than_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6, in words and in the rows.

    Both, because a caveat twenty lines above a table is a caveat a reader
    scanning one row has already passed.
    """
    result = run(tmp_path, monkeypatch)

    assert "not evidence about what will" in result.stdout
    assert "record only" in result.stdout


def test_five_mornings_in_a_row_are_not_printed_as_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five daily reports are 140 rows and one independent window.

    The drift follows each pair's dollar leg the whole way, so a row count
    would clear thirty in every bucket with an interval above a half. The output
    has to say the record holds one window and name nothing as separating.
    """
    result = run(tmp_path, monkeypatch, copies=5)

    assert result.exit_code == EXIT_OK
    assert "from 1 independent windows" in result.stdout
    assert "Above chance" not in result.stdout
    assert "No group separates from chance" in result.stdout


def test_every_rate_is_printed_beside_its_interval_and_its_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 2's "beside the point estimate rather than instead of it".

    Asserted on the shape of the rendered rows rather than on one value: every
    data row carries a count, a percentage and a range of two more.
    """
    result = run(tmp_path, monkeypatch)

    rows = [
        line
        for line in result.stdout.splitlines()
        if re.search(r"\s\d+\s+\d+\.\d%\s+\d+\.\d% to \s*\d+\.\d%", line)
    ]
    assert rows, result.stdout
    assert all("%" in row for row in rows)


def test_no_price_at_or_before_the_as_of_is_ever_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The look-ahead line, enforced at the command as well as in the join.

    A report is built from observations released on or before its as-of, so a
    fixing dated that day is one the run could have been looking at. The command
    assembles the rate table itself, and a table that reached back over the line
    would hand the join prices it is supposed to refuse. Asserted on what the
    source was asked for, not on what came back: the join's own guard would hide
    a command that asked too early.
    """
    asked: list[date] = []

    class _Recording(_Prices):
        def spot_history(
            self, pair: str, start: date, end: date
        ) -> list[tuple[date, float]]:
            asked.append(start)
            return super().spot_history(pair, start, end)

    _Prices.drift = 0.01
    _Prices.sessions = tuple(ASOF + timedelta(days=n) for n in range(1, 21))
    monkeypatch.setattr("fbe.cli.PricesSource", _Recording)
    directory = reports(tmp_path)

    runner.invoke(app, ["evaluate", "--reports", str(directory)])

    assert asked
    assert min(asked) > ASOF


# ----------------------------------------------------------------------
# What must not be claimed
# ----------------------------------------------------------------------


def test_nothing_printed_claims_an_edge_or_a_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 8, scanned over the rendered output in all three formats.

    The standing instruction in `CLAUDE.md` is that no file may claim a
    backtested edge that has not been measured, and this is the one command
    whose output a reader would most readily take for one.
    """
    forbidden = re.compile(r"\b(proven|backtest(ed)?|edge|win rate)\b", re.IGNORECASE)

    for arguments in ((), ("--format", "json"), ("--format", "csv")):
        rendered = run(tmp_path, monkeypatch, *arguments).stdout
        assert not forbidden.search(rendered), (arguments, rendered)


def test_the_output_says_the_figures_describe_this_record_and_not_a_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A separating record must still not read as a prediction.

    Twenty mornings of the same fixture with a drift that always follows the
    spread gives enough observations to clear the threshold, which is the only
    state in which the command names anything, and is where a claim would be
    easiest to make by accident.
    """
    result = run(tmp_path, monkeypatch, copies=20)

    if "Above chance" in result.stdout:
        assert "It is not a forecast." in result.stdout
    else:
        assert "No group separates from chance" in result.stdout


# ----------------------------------------------------------------------
# The three formats carry the same figures
# ----------------------------------------------------------------------


def test_json_and_csv_carry_the_same_figures_as_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9. A format that drops a column is a format nobody can check.

    Compared on the data rather than on the rendering: every bucket in the JSON
    appears in the CSV with the same count, the same hit rate and the same
    interval.
    """
    as_json = json.loads(run(tmp_path, monkeypatch, "--format", "json").stdout)
    rendered = run(tmp_path, monkeypatch, "--format", "csv").stdout
    as_csv = list(csv.DictReader(io.StringIO(rendered)))

    from_json = {
        (grouping.removeprefix("by_"), entry["label"]): entry
        for grouping in ("by_conviction", "by_direction", "by_agreement")
        for entry in as_json[grouping]
    }
    assert from_json
    assert len(as_csv) == len(from_json)
    for row in as_csv:
        entry = from_json[(row["grouping"], row["label"])]
        assert int(row["observations"]) == entry["observations"]
        assert float(row["hit_rate"]) == pytest.approx(entry["hit_rate"], abs=5e-5)
        assert float(row["hit_rate_low"]) == pytest.approx(
            entry["hit_rate_low"], abs=5e-5
        )


def test_the_json_carries_the_conclusion_and_not_only_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer piping this should not have to re-derive what separated."""
    payload = json.loads(run(tmp_path, monkeypatch, "--format", "json").stdout)

    assert payload["shows_no_separation"] is True
    assert payload["separating"] == []
    assert payload["rows"] > 0


# ----------------------------------------------------------------------
# The digest mixture
# ----------------------------------------------------------------------


def test_a_pooled_mixture_of_digests_is_named_at_the_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7, in its second form: pooled, and the mixture stated.

    Splitting a record this young by digest leaves every bucket below the point
    of being reportable, so the figures are pooled and the caveat is loud.
    """
    result = run(tmp_path, monkeypatch, copies=4, digest="other-digest")

    assert "config digests" in result.stdout
    assert "not strictly comparable" in result.stdout


def test_one_digest_prints_no_mixture_caveat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caveat on every run is a caveat nobody reads."""
    result = run(tmp_path, monkeypatch)

    assert "config digests" not in result.stdout


# ----------------------------------------------------------------------
# The journal split
# ----------------------------------------------------------------------


def test_the_journal_split_is_printed_when_the_journal_has_closed_trades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4 at the surface, read from a journal under ``tmp_path``.

    Never `fbe.journal.JOURNAL_PATH`: on the owner's machine that is a private
    financial record, and `CLAUDE.md` keeps it out of the repository for that
    reason.
    """
    from tests.test_evaluation_statistics import trade

    path = tmp_path / "trades.jsonl"
    append(trade(aligned=True, r_multiple=1.4, index=1), path)
    append(trade(aligned=False, r_multiple=-1.0, index=2), path)

    result = run(tmp_path, monkeypatch)

    assert "traded with the bias" in result.stdout
    assert "traded against the bias" in result.stdout


def test_an_absent_journal_leaves_the_split_out_rather_than_printing_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case on a fresh clone, where `data/journal/` does not exist."""
    result = run(tmp_path, monkeypatch)

    assert "traded with the bias" not in result.stdout
    assert "By conviction" in result.stdout


# ----------------------------------------------------------------------
# What cannot be evaluated
# ----------------------------------------------------------------------


def test_a_missing_reports_directory_is_named_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong path and an empty record are different answers."""
    priced(monkeypatch, drift=0.01)

    result = runner.invoke(app, ["evaluate", "--reports", str(tmp_path / "nowhere")])

    assert result.exit_code == EXIT_UNUSABLE
    assert "No reports directory" in result.stderr


def test_a_window_that_cannot_be_priced_is_refused_rather_than_reported_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zeros from an unpriceable window read as a model that called nothing right.

    The reasons go to stderr and the exit code is 1, so a script cannot mistake
    "the newest reports are younger than the horizon" for a measured result.
    """
    _Prices.drift = 0.0
    _Prices.sessions = ()
    monkeypatch.setattr("fbe.cli.PricesSource", _Prices)
    directory = reports(tmp_path)

    result = runner.invoke(app, ["evaluate", "--reports", str(directory)])

    assert result.exit_code == EXIT_UNUSABLE
    assert "nothing to evaluate yet" in result.stderr


def test_a_gap_in_the_record_is_reported_rather_than_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair with no row is a gap, and a gap that is not named flatters the rest.

    The problems go to stderr so that a redirected JSON or CSV stays machine
    readable while the reader still sees them.
    """
    priced(monkeypatch, drift=0.01)
    directory = reports(tmp_path)
    (directory / "bias-2026-06-29.json").write_text("{ not json", encoding="utf-8")

    result = runner.invoke(
        app, ["evaluate", "--reports", str(directory), "--format", "json"]
    )

    assert result.exit_code == EXIT_OK
    json.loads(result.stdout)
    assert "2026-06-29" in result.stderr


# ----------------------------------------------------------------------
# The real price source, behind a mocked FRED
# ----------------------------------------------------------------------

OBSERVATIONS_URL = f"{FRED_BASE_URL}{ENDPOINTS['observations']}"
SERIES_PAIRS = {series: pair for pair, series in FRED_SPOT_SERIES.items()}


def _fred_answers(request: httpx.Request) -> httpx.Response:
    """Answer one observations request with a fixing on every weekday it spans.

    Built from the request's own window, so a command that asked for the wrong
    dates gets the wrong dates back rather than a fixture that hides it.
    """
    params = request.url.params
    pair = SERIES_PAIRS[params["series_id"]]
    start = date.fromisoformat(params["observation_start"])
    end = date.fromisoformat(params["observation_end"])
    observations = []
    session = start
    while session <= end:
        if session.weekday() < 5:
            observations.append(
                {"date": session.isoformat(), "value": f"{BASE_RATES[pair]:.4f}"}
            )
        session += timedelta(days=1)
    return httpx.Response(200, json={"observations": observations})


def _real_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the real source at a cold cache under ``tmp_path``, with a key set."""
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    monkeypatch.setenv("FBE_DATA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("fbe.datasources.base.time.sleep", lambda seconds: None)


@respx.mock
def test_the_real_source_is_asked_once_per_series_over_the_whole_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seven requests for the whole record, not one per pair per calendar day.

    Asking day by day sends a request for every weekend and holiday, and with
    each window keyed into the cache on its own, an offline run misses on
    nearly all of them. One request per series over the whole window is what
    the cache can answer and what FRED can serve without a throttle.
    """
    _real_source(tmp_path, monkeypatch)
    route = respx.get(OBSERVATIONS_URL).mock(side_effect=_fred_answers)
    directory = reports(tmp_path, copies=3)

    result = runner.invoke(app, ["evaluate", "--reports", str(directory)])

    assert result.exit_code == EXIT_OK, result.output
    asked = [call.request.url.params["series_id"] for call in route.calls]
    assert sorted(asked) == sorted(FRED_SPOT_SERIES.values())
    for call in route.calls:
        start = call.request.url.params["observation_start"]
        assert date.fromisoformat(start) > ASOF


@respx.mock
def test_no_price_dated_after_today_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report younger than the horizon asks up to today and no further.

    A window reaching into the future asks FRED for sessions that have not
    happened, and on an offline run looks for a cache entry nothing ever wrote.
    """
    from dataclasses import replace

    _real_source(tmp_path, monkeypatch)
    route = respx.get(OBSERVATIONS_URL).mock(side_effect=_fred_answers)
    directory = tmp_path / "reports"
    directory.mkdir()
    recent = date.today() - timedelta(days=3)
    write_report(replace(load_report(FIXTURE), asof=recent), directory)

    runner.invoke(app, ["evaluate", "--reports", str(directory)])

    assert route.calls
    for call in route.calls:
        end = call.request.url.params["observation_end"]
        assert date.fromisoformat(end) <= date.today()


@respx.mock
def test_a_price_fetch_that_fails_is_named_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead FRED is a fact about the run, and it reaches the reader as a line.

    Not a traceback, and not an empty evaluation: zeros would read as a model
    that called nothing right.
    """
    _real_source(tmp_path, monkeypatch)
    respx.get(OBSERVATIONS_URL).mock(return_value=httpx.Response(503))
    directory = reports(tmp_path)

    result = runner.invoke(app, ["evaluate", "--reports", str(directory)])

    assert result.exit_code == EXIT_UNUSABLE
    assert isinstance(result.exception, SystemExit)
    assert "Could not read the forward prices" in result.stderr


def test_an_offline_run_with_a_cold_cache_is_named_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The global ``--offline`` reads the cache, and a cold one is said so."""
    _real_source(tmp_path, monkeypatch)
    directory = reports(tmp_path)

    result = runner.invoke(app, ["--offline", "evaluate", "--reports", str(directory)])

    assert result.exit_code == EXIT_UNUSABLE
    assert isinstance(result.exception, SystemExit)
    assert "Could not read the forward prices" in result.stderr
