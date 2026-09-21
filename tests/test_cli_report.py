"""Tests for ``fbe report``.

The command's job is to leave two files on disk that a person can read next
week and a Phase 6 join can read next year. Three things it could get wrong
would not show up on the morning it happened:

- Writing only the Markdown. ``--compare last`` still finds a report, the
  what-changed section is empty forever, and every run reports success.
- Diffing against itself. A second run on one day comparing against its own
  earlier output reports the intraday change as the day's move.
- Going to the network. `refresh` is the only command allowed to be slow or to
  fail on a connection, and a report that refetched mid-session would not be
  reproducible from the cache it claims to have used.

`fbe.cli.collect`, the scorer and the bias layer are replaced in every test, so
no source is constructed and no socket is opened. Every test writes under
``tmp_path``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from fbe.cli import EXIT_UNUSABLE, app
from fbe.config import DataConfig, load_config
from fbe.datasources.collect import CollectionResult
from fbe.report import load_report, write_report
from fbe.types import (
    BiasReport,
    Conviction,
    CurrencyScore,
    Direction,
    Frequency,
    Observation,
    PairBias,
    PillarName,
    PillarScore,
    TradeIdea,
)

runner = CliRunner()

ASOF = date(2026, 9, 9)
YESTERDAY = date(2026, 9, 8)

OBSERVATION = Observation(
    indicator="yield_2y",
    currency="USD",
    value=4.25,
    period=date(2026, 9, 1),
    source="fixture",
    series_id="FIXTURE",
    unit="percent",
    frequency=Frequency.DAILY,
)


def score(currency: str, composite: float, rank: int) -> CurrencyScore:
    return CurrencyScore(
        currency=currency,
        composite=composite,
        pillars={
            PillarName.MONETARY: PillarScore(
                pillar=PillarName.MONETARY,
                currency=currency,
                raw=1.0,
                z=0.5,
                score=composite,
                weight=0.3,
                asof=ASOF,
            )
        },
        asof=ASOF,
        rank=rank,
        dispersion=0.3,
        coverage=0.8,
    )


def bias(
    pair: str = "EURUSD",
    spread: float = -1.4,
    direction: Direction = Direction.SHORT,
    conviction: Conviction = Conviction.MEDIUM,
) -> PairBias:
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=ASOF,
        base_score=-0.2,
        quote_score=1.2,
        agreement=0.7,
        tradeable=True,
        blockers=("cost:unchecked", "event:unchecked"),
    )


SCORES = (score("USD", 1.2, 1), score("EUR", -0.2, 2))
BIASES = (bias(),)


def run(
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    biases: Sequence[PairBias] = BIASES,
    scores: Sequence[CurrencyScore] = SCORES,
    observations: Sequence[Observation] = (OBSERVATION,),
) -> tuple[Result, dict[str, object]]:
    """Drive ``fbe report`` with collection, scoring and the bias layer replaced.

    `apply_filters` is replaced with the identity so the fixtures' own
    ``tradeable`` and ``blockers`` survive: the real function would overwrite
    both and the test would be asserting on `apply_filters`.
    """
    captured: dict[str, object] = {}

    def fake_collect(config_in: object, **kwargs: object) -> CollectionResult:
        captured["data_config"] = config_in
        captured["collect"] = kwargs
        return CollectionResult(observations=tuple(observations), outcomes=(), gaps={})

    def fake_score_currencies(
        observations_in: Sequence[Observation],
        pillars_in: Sequence[object],
        config_in: object,
        asof_in: date,
    ) -> Sequence[CurrencyScore]:
        captured["score"] = {"asof": asof_in}
        return tuple(scores)

    def fake_build_pair_biases(
        scores_in: Sequence[CurrencyScore],
        config_in: object,
        asof_in: date,
        event_horizon_guard: object = None,
    ) -> Sequence[PairBias]:
        return tuple(biases)

    def fake_apply_filters(
        bias_in: PairBias,
        scores_in: Mapping[str, CurrencyScore],
        config_in: object,
        asof_in: date,
        calendar_guard: object = None,
        cost_ratio: object = None,
    ) -> PairBias:
        # Recorded as well as replaced. The identity keeps the test off
        # `apply_filters`' own behaviour, and without the record nothing here
        # would notice the command dropping the call entirely, which strips
        # the blockers and the tradeable flag from every row on the page.
        filtered = captured.setdefault("filtered", [])
        assert isinstance(filtered, list)
        filtered.append((bias_in.pair, tuple(sorted(scores_in)), asof_in))
        return bias_in

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)
    monkeypatch.setattr("fbe.cli.build_pair_biases", fake_build_pair_biases)
    monkeypatch.setattr("fbe.cli.apply_filters", fake_apply_filters)
    return runner.invoke(app, ["report", "--asof", ASOF.isoformat(), *args]), captured


def previous_run(
    directory: Path,
    config_digest: str | None = None,
    pairs: Sequence[PairBias] = (),
) -> Path:
    """A report for the day before, written the way a real run writes one.

    The digest defaults to the one this invocation will compute, so the diff
    lands on its comparable branch. Passing a different one is how the
    not-comparable branch is reached, which is a separate test.
    """
    report = BiasReport(
        asof=YESTERDAY,
        generated_at=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
        currencies=(score("USD", 1.2, 1), score("EUR", -0.2, 2)),
        pairs=tuple(pairs) if pairs else (bias(),),
        # The same single idea this run's fixtures produce, so a test that
        # does not set out to move the shortlist counts no shortlist moves.
        shortlist=(TradeIdea(bias=bias()),),
        config_digest=(
            load_config().digest() if config_digest is None else config_digest
        ),
    )
    return write_report(report, directory)


# --- the two files -----------------------------------------------------------


def test_both_files_land_in_the_output_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert result.exit_code == 0
    assert (tmp_path / "bias-2026-09-09.md").exists()
    assert (tmp_path / "bias-2026-09-09.json").exists()


def test_the_sidecar_reads_back_as_the_run_that_was_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The join Phase 6 makes. If this file is not a `BiasReport`, the whole
    point of writing it is gone and nothing says so until Phase 6."""
    run(monkeypatch, "--out", str(tmp_path))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert loaded.asof == ASOF
    assert [row.pair for row in loaded.pairs] == ["EURUSD"]
    assert loaded.pairs[0].direction is Direction.SHORT


def test_the_report_carries_the_config_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run(monkeypatch, "--out", str(tmp_path))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert loaded.config_digest
    assert loaded.config_digest in (tmp_path / "bias-2026-09-09.md").read_text()


def test_the_default_directory_is_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--out`` is an override. The default has to be the configured reports
    directory and not a path assembled here, or moving the data tree leaves
    the reports behind."""
    monkeypatch.setenv("FBE_DATA_REPORTS_DIR", str(tmp_path / "configured"))

    result, _ = run(monkeypatch)

    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "configured" / "bias-2026-09-09.json").exists()


def test_the_output_directory_is_created_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "not" / "yet"

    result, _ = run(monkeypatch, "--out", str(target))

    assert result.exit_code == 0
    assert (target / "bias-2026-09-09.md").exists()


def test_the_run_names_the_file_it_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "bias-2026-09-09.md" in result.stdout


# --- --stdout ----------------------------------------------------------------


def test_stdout_prints_the_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _ = run(monkeypatch, "--out", str(tmp_path), "--stdout")

    assert "# FX fundamental bias, 2026-09-09" in result.stdout


def test_stdout_writes_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry run that quietly wrote the record would make the record depend on
    who looked at it.

    The exit code and the rendered heading are asserted alongside the empty
    directory, because a command that crashed also leaves nothing behind and
    the directory alone cannot tell the two apart.
    """
    result, _ = run(monkeypatch, "--out", str(tmp_path), "--stdout")

    assert result.exit_code == 0, result.stdout
    assert "# FX fundamental bias" in result.stdout
    assert list(tmp_path.iterdir()) == []


# --- --compare ---------------------------------------------------------------


def test_compare_last_diffs_against_the_previous_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    previous_run(tmp_path)

    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "bias-2026-09-08.json" in result.stdout
    assert "Against 2026-09-08" in (tmp_path / "bias-2026-09-09.md").read_text()


def test_compare_last_with_no_earlier_report_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "No baseline report" in result.stdout


def test_compare_none_skips_the_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    previous_run(tmp_path)

    result, _ = run(monkeypatch, "--out", str(tmp_path), "--compare", "none")

    assert "bias-2026-09-08" not in result.stdout
    assert "No baseline report" in (tmp_path / "bias-2026-09-09.md").read_text()


def test_compare_a_path_reads_that_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "archive"
    baseline = previous_run(elsewhere)

    result, _ = run(monkeypatch, "--out", str(tmp_path), "--compare", str(baseline))

    assert "bias-2026-09-08" in result.stdout


def test_compare_a_path_that_does_not_exist_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falling back to no baseline would print "first run" on the hundredth."""
    result, _ = run(
        monkeypatch, "--out", str(tmp_path), "--compare", str(tmp_path / "gone.json")
    )

    assert result.exit_code == 2


def test_a_rerun_on_the_same_day_does_not_diff_against_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second run of a morning would otherwise report the intraday change
    as the day's move, on a page whose whole subject is what moved overnight."""
    run(monkeypatch, "--out", str(tmp_path))

    result, _ = run(monkeypatch, "--out", str(tmp_path))

    compare_line = result.stdout.splitlines()[-1]
    assert "No baseline report" in compare_line
    assert "Compared against" not in result.stdout


def test_a_changed_digest_is_said_on_the_console_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one case where the numbers in that section are there to be read and
    must not be, and a reader who stopped at the console would not have seen
    it."""
    previous_run(tmp_path, config_digest="something-else")

    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "not comparable" in result.stdout


# --- the wire ----------------------------------------------------------------


def test_the_command_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Forced offline at the call, not inherited from the config. A rolled-over
    cache TTL refetching mid-session would make two runs at one --asof and one
    digest write two different records."""
    _, captured = run(monkeypatch, "--out", str(tmp_path))

    data_config = captured["data_config"]
    assert isinstance(data_config, DataConfig)
    assert data_config.offline is True


def test_the_run_date_reaches_the_scorer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, captured = run(monkeypatch, "--out", str(tmp_path))

    assert captured["score"] == {"asof": ASOF}


def test_a_future_asof_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_collect(config_in: object, **kwargs: object) -> CollectionResult:
        captured["called"] = True
        return CollectionResult(observations=(), outcomes=(), gaps={})

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    result = runner.invoke(
        app, ["report", "--asof", "2099-01-01", "--out", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "called" not in captured


def test_an_empty_cache_exits_unusable_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A report of a run that scored nothing is indistinguishable from a run
    that scored the universe and found no opinions.

    The printed reason is asserted as well as the code. `EXIT_UNUSABLE` is 1,
    which is also what an unhandled exception exits with, so the code on its
    own cannot tell a deliberate refusal from a crash.
    """
    result, _ = run(monkeypatch, "--out", str(tmp_path), observations=())

    assert result.exit_code == EXIT_UNUSABLE
    assert "No observations in the cache" in result.stdout
    assert list(tmp_path.iterdir()) == []


# --- what the page says ------------------------------------------------------


def test_the_run_says_no_calendar_was_consulted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`fbe.calendar_guard` is scaffolded, so the blackout filter and the
    24-hour conviction cap did not run. A report that did not say so would
    look exactly like one whose calendar was checked and found clear."""
    run(monkeypatch, "--out", str(tmp_path))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert any("No calendar was consulted" in line for line in loaded.warnings)
    assert loaded.events == ()


def test_the_shortlist_is_capped_by_the_risk_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap lives in `RiskConfig` and nowhere else. A second copy here would
    hand the trader more ideas than the risk rules permit to be open."""
    monkeypatch.setenv("FBE_RISK_MAX_CONCURRENT_POSITIONS", "1")
    crowded = (
        bias("EURUSD", -2.4, Direction.SHORT, Conviction.HIGH),
        bias("GBPJPY", 2.1, Direction.LONG, Conviction.HIGH),
    )

    result, _ = run(monkeypatch, "--out", str(tmp_path), biases=crowded)

    assert result.exit_code == 0, result.stdout
    assert len(load_report(tmp_path / "bias-2026-09-09.json").shortlist) == 1


def test_the_sidecar_is_plain_json_anything_can_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run(monkeypatch, "--out", str(tmp_path))

    payload = json.loads((tmp_path / "bias-2026-09-09.json").read_text())

    assert payload["asof"] == "2026-09-09"
    assert payload["pairs"][0]["direction"] == "short"


# --- the wire into the page --------------------------------------------------


def test_every_pair_goes_through_the_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dropping the call removes the blocker markers and the tradeable flag
    from every row, and the report still writes and still looks like one.

    The whole call is recorded, not only the pair. Handing `apply_filters` an
    empty score map loses the coverage blockers, and handing it today's date
    moves the event window off the run.
    """
    _, captured = run(monkeypatch, "--out", str(tmp_path))

    assert captured["filtered"] == [("EURUSD", ("EUR", "USD"), ASOF)]


def test_the_currency_ranking_reaches_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Section 1 of the report is the ranking. An empty one renders as a table
    with headings and no rows, which reads as a run that scored nothing."""
    run(monkeypatch, "--out", str(tmp_path))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert [row.currency for row in loaded.currencies] == ["USD", "EUR"]
    assert "+1.20" in (tmp_path / "bias-2026-09-09.md").read_text()


def test_the_config_reaches_the_renderer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without it the pillar columns silently fall back from configured-weight
    order to declaration order and the weights block disappears, while every
    number on the page stays plausible.
    """
    run(monkeypatch, "--out", str(tmp_path))

    rendered = (tmp_path / "bias-2026-09-09.md").read_text()

    # The weights block is the visible half, and it is the half that vanishes.
    # The column order is the half that matters more and it cannot be asserted
    # from the CLI, because the shipped defaults happen to put the heaviest
    # pillar first already, so weight order and declaration order agree.
    # `tests/test_report.py` pins the ordering on a config where they differ.
    assert "Pillar weights:" in rendered
    assert "monetary 0.30" in rendered


def test_the_shortlist_is_ranked_and_not_merely_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slicing the pair list would pass a length check and hand the owner the
    wrong trade.

    The high-conviction pair is second in the list the bias layer produced, so
    a slice takes the low-conviction one. This is the decision about which
    trade gets handed over, and a count is not enough to pin it.
    """
    monkeypatch.setenv("FBE_RISK_MAX_CONCURRENT_POSITIONS", "1")
    unranked = (
        bias("EURUSD", -0.9, Direction.SHORT, Conviction.LOW),
        bias("GBPJPY", 2.4, Direction.LONG, Conviction.HIGH),
    )

    result, _ = run(monkeypatch, "--out", str(tmp_path), biases=unranked)

    assert result.exit_code == 0, result.stdout
    picked = load_report(tmp_path / "bias-2026-09-09.json").shortlist
    assert [idea.bias.pair for idea in picked] == ["GBPJPY"]


def test_an_untradeable_pair_never_reaches_the_shortlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first rule `fbe.bias.shortlist` applies, and a slice applies none."""
    blocked = (
        replace(
            bias("EURUSD", -2.4, Direction.SHORT, Conviction.HIGH),
            tradeable=False,
            blockers=("cost",),
        ),
        bias("GBPJPY", 0.9, Direction.LONG, Conviction.LOW),
    )

    result, _ = run(monkeypatch, "--out", str(tmp_path), biases=blocked)

    assert result.exit_code == 0, result.stdout
    picked = load_report(tmp_path / "bias-2026-09-09.json").shortlist
    assert [idea.bias.pair for idea in picked] == ["GBPJPY"]


def test_the_run_stamp_carries_an_offset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 6 joins journal entries to this stamp. A naive one compared
    against an offset-aware one is wrong by the machine's offset, with nothing
    on the page to say so."""
    run(monkeypatch, "--out", str(tmp_path))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert loaded.generated_at.utcoffset() is not None
    assert "UTC" in (tmp_path / "bias-2026-09-09.md").read_text()


def test_the_warnings_say_which_pillars_could_not_score(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run on six pillars looks exactly like a run on seven, apart from the
    coverage column, unless the reason is carried up to the page."""
    unscored = replace(
        score("USD", 1.2, 1),
        pillars={
            PillarName.POSITIONING: PillarScore(
                pillar=PillarName.POSITIONING,
                currency="USD",
                raw=None,
                z=None,
                score=0.0,
                weight=0.0,
                asof=ASOF,
                notes="POSITIONING could not score: the CFTC source is scaffolded",
            )
        },
    )

    run(monkeypatch, "--out", str(tmp_path), scores=(unscored,))

    loaded = load_report(tmp_path / "bias-2026-09-09.json")

    assert any("CFTC source is scaffolded" in line for line in loaded.warnings)


# --- the console line --------------------------------------------------------


def test_the_console_counts_the_flips_and_the_shortlist_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The published example in ``docs/interfaces.md`` is this line, and the
    counts on it are what a reader uses to decide whether to open the file."""
    previous_run(tmp_path, pairs=(bias("EURUSD", 1.4, Direction.LONG),))

    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "1 direction flip, 0 shortlist changes." in result.stdout


def test_an_unchanged_run_counts_nothing_and_says_so_in_the_plural(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Zero is plural. "0 direction flip" is the sort of line a reader stops
    trusting, and the singular is what the published example uses for one."""
    previous_run(tmp_path)

    result, _ = run(monkeypatch, "--out", str(tmp_path))

    assert "0 direction flips, 0 shortlist changes." in result.stdout


def test_the_published_console_line_still_reproduces() -> None:
    """``docs/interfaces.md`` publishes this line. A published example that
    does not reproduce is worse than no example, so it is a fixture here.

    Built from a `ReportDiff` directly rather than from a run, because the
    example's counts are one flip and two shortlist moves and the point is the
    wording, not how a run reaches those numbers.
    """
    from fbe.cli import _compare_line
    from fbe.report import PairChange, ReportDiff

    diff = ReportDiff(
        previous_asof=YESTERDAY,
        current_asof=ASOF,
        pairs=(
            PairChange(
                pair="AUDJPY",
                previous_direction=Direction.LONG,
                current_direction=Direction.SHORT,
                previous_conviction=Conviction.MEDIUM,
                current_conviction=Conviction.MEDIUM,
                flipped=True,
            ),
        ),
        shortlist_added=("EURUSD",),
        shortlist_removed=(("GBPJPY", "blocked: coverage"),),
    )

    line = _compare_line(diff, Path("data/reports/bias-2026-09-08.json"))

    assert line == (
        "Compared against bias-2026-09-08.json: 1 direction flip, 2 shortlist changes."
    )
