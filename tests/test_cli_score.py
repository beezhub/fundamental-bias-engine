"""Tests for ``fbe score``.

This command renders and computes nothing, so the failures worth guarding are
the ones where a renderer quietly invents a number rather than reading one:

- A rank taken from the row's position instead of from `CurrencyScore.rank`,
  which is right until ``--currency`` narrows the rows and then silently
  promotes whatever is left to first.
- A composite, dispersion or coverage recomputed in the command, which would
  drift from the value the scorer recorded with nothing to show for it.
- A pillar with no data printed as ``+0.00``, which is the score the absent
  case genuinely carries and reads as a real neutral opinion.
- Coverage rounded up, so a run at 99.6% prints as complete.
- An empty run printing an empty table, which looks like a working engine with
  no opinions rather than a cold cache.

Every fixture builds its `CurrencyScore` values directly rather than scoring
anything, which is what lets the ranks, composites and coverages disagree with
each other on purpose: a renderer that derives any of them fails here and
cannot fail against a self-consistent fixture.

Nothing reaches the network. `fbe.cli.collect` is replaced in every test, so no
source is constructed and no socket is opened.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from typer.testing import CliRunner, Result

from fbe.cli import ABSENT_CELL, EXIT_OK, EXIT_UNUSABLE, PILLAR_ABBREVIATIONS, app
from fbe.config import load_config
from fbe.datasources.collect import CollectionResult
from fbe.types import CurrencyScore, Frequency, Observation, PillarName, PillarScore

runner = CliRunner()

ASOF = date(2026, 9, 9)

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
"""One observation, so `CollectionResult.usable` is true and the command gets
past the cold-cache guard. Its value never reaches an assertion, because the
scorer is replaced in every test."""


def pillar(
    name: PillarName,
    currency: str,
    score: float,
    *,
    absent: bool = False,
    notes: str = "",
) -> PillarScore:
    """One pillar's score, or its absence.

    Args:
        name: Which pillar.
        currency: ISO code.
        score: The value on the ``-3..+3`` band.
        absent: When true, ``raw`` and ``z`` are ``None``, which is how every
            consumer tells absence of evidence from evidence of neutrality.
            ``score`` still carries whatever was passed, so a test can prove the
            renderer reads the marker and not the number.
        notes: Human prose, such as the reason a pillar could not run.
    """
    return PillarScore(
        pillar=name,
        currency=currency,
        raw=None if absent else score,
        z=None if absent else score,
        score=score,
        weight=0.1,
        asof=ASOF,
        notes=notes,
    )


def currency_score(
    code: str,
    composite: float,
    rank: int,
    *,
    dispersion: float = 0.5,
    coverage: float = 1.0,
    pillars: Mapping[PillarName, PillarScore] | None = None,
) -> CurrencyScore:
    """One currency's row, with every field settable independently.

    ``rank`` is deliberately not derived from ``composite`` here. The whole
    point of several tests below is to hand the renderer a rank that the row
    order does not imply, so that a renderer numbering its own rows fails.
    """
    return CurrencyScore(
        currency=code,
        composite=composite,
        pillars=pillars if pillars is not None else {},
        asof=ASOF,
        rank=rank,
        dispersion=dispersion,
        coverage=coverage,
    )


def full_pillars(code: str, value: float) -> dict[PillarName, PillarScore]:
    """Every pillar present for one currency, all at the same score."""
    return {name: pillar(name, code, value) for name in PillarName}


UNIVERSE: tuple[CurrencyScore, ...] = (
    currency_score("USD", 1.42, 1, dispersion=0.61, coverage=1.0),
    currency_score("CHF", 0.77, 2, dispersion=0.44, coverage=1.0),
    currency_score("GBP", 0.31, 3, dispersion=1.02, coverage=0.86),
    currency_score("JPY", -1.18, 4, dispersion=1.31, coverage=0.71),
)
"""Four currencies with the interfaces example's own figures. Four rather than
eight because nothing here counts currencies, and a shorter fixture makes a
wrong row easier to see in a failure."""


def run(
    monkeypatch: pytest.MonkeyPatch,
    scores: Sequence[CurrencyScore],
    *args: str,
    observations: Sequence[Observation] = (OBSERVATION,),
) -> tuple[Result, dict[str, object]]:
    """Drive ``fbe score`` with the collection and the scorer replaced.

    Returns the Typer result and what the two replaced callables were handed,
    so a test can assert on the call rather than on the rendering where the
    criterion is about what reached the scorer.
    """
    captured: dict[str, object] = {}

    def fake_collect(_config: object, **kwargs: object) -> CollectionResult:
        captured["collect"] = kwargs
        return CollectionResult(observations=tuple(observations), outcomes=(), gaps={})

    def fake_score_currencies(
        observations_in: Sequence[Observation],
        pillars_in: Sequence[object],
        config_in: object,
        asof_in: date,
    ) -> Sequence[CurrencyScore]:
        captured["score"] = {
            "observations": tuple(observations_in),
            "pillars": tuple(pillars_in),
            "config": config_in,
            "asof": asof_in,
        }
        return tuple(scores)

    def fake_collect_capturing(config_in: object, **kwargs: object) -> CollectionResult:
        captured["data_config"] = config_in
        return fake_collect(config_in, **kwargs)

    monkeypatch.setattr("fbe.cli.collect", fake_collect_capturing)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)
    return runner.invoke(app, ["score", *args]), captured


def body(result: Result) -> list[str]:
    """The table's data rows, without the header lines or the trailing blanks."""
    return [line for line in result.stdout.splitlines() if line[:3].strip().isdigit()]


# --- the ranking ------------------------------------------------------------


def test_rows_are_printed_strongest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Descending by composite, which is the order the scorer already returns.

    Handed in shuffled, so a renderer relying on the scorer having sorted them
    fails rather than passing on the fixture's own ordering.
    """
    shuffled = (UNIVERSE[2], UNIVERSE[0], UNIVERSE[3], UNIVERSE[1])

    result, _ = run(monkeypatch, shuffled)

    assert result.exit_code == EXIT_OK
    assert [line.split()[1] for line in body(result)] == ["USD", "CHF", "GBP", "JPY"]


def test_the_printed_rank_is_the_field_not_the_row_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rank belongs to the universe, so the renderer may not number rows.

    These three currencies carry ranks 2, 4 and 7, which is what a filtered view
    of a larger universe looks like. A renderer counting rows prints 1, 2, 3 and
    tells the reader that JPY is the strongest currency in the G10.
    """
    scores = (
        currency_score("CHF", 0.77, 2),
        currency_score("GBP", 0.31, 4),
        currency_score("JPY", -1.18, 7),
    )

    result, _ = run(monkeypatch, scores)

    assert [line.split()[0] for line in body(result)] == ["2", "4", "7"]


def test_narrowing_the_rows_keeps_the_universe_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--currency`` is a row filter and not a re-ranking.

    A score is cross-sectional: a currency has no standing except relative to
    the others, so printing GBP as rank 1 because it is the first surviving row
    states something false about the run.
    """
    result, _ = run(monkeypatch, UNIVERSE, "--currency", "GBP", "--currency", "JPY")

    rows = body(result)
    assert len(rows) == 2
    assert [line.split()[0] for line in rows] == ["3", "4"]
    assert [line.split()[1] for line in rows] == ["GBP", "JPY"]


def test_a_currency_outside_the_universe_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter matching nothing must not print an empty table.

    ``--currency ZAR`` is a typo or a misunderstanding, and an empty table is
    the one answer that looks like a successful run with no opinions. The
    universe is the G10 and the message says so.
    """
    result, _ = run(monkeypatch, UNIVERSE, "--currency", "ZAR")

    assert result.exit_code != EXIT_OK
    assert "ZAR" in result.output


def test_the_currency_filter_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-C gbp`` is the same request as ``-C GBP``.

    Codes are upper case everywhere in this engine, and a shell user typing
    lower case should not meet the refusal above.
    """
    result, _ = run(monkeypatch, UNIVERSE, "-C", "gbp")

    assert result.exit_code == EXIT_OK
    assert [line.split()[1] for line in body(result)] == ["GBP"]


# --- the numbers are read, never derived ------------------------------------


def test_every_printed_number_is_the_field_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole contract of this command, in one fixture.

    The row is built so that every field contradicts what the others imply: a
    composite of ``+2.50`` beside seven pillars that all read ``-1.00``, a
    dispersion of ``0.00`` where those pillars are identical and so is the true
    dispersion, and a coverage of ``0.40`` on a currency holding every pillar.
    A renderer deriving any of the four prints a different number here, and a
    self-consistent fixture could never tell the two apart.
    """
    scores = (
        currency_score(
            "USD",
            2.50,
            6,
            dispersion=0.0,
            coverage=0.40,
            pillars=full_pillars("USD", -1.00),
        ),
    )

    result, _ = run(monkeypatch, scores, "--pillars")

    row = body(result)[0]
    assert row.split()[0] == "6"
    assert "+2.50" in row
    assert "0.00" in row
    assert "40%" in row
    assert row.count("-1.00") == len(PillarName)


def test_coverage_is_never_rounded_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """99.6% coverage is not 100%, and the difference is the whole column.

    Coverage below 1.0 means part of the pillar weight had no usable data, and
    it caps conviction downstream. Rounding to nearest prints a complete run,
    which is the one value a reader takes as needing no further thought.
    """
    scores = (
        currency_score("USD", 1.0, 1, coverage=0.996),
        currency_score("EUR", 0.5, 2, coverage=1.0),
    )

    result, _ = run(monkeypatch, scores)

    rows = body(result)
    assert "99%" in rows[0]
    assert "100%" in rows[1]


def test_exact_percentages_are_not_dragged_down_by_binary_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse of the test above, which a bare floor would fail.

    ``0.29 * 100`` is ``28.999999999999996`` in binary, so flooring the product
    prints 28% for a coverage that is exactly 29%. Understating is the safe
    direction and still wrong.
    """
    scores = tuple(
        currency_score(code, composite, rank, coverage=cov)
        for rank, (code, composite, cov) in enumerate(
            (("USD", 3.0, 0.29), ("EUR", 2.0, 0.58), ("GBP", 1.0, 0.87)), start=1
        )
    )

    result, _ = run(monkeypatch, scores)

    # Descending composites, so the printed order is the order written here.
    # Equal composites would sort by ISO code and the assertion would be about
    # the alphabet rather than about the percentages.
    assert [line.split()[-1] for line in body(result)] == ["29%", "58%", "87%"]


# --- the pillar columns -----------------------------------------------------


def test_pillar_columns_run_heaviest_weight_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order is the configured weights, not the enum and not the fixture.

    Asserted against the config the command resolved rather than a retyped
    list, so a weight change moves the columns and this test with it.
    """
    scores = (currency_score("USD", 1.0, 1, pillars=full_pillars("USD", 0.5)),)

    result, _ = run(monkeypatch, scores, "--pillars")

    weights = load_config(None).scoring.weights
    order = sorted(
        PillarName, key=lambda name: (-weights[name], list(PillarName).index(name))
    )
    header = next(line for line in result.stdout.splitlines() if "Composite" in line)
    labels = header.split()[5:]
    assert labels == [PILLAR_ABBREVIATIONS[name] for name in order]


def test_no_pillars_omits_the_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default, and the converse of the test above."""
    scores = (currency_score("USD", 1.0, 1, pillars=full_pillars("USD", 0.5)),)

    result, _ = run(monkeypatch, scores, "--no-pillars")

    header = next(line for line in result.stdout.splitlines() if "Composite" in line)
    assert header.split() == ["#", "CCY", "Composite", "Disp", "Cov"]


def test_a_pillar_with_no_data_is_not_printed_as_a_neutral_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent pillar carries a score of 0.0, and printing it says the wrong
    thing.

    ``missing_score`` returns ``0.0`` with ``raw`` and ``z`` both ``None``
    precisely so the absence survives to here. Rendering the number gives a
    column of ``+0.00`` that reads as seven pillars finding nothing to choose
    between, which is a finding rather than an outage.
    """
    scores = (
        currency_score(
            "USD",
            1.23,
            1,
            coverage=0.7,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.RISK: pillar(PillarName.RISK, "USD", 0.0, absent=True),
            },
        ),
    )

    result, _ = run(monkeypatch, scores, "--pillars")

    row = body(result)[0]
    assert "n/a" in row
    # The composite is 1.23 rather than 0.0 on purpose: with a zero composite
    # the row carries "+0.00" whatever the pillar column does, and this
    # assertion would pass against a renderer printing the absent score.
    assert "+0.00" not in row


def test_a_pillar_missing_from_the_mapping_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that is not there is the same fact as a key marked absent.

    `BasePillar.compute` returns every currency, but a pillar switched off for
    the run never appears at all, and the column still has to say something.
    """
    scores = (currency_score("USD", 0.0, 1, pillars={}),)

    result, _ = run(monkeypatch, scores, "--pillars")

    assert body(result)[0].count("n/a") == len(PillarName)


# --- the machine formats ----------------------------------------------------


def test_json_carries_the_same_numbers_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One run, two renderings, and they must not disagree.

    A formatter that rounds the table and not the JSON, or the other way round,
    hands a spreadsheet different numbers from the terminal for the same run.
    """
    table, _ = run(monkeypatch, UNIVERSE)
    payload, _ = run(monkeypatch, UNIVERSE, "--format", "json")

    rows = json.loads(payload.stdout)["currencies"]
    assert [row["currency"] for row in rows] == ["USD", "CHF", "GBP", "JPY"]
    for row, score in zip(rows, UNIVERSE, strict=True):
        assert row["composite"] == pytest.approx(score.composite)
        assert row["dispersion"] == pytest.approx(score.dispersion)
        assert row["coverage"] == pytest.approx(score.coverage)
        assert row["rank"] == score.rank
    assert f"{UNIVERSE[0].composite:+.2f}" in body(table)[0]


def test_csv_carries_the_same_numbers_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, through the other machine format."""
    payload, _ = run(monkeypatch, UNIVERSE, "--format", "csv")

    rows = list(csv.DictReader(io.StringIO(payload.stdout)))
    assert [row["currency"] for row in rows] == ["USD", "CHF", "GBP", "JPY"]
    for row, score in zip(rows, UNIVERSE, strict=True):
        assert float(row["composite"]) == pytest.approx(score.composite)
        assert float(row["dispersion"]) == pytest.approx(score.dispersion)
        assert float(row["coverage"]) == pytest.approx(score.coverage)
        assert int(row["rank"]) == score.rank


def test_the_machine_formats_carry_the_pillar_scores_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pillars`` is a request for data, not a table decoration.

    An absent pillar is ``null`` in JSON and empty in CSV rather than ``0.0``,
    for the same reason the table prints ``n/a``.
    """
    scores = (
        currency_score(
            "USD",
            1.0,
            1,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.RISK: pillar(PillarName.RISK, "USD", 0.0, absent=True),
            },
        ),
    )

    payload, _ = run(monkeypatch, scores, "--pillars", "--format", "json")

    row = json.loads(payload.stdout)["currencies"][0]
    assert row["pillars"][PillarName.MONETARY.value] == pytest.approx(0.5)
    assert row["pillars"][PillarName.RISK.value] is None


# --- the run itself ---------------------------------------------------------


def test_the_asof_reaches_the_scorer_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted on the call, because the printed date proves only the header.

    ``--asof`` is one of the two things that make a run reproducible, and a
    command that prints the requested date while scoring today would be wrong
    in the one way nobody would check.
    """
    _, captured = run(monkeypatch, UNIVERSE, "--asof", "2026-09-09")

    assert captured["score"]["asof"] == ASOF  # type: ignore[index]
    assert captured["collect"]["end"] == ASOF  # type: ignore[index]


def test_the_asof_defaults_to_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """The converse, so the test above is not passing on a hardcoded date."""
    _, captured = run(monkeypatch, UNIVERSE)

    assert captured["score"]["asof"] == date.today()  # type: ignore[index]


def test_the_header_ties_the_table_to_the_run_that_made_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asof date and the config digest, which pin a run between them.

    The digest is asserted against the config the command resolved rather than
    a literal, since a literal would only prove the header has twelve hex
    characters on it.
    """
    result, _ = run(monkeypatch, UNIVERSE, "--asof", "2026-09-09")

    header = result.stdout.splitlines()[0]
    assert "2026-09-09" in header
    assert load_config(None).digest() in header


def test_a_cold_cache_is_not_an_empty_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing collected is a broken run, not a run with no opinions.

    An empty table and exit 0 is the shape a script downstream reads as
    success, which is why the exit code is part of the contract.
    """
    empty, _ = run(monkeypatch, UNIVERSE, observations=())
    filled, _ = run(monkeypatch, UNIVERSE)

    assert empty.exit_code == EXIT_UNUSABLE
    assert body(empty) == []
    # Paired, because a command that raised on every path would satisfy the
    # two assertions above on its own. That is how this test passed against
    # the stub before it was written properly.
    assert filled.exit_code == EXIT_OK
    assert body(filled) != []


def test_a_cold_cache_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exit code is for the script and the line is for the person."""
    result, _ = run(monkeypatch, UNIVERSE, observations=())

    assert "refresh" in result.output.lower()


def test_the_scorer_gets_the_collected_observations_and_every_pillar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire from the collection into the scorer.

    A command that collected and then scored something else would print a table
    off an empty universe while the cache filled correctly beside it.
    """
    _, captured = run(monkeypatch, UNIVERSE)

    call = captured["score"]
    assert call["observations"] == (OBSERVATION,)  # type: ignore[index]
    assert len(call["pillars"]) == len(PillarName)  # type: ignore[index]


def test_a_pillar_that_failed_reaches_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scorer records a failed pillar rather than raising, so it must print.

    `score_currencies` catches a pillar that raises, gives every currency that
    pillar's absence, and puts the reason on the score's notes. If the command
    drops the notes, a run on six pillars is indistinguishable from a run on
    seven and only the coverage column hints at it.
    """
    reason = (
        "risk raised RuntimeError: vix feed empty; every currency scored without it"
    )
    scores = (
        currency_score(
            "USD",
            1.0,
            1,
            coverage=0.9,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.RISK: pillar(
                    PillarName.RISK, "USD", 0.0, absent=True, notes=reason
                ),
            },
        ),
    )

    result, _ = run(monkeypatch, scores)

    assert "vix feed empty" in result.output


def test_a_currency_with_no_coverage_still_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero coverage is a row with a warning, not a row that disappears.

    A currency dropped from the table reads as an absence of opportunity. It is
    an absence of data, and the two call for opposite responses.
    """
    scores = (
        currency_score("USD", 1.0, 1),
        currency_score(
            "JPY",
            0.0,
            2,
            coverage=0.0,
            pillars={
                name: pillar(name, "JPY", 0.0, absent=True, notes="no data for JPY")
                for name in PillarName
            },
        ),
    )

    result, _ = run(monkeypatch, scores)

    rows = body(result)
    assert [line.split()[1] for line in rows] == ["USD", "JPY"]
    assert "0%" in rows[1]
    assert "no data for JPY" in result.output


# --- the boundary this command must not cross -------------------------------


def test_the_command_derives_no_number_of_its_own() -> None:
    """`cli.py` may not import the scorer's arithmetic.

    `test_every_printed_number_is_the_field_it_came_from` covers the behaviour.
    This covers the temptation: `composite`, `coverage` and `dispersion` are
    exported by `fbe.scoring` and one import away, and a later change that
    recomputed a column would pass every behavioural test written before it.
    """
    import ast
    import inspect
    from pathlib import Path

    import fbe.cli as cli_module

    tree = ast.parse(Path(inspect.getfile(cli_module)).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert {"composite", "coverage", "dispersion"} & imported == set()


# --- what the review found the suite could not see --------------------------


def test_scoring_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetching is `refresh`'s job, and scoring reads the cache.

    ``docs/interfaces.md`` opens with the separation: refresh is the only
    command allowed to be slow or to fail on a connection, and everything
    downstream reads the cache so that scoring is instant and repeatable. A
    command passing the config through unchanged lets a rolled-over TTL refetch
    mid-session, so two runs at the same ``--asof`` and the same digest can
    print different tables with nothing on screen to explain it.
    """
    _, captured = run(monkeypatch, UNIVERSE)

    assert captured["data_config"].offline is True  # type: ignore[union-attr]


def test_a_universe_with_no_coverage_is_not_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every currency at zero coverage is the collapse that exit 1 is for.

    This is the live state of the tree: every pillar is scaffolded, so a real
    run prints eight composites of +0.00 and the rows say 0%. The rows are the
    right output and the issue asks for them. The exit code is what a script
    reads, and ``docs/interfaces.md`` reserves 1 for coverage collapsing, so a
    zero exit here reports eight equally strong currencies as a finding.
    """
    scores = tuple(
        currency_score(code, 0.0, rank, coverage=0.0)
        for rank, code in enumerate(("AUD", "CAD", "CHF", "EUR"), start=1)
    )

    result, _ = run(monkeypatch, scores)

    assert result.exit_code == EXIT_UNUSABLE
    # The rows still print. Hiding them would be the other half of the same
    # defect: an empty table that says nothing about why.
    assert len(body(result)) == len(scores)
    assert all("0%" in line for line in body(result))


def test_one_thin_currency_is_not_a_collapsed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse, so the test above is not passing on any zero it sees.

    A single currency with no data inside a working run is a thin currency, not
    a broken engine, and narrowing the rows to it does not change that. The
    exit code describes the run and the coverage column describes the currency.
    """
    scores = (
        currency_score("USD", 1.0, 1, coverage=1.0),
        currency_score("JPY", 0.0, 2, coverage=0.0),
    )

    both, _ = run(monkeypatch, scores)
    only_thin, _ = run(monkeypatch, scores, "--currency", "JPY")

    assert both.exit_code == EXIT_OK
    assert only_thin.exit_code == EXIT_OK


def test_a_note_from_a_pillar_that_scored_still_reaches_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notes are not only for failures, and filtering on absence drops the rest.

    `BasePillar.compute` puts two facts on the notes of pillars that did score,
    and its docstring says both must reach a reader on every run: which path
    `blend_divisor` took, and how many inputs were admitted on an assumed
    publication lag rather than a real release date. The first is the only
    thing that says whether two runs are on the same scale, so a renderer
    collecting notes only from absent pillars silently drops it.
    """
    scored = pillar(PillarName.MONETARY, "USD", 0.5)
    scores = (
        currency_score(
            "USD",
            1.0,
            1,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.MONETARY: pillar(
                    PillarName.MONETARY,
                    "USD",
                    0.5,
                    notes="monetary divisor path run_local",
                ),
            },
        ),
    )
    assert scored.z is not None

    result, _ = run(monkeypatch, scores)

    assert "run_local" in result.output


def test_csv_marks_an_absent_pillar_rather_than_writing_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of the absence rule the JSON test names and does not exercise.

    A spreadsheet reading ``0`` for a pillar that never ran averages it in with
    the ones that did, which is the same error as printing ``+0.00`` in the
    table and is harder to notice.
    """
    scores = (
        currency_score(
            "USD",
            1.0,
            1,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.RISK: pillar(PillarName.RISK, "USD", 0.0, absent=True),
            },
        ),
    )

    payload, _ = run(monkeypatch, scores, "--pillars", "--format", "csv")

    row = next(iter(csv.DictReader(io.StringIO(payload.stdout))))
    assert row[PillarName.MONETARY.value] == "0.5"
    assert row[PillarName.RISK.value] == ""


def test_csv_carries_the_run_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved CSV outlives the terminal it was printed in.

    The table puts the date and the digest in its header. A file of composites
    that cannot be tied to the weights that produced it is a file nobody can
    check later, and reproducibility is a stated requirement here rather than
    speculative generality.
    """
    payload, _ = run(monkeypatch, UNIVERSE, "--format", "csv", "--asof", "2026-09-09")

    rows = list(csv.DictReader(io.StringIO(payload.stdout)))
    assert all(row["asof"] == "2026-09-09" for row in rows)
    assert all(row["config_digest"] == load_config(None).digest() for row in rows)


def test_a_future_asof_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing has been published for a date that has not happened.

    Every series would be past its allowance, so the table would be eight
    composites of zero under a header confidently naming a date in 2099.
    ``refresh`` refuses a future ``--since`` for the same reason.
    """
    result, _ = run(monkeypatch, UNIVERSE, "--asof", "2099-01-01")

    assert result.exit_code != EXIT_OK
    assert "2099-01-01" in result.output


def test_an_unusable_lookback_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing validates ``scoring.lookback_years`` and this command reads it.

    ``refresh`` already turns the same failure into a message naming the
    setting. This command runs several times a day, so a traceback out of it is
    the worse of the two places to meet one.
    """
    monkeypatch.setenv("FBE_SCORING_LOOKBACK_YEARS", "-1")

    result, _ = run(monkeypatch, UNIVERSE)

    assert result.exit_code != EXIT_OK
    assert "lookback_years" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_absent_rank_is_marked_rather_than_numbered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rank`` is optional on the dataclass, and ``0`` is a plausible rank.

    `score_currencies` fills it on every row today, so this is a trap rather
    than a live defect. It is worth closing because ``0`` sorts above first
    place and reads as data, where the row already knows how to say ``n/a``.
    """
    scores = (currency_score("USD", 1.0, 1), currency_score("EUR", 0.5, None))  # type: ignore[arg-type]

    result, _ = run(monkeypatch, scores)

    printed = [line for line in result.stdout.splitlines() if "EUR" in line]
    assert printed
    assert "n/a" in printed[0]
    assert not printed[0].startswith("  0")


def test_a_pillar_that_genuinely_scored_zero_is_not_marked_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction this whole repository is built on, at the last mile.

    A score of ``0.0`` with ``z`` set is a real reading: `RiskPillar` emits
    exactly that in a calm market, where the regime is neither risk-on nor
    risk-off. A score of ``0.0`` with ``z`` of ``None`` is an outage. They are
    the same number and opposite facts, so a renderer keying on the number
    instead of the marker prints ``n/a`` over a genuine finding and tells the
    reader there is no data where there is.

    Found by a mutation that survived every other test here, because no fixture
    had a pillar honestly reading zero.
    """
    scores = (
        currency_score(
            "USD",
            1.0,
            1,
            pillars={
                **full_pillars("USD", 0.5),
                PillarName.RISK: pillar(PillarName.RISK, "USD", 0.0),
                PillarName.EXTERNAL: pillar(
                    PillarName.EXTERNAL, "USD", 0.0, absent=True
                ),
            },
        ),
    )

    result, _ = run(monkeypatch, scores, "--pillars")

    row = body(result)[0]
    assert "+0.00" in row
    assert row.count(ABSENT_CELL) == 1


def test_the_row_order_follows_the_rank_column_it_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order and the rank beside it cannot be allowed to disagree.

    "Strongest first, ties broken by ISO code" is stated in
    `fbe.scoring.score_currencies`. A renderer that re-derives it holds a second
    copy of that rule, and two copies of one rule drift: change the tie-break
    upstream and this table prints rank 3 above rank 2, with the column
    contradicting the order and nothing on screen saying which is right.

    The fixture puts the two in conflict on purpose, with rank 1 on the smaller
    composite. That is not a state the scorer produces today, which is why no
    other test here can tell a sort on the rank from a sort on the composite.
    """
    scores = (
        currency_score("USD", 0.50, 1),
        currency_score("EUR", 0.90, 2),
    )

    result, _ = run(monkeypatch, scores)

    rows = body(result)
    assert [line.split()[0] for line in rows] == ["1", "2"]
    assert [line.split()[1] for line in rows] == ["USD", "EUR"]
