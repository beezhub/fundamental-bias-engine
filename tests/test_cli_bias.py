"""Tests for ``fbe bias``.

This command renders and computes nothing. Every number and every marker on a
printed row is read off a `fbe.types.PairBias`, so the failures worth guarding
are the ones where the renderer derives a field it was handed:

- A direction taken from the sign of the spread. Right until a pair the model
  refused to back carries `Direction.NEUTRAL` against a wide spread, and then
  the table argues with the engine about what the engine thought.
- A conviction taken from the width of the spread, which skips every reason
  `conviction_for` demotes a pair: thin coverage, dispersed pillars, a release
  inside the horizon.
- A pair removed by a filter and dropped rather than listed. A pair missing
  from the table is indistinguishable from a pair the engine never scored,
  which is why the roadmap's open question 10 was answered "show it".
- A non-blocking marker such as ``event:unchecked`` printed nowhere. An offline
  run has no calendar, and a marker that is not rendered does not exist.
- The ranked list sorted on the signed spread, which puts every short pair
  below every long one and buries the widest disagreement in the run.

Every fixture builds its `PairBias` values directly, and several build them
inconsistent on purpose: a direction that does not match the sign of its
spread, a conviction that does not match its width. `build_pair_biases` cannot
produce those, which is the point. A renderer deriving either field passes
against a self-consistent fixture and fails here.

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

from fbe.cli import EXIT_OK, EXIT_UNUSABLE, app
from fbe.config import load_config
from fbe.datasources.collect import CollectionResult
from fbe.types import (
    Conviction,
    CurrencyScore,
    Direction,
    Frequency,
    Observation,
    PairBias,
)
from fbe.universe import ALL_PAIRS, MAJORS

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
scorer and the bias layer are both replaced in every test."""


def pair_bias(
    pair: str,
    spread: float,
    *,
    direction: Direction = Direction.LONG,
    conviction: Conviction = Conviction.HIGH,
    agreement: float = 0.86,
    tradeable: bool = True,
    blockers: Sequence[str] = (),
    base_score: float | None = None,
    quote_score: float | None = None,
) -> PairBias:
    """One row, with every field settable independently.

    ``direction`` and ``conviction`` default to values a caller can override
    into disagreement with ``spread``. That is deliberate and it is what makes
    several tests here able to fail: `build_pair_biases` forces direction to
    `Direction.NEUTRAL` whenever conviction is `Conviction.NONE`, and derives
    direction from the sign, so a renderer doing either derivation is correct
    on every fixture that is internally consistent.

    ``base_score`` and ``quote_score`` default to values that deliberately do
    **not** reconstruct the spread. In a real run ``spread`` is
    ``base_score - quote_score``, and a fixture honouring that lets a renderer
    print ``base_score - spread`` in the quote column and pass every test in
    this file. The three numbers are independent here so that derivation
    fails, which is the same reason `tests/test_cli_score.py` gives its pillar
    helper three unequal numbers.
    """
    base = base_score if base_score is not None else spread * 0.75 + 0.11
    quote = quote_score if quote_score is not None else -spread * 0.2 + 0.07
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=ASOF,
        base_score=base,
        quote_score=quote,
        agreement=agreement,
        tradeable=tradeable,
        blockers=tuple(blockers),
    )


def run(
    monkeypatch: pytest.MonkeyPatch,
    biases: Sequence[PairBias],
    *args: str,
    observations: Sequence[Observation] = (OBSERVATION,),
) -> tuple[Result, dict[str, object]]:
    """Drive ``fbe bias`` with the collection, the scorer and the bias layer replaced.

    `apply_filters` is replaced with the identity, not with a fake that sets
    something. The fixtures arrive with ``tradeable`` and ``blockers`` already
    set to whatever the test needs, and the real function would overwrite both
    from its own checks, which would mean the test was asserting on
    `apply_filters` rather than on the rendering.

    Returns the Typer result and what the replaced callables were handed, so a
    test can assert on the call where the criterion is about what reached the
    bias layer rather than about what was printed.
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
        return ()

    def fake_build_pair_biases(
        scores_in: Sequence[CurrencyScore],
        config_in: object,
        asof_in: date,
        event_horizon_guard: object = None,
    ) -> Sequence[PairBias]:
        captured["build"] = {"asof": asof_in, "scores": tuple(scores_in)}
        return tuple(biases)

    def fake_apply_filters(
        bias_in: PairBias,
        scores_in: Mapping[str, CurrencyScore],
        config_in: object,
        asof_in: date,
        calendar_guard: object = None,
        cost_ratio: object = None,
    ) -> PairBias:
        captured.setdefault("filtered", [])
        assert isinstance(captured["filtered"], list)
        captured["filtered"].append(bias_in.pair)
        return bias_in

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)
    monkeypatch.setattr("fbe.cli.build_pair_biases", fake_build_pair_biases)
    monkeypatch.setattr("fbe.cli.apply_filters", fake_apply_filters)
    return runner.invoke(app, ["bias", *args]), captured


def rows(result: Result) -> list[str]:
    """The table's data rows: the lines beginning with a known pair code."""
    return [
        line
        for line in result.stdout.splitlines()
        if line[:6] in ALL_PAIRS and not line.startswith(" ")
    ]


def printed_pairs(result: Result) -> list[str]:
    """The pairs of the ranked table, in printed order."""
    return [line[:6] for line in rows(result)]


def hidden_block(result: Result) -> list[str]:
    """The indented lines below the hidden-pairs heading."""
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if "hidden by the filters" in line:
            return [rest for rest in lines[index + 1 :] if rest.startswith("  ")]
    return []


def hidden_heading(result: Result) -> str:
    """The hidden-pairs heading, or the empty string when there is none."""
    for line in result.stdout.splitlines():
        if "hidden by the filters" in line:
            return line
    return ""


# The fixture the ordering tests use. Widths deliberately out of pair-name
# order and out of signed order, so a sort on either fails.
SPREADS: tuple[tuple[str, float], ...] = (
    ("AUDCAD", -2.48),
    ("EURUSD", 1.11),
    ("GBPUSD", -0.65),
    ("USDJPY", 2.60),
    ("NZDUSD", -1.47),
)

MIXED: tuple[PairBias, ...] = tuple(pair_bias(pair, spread) for pair, spread in SPREADS)


def test_the_ranked_list_sorts_by_the_width_of_the_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widest disagreement first, whichever way it points.

    The fixture holds both signs, so a sort on the signed value puts every
    short pair below every long one and buries the widest spread in the run.
    """
    result, _ = run(monkeypatch, MIXED)

    assert result.exit_code == EXIT_OK
    assert printed_pairs(result) == [
        "USDJPY",
        "AUDCAD",
        "NZDUSD",
        "EURUSD",
        "GBPUSD",
    ]


def test_every_pair_the_bias_layer_returned_is_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 28 by default, checked against the universe rather than a retyped list."""
    universe = tuple(
        pair_bias(pair, 0.5 + index * 0.01) for index, pair in enumerate(ALL_PAIRS)
    )

    result, _ = run(monkeypatch, universe)

    assert sorted(printed_pairs(result)) == sorted(ALL_PAIRS)
    assert len(printed_pairs(result)) == 28


def test_majors_narrows_to_the_universes_own_major_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--majors`` keeps exactly `fbe.universe.MAJORS`, and drops the rest."""
    universe = tuple(
        pair_bias(pair, 0.5 + index * 0.01) for index, pair in enumerate(ALL_PAIRS)
    )

    result, _ = run(monkeypatch, universe, "--majors")

    assert sorted(printed_pairs(result)) == sorted(MAJORS)


def test_all_pairs_is_the_default_and_can_be_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--all-pairs`` is the other half of the flag and prints the full set."""
    universe = tuple(
        pair_bias(pair, 0.5 + index * 0.01) for index, pair in enumerate(ALL_PAIRS)
    )

    result, _ = run(monkeypatch, universe, "--all-pairs")

    assert sorted(printed_pairs(result)) == sorted(ALL_PAIRS)


CONVICTIONS: tuple[PairBias, ...] = (
    pair_bias("USDJPY", 2.60, conviction=Conviction.HIGH),
    pair_bias("EURUSD", 2.31, conviction=Conviction.MEDIUM),
    pair_bias("AUDUSD", 2.10, conviction=Conviction.LOW),
    pair_bias("NZDUSD", 1.90, conviction=Conviction.NONE, direction=Direction.NEUTRAL),
)
"""One pair at each conviction, widest first, so a filter that drops by
position rather than by level is visible in the order."""


def test_min_conviction_medium_keeps_medium_and_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary is inclusive at the level named and exclusive below it."""
    result, _ = run(monkeypatch, CONVICTIONS, "--min-conviction", "medium")

    assert printed_pairs(result) == ["USDJPY", "EURUSD"]


def test_min_conviction_low_keeps_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """One step down admits the low pair and still refuses the unbacked one."""
    result, _ = run(monkeypatch, CONVICTIONS, "--min-conviction", "low")

    assert printed_pairs(result) == ["USDJPY", "EURUSD", "AUDUSD"]


def test_min_conviction_defaults_to_keeping_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``none`` is the default and it is a floor, not a selection."""
    result, _ = run(monkeypatch, CONVICTIONS)

    assert printed_pairs(result) == ["USDJPY", "EURUSD", "AUDUSD", "NZDUSD"]


BLOCKED: tuple[PairBias, ...] = (
    pair_bias("USDJPY", 2.60, blockers=("event:unchecked", "cost:unchecked")),
    pair_bias(
        "EURUSD", 2.31, tradeable=False, blockers=("coverage", "event:unchecked")
    ),
    pair_bias("AUDUSD", 2.10, blockers=()),
)


def test_tradeable_only_removes_the_blocked_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pair carrying a hard block goes, and one carrying only a marker stays.

    The marker case is the one worth pinning. ``event:unchecked`` means the
    calendar was never consulted, which is the normal state of an offline run,
    and a filter keying on "has any blocker" would hide every pair in the run.
    """
    result, _ = run(monkeypatch, BLOCKED, "--tradeable-only")

    assert printed_pairs(result) == ["USDJPY", "AUDUSD"]


def test_a_non_blocking_marker_prints_on_the_row_it_belongs_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker that is not rendered does not exist."""
    result, _ = run(monkeypatch, BLOCKED, "--tradeable-only")

    row = next(line for line in rows(result) if line.startswith("USDJPY"))
    assert "event:unchecked" in row
    assert "cost:unchecked" in row
    other = next(line for line in rows(result) if line.startswith("AUDUSD"))
    assert "unchecked" not in other


def test_a_pair_with_no_coverage_prints_rather_than_vanishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero coverage on a leg is a fact about the run, not a pair to omit."""
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias(
            "EURCHF", 0.04, tradeable=False, blockers=("no_coverage", "coverage")
        ),
    )

    result, _ = run(monkeypatch, biases)

    assert "EURCHF" in printed_pairs(result)
    row = next(line for line in rows(result) if line.startswith("EURCHF"))
    assert "no_coverage" in row


def test_every_pair_a_filter_removed_is_listed_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pair dropped in silence reads as a pair the engine never scored."""
    result, _ = run(monkeypatch, CONVICTIONS, "--min-conviction", "medium")

    listed = hidden_block(result)
    assert [line.split()[0] for line in listed] == ["AUDUSD", "NZDUSD"]
    assert "low" in listed[0]
    assert "none" in listed[1]


def test_the_hidden_count_matches_the_number_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heading counts what the block lists, with all three filters composing."""
    universe = []
    for index, pair in enumerate(ALL_PAIRS):
        universe.append(
            pair_bias(
                pair,
                2.60 - index * 0.05,
                conviction=Conviction.HIGH if index % 2 == 0 else Conviction.LOW,
                tradeable=index % 3 != 0,
                blockers=() if index % 3 != 0 else ("coverage",),
            )
        )

    result, _ = run(
        monkeypatch,
        tuple(universe),
        "--majors",
        "--min-conviction",
        "medium",
        "--tradeable-only",
    )

    listed = hidden_block(result)
    shown = printed_pairs(result)
    assert len(shown) + len(listed) == len(MAJORS)
    assert str(len(listed)) in hidden_heading(result)
    assert set(shown).isdisjoint({line.split()[0] for line in listed})


def test_nothing_is_hidden_when_no_filter_removes_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No heading at all rather than a heading saying zero.

    The table is asserted to be full first. Asserting only the absence of the
    heading passes against a command that printed nothing whatsoever, which is
    how this test first passed against the stub.
    """
    result, _ = run(monkeypatch, MIXED)

    assert len(printed_pairs(result)) == len(MIXED)
    assert hidden_heading(result) == ""
    assert hidden_block(result) == []


def test_top_truncates_without_changing_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first N rows of ``--top N`` are the first N rows of the full list.

    The count is asserted as well as the prefix. Two empty lists satisfy the
    prefix comparison, which is how this test first passed against the stub.
    """
    full, _ = run(monkeypatch, MIXED)
    topped, _ = run(monkeypatch, MIXED, "--top", "3")

    assert len(printed_pairs(full)) == len(MIXED)
    assert len(printed_pairs(topped)) == 3
    assert printed_pairs(topped) == printed_pairs(full)[:3]


def test_top_does_not_change_the_hidden_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--top`` truncates the view. It is not a filter and hides no pair."""
    without, _ = run(monkeypatch, CONVICTIONS, "--min-conviction", "medium")
    with_top, _ = run(
        monkeypatch, CONVICTIONS, "--min-conviction", "medium", "--top", "1"
    )

    assert len(printed_pairs(with_top)) == 1
    assert hidden_block(with_top) == hidden_block(without)


def test_the_header_carries_the_asof_and_the_config_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table that cannot be tied to the run that made it cannot be checked."""
    result, _ = run(monkeypatch, MIXED, "--asof", "2026-09-09")

    header = result.stdout.splitlines()[0]
    assert "2026-09-09" in header
    assert load_config().digest() in header


def test_json_carries_the_same_fields_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both renderings of one run agree on every field that decides a trade."""
    result, _ = run(monkeypatch, BLOCKED, "--asof", "2026-09-09", "--format", "json")

    payload = json.loads(result.stdout)
    by_pair = {row["pair"]: row for row in payload["pairs"]}
    assert by_pair["EURUSD"]["spread"] == pytest.approx(2.31)
    assert by_pair["EURUSD"]["direction"] == Direction.LONG.value
    assert by_pair["EURUSD"]["conviction"] == Conviction.HIGH.value
    assert by_pair["EURUSD"]["blockers"] == ["coverage", "event:unchecked"]
    assert by_pair["EURUSD"]["tradeable"] is False
    assert payload["asof"] == ASOF.isoformat()
    assert payload["config_digest"] == load_config().digest()


def test_csv_carries_the_same_fields_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saved rendering carries the run's identifiers on every line."""
    result, _ = run(monkeypatch, BLOCKED, "--asof", "2026-09-09", "--format", "csv")

    reader = csv.DictReader(io.StringIO(result.stdout))
    parsed = {row["pair"]: row for row in reader}
    assert float(parsed["EURUSD"]["spread"]) == pytest.approx(2.31)
    assert parsed["EURUSD"]["direction"] == Direction.LONG.value
    assert parsed["EURUSD"]["conviction"] == Conviction.HIGH.value
    assert parsed["EURUSD"]["blockers"] == "coverage event:unchecked"
    assert parsed["EURUSD"]["asof"] == ASOF.isoformat()
    assert parsed["EURUSD"]["config_digest"] == load_config().digest()


def test_the_direction_printed_is_the_one_the_engine_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not the sign of the spread.

    `build_pair_biases` forces `Direction.NEUTRAL` whenever conviction is
    `Conviction.NONE`, however wide the spread, so a wide spread with no
    direction is a state the engine really produces. A renderer reading the
    sign would print "long" over the engine's own refusal to back it.
    """
    biases = (
        pair_bias(
            "USDJPY", 2.60, direction=Direction.NEUTRAL, conviction=Conviction.NONE
        ),
        pair_bias("EURUSD", -2.31, direction=Direction.SHORT),
    )

    result, _ = run(monkeypatch, biases)

    top = next(line for line in rows(result) if line.startswith("USDJPY"))
    assert Direction.NEUTRAL.value in top
    assert Direction.LONG.value not in top


def test_the_conviction_printed_is_the_one_the_engine_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a band derived from the width of the spread.

    `conviction_for` demotes on thin coverage, on dispersed pillars and on a
    release inside the horizon, none of which the spread can show. The widest
    pair in this fixture is the least backed one.
    """
    biases = (
        pair_bias("USDJPY", 2.60, conviction=Conviction.LOW),
        pair_bias("EURUSD", 0.40, conviction=Conviction.HIGH),
    )

    result, _ = run(monkeypatch, biases)

    top = next(line for line in rows(result) if line.startswith("USDJPY"))
    assert Conviction.LOW.value in top
    other = next(line for line in rows(result) if line.startswith("EURUSD"))
    assert Conviction.HIGH.value in other


def test_the_legs_printed_are_the_ones_the_engine_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both composites come off the row, neither derived from the spread.

    The trio is inconsistent on purpose: ``1.42 - (-0.31)`` is ``1.73`` and not
    the ``2.60`` this row carries. A renderer printing ``base_score - spread``
    in the quote column would show ``-1.18`` here, which is what the published
    example happens to hold and what a self-consistent fixture cannot catch.
    """
    biases = (pair_bias("USDJPY", 2.60, base_score=1.42, quote_score=-0.31),)

    result, _ = run(monkeypatch, biases)

    row = rows(result)[0]
    assert "+1.42" in row
    assert "-0.31" in row
    assert "-1.18" not in row


def test_matrix_says_the_view_is_not_available_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial grid would show plausible numbers under the wrong headers."""
    result, _ = run(monkeypatch, MIXED, "--matrix")

    assert result.exit_code != EXIT_OK
    assert "--matrix" in result.output


def test_a_future_asof_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every series would be past its allowance and every pair would be flat."""
    ahead = date.today().replace(year=date.today().year + 1)

    result, _ = run(monkeypatch, MIXED, "--asof", ahead.isoformat())

    assert result.exit_code == 2


def test_a_cold_cache_says_so_rather_than_printing_an_empty_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty table reads as a working engine with no opinions.

    The message is asserted and not only the code. `EXIT_UNUSABLE` is 1, which
    is also what an uncaught exception exits with, so a test resting on the
    code alone passes against a command that simply raised. That is how this
    test first passed against the stub, and it is the same trap #137 hit.
    """
    result, _ = run(monkeypatch, MIXED, observations=())

    assert result.exit_code == EXIT_UNUSABLE
    assert printed_pairs(result) == []
    assert "nothing to difference" in result.stdout
    assert "fbe refresh" in result.stdout


def test_the_run_date_reaches_the_bias_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bias layer is asked about the run's date, not about today."""
    result, captured = run(monkeypatch, MIXED, "--asof", "2026-09-09")

    assert result.exit_code == EXIT_OK
    build = captured["build"]
    assert isinstance(build, dict)
    assert build["asof"] == ASOF


def test_the_filters_run_over_every_pair_the_bias_layer_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filtering after the view filter would leave an unfiltered row printable."""
    result, captured = run(monkeypatch, MIXED, "--majors")

    assert result.exit_code == EXIT_OK
    assert captured["filtered"] == [bias.pair for bias in MIXED]


# --- what the sweep found the tests above could not see ---------------------


def test_two_pairs_of_the_same_width_print_in_a_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table that reorders itself between two identical runs cannot be diffed.

    No other fixture here holds a tie, so an implementation with no tiebreak
    passes them all and falls back to whatever order `fbe.universe.ALL_PAIRS`
    handed over. The two pairs below are the same width and opposite signs,
    and they are supplied in the reverse of the order they must print in.
    """
    biases = (
        pair_bias("USDJPY", -1.80),
        pair_bias("EURUSD", 1.80),
        pair_bias("GBPUSD", 1.80),
    )

    result, _ = run(monkeypatch, biases)

    assert printed_pairs(result) == ["EURUSD", "GBPUSD", "USDJPY"]


def test_a_short_pair_prints_its_spread_with_the_minus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sign is the direction of the disagreement and is never implied.

    Every other assertion here matches on the pair or on a word, so a renderer
    printing the absolute spread passes them: the ranked order is built on the
    width and would not move.
    """
    biases = (pair_bias("NZDUSD", -2.48, direction=Direction.SHORT),)

    result, _ = run(monkeypatch, biases)

    row = rows(result)[0]
    assert "-2.48" in row
    assert "+2.48" not in row


def test_a_hidden_pair_names_the_blockers_that_stopped_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Blocked" is not a reason. The next action differs by which blocker it is.

    ``coverage`` means go and look at why the data is thin. ``event`` means
    wait for the release. A reader given only the word has to run the command
    again without the filter to find out which.
    """
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias(
            "EURUSD",
            2.31,
            tradeable=False,
            blockers=("coverage", "event: ECB at 12:45 UTC"),
        ),
    )

    result, _ = run(monkeypatch, biases, "--tradeable-only")

    listed = hidden_block(result)
    assert len(listed) == 1
    assert "coverage" in listed[0]
    assert "ECB at 12:45 UTC" in listed[0]


def test_top_truncates_the_json_as_well_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both renderings of one run show the same pairs.

    A machine format carrying rows the table did not print means a script and
    the person reading over its shoulder disagree about what the run said.
    """
    result, _ = run(monkeypatch, MIXED, "--top", "2", "--format", "json")

    payload = json.loads(result.stdout)
    table, _ = run(monkeypatch, MIXED, "--top", "2")
    assert [row["pair"] for row in payload["pairs"]] == printed_pairs(table)
    assert len(payload["pairs"]) == 2


def test_top_truncates_the_csv_as_well_as_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same agreement on the other machine format."""
    result, _ = run(monkeypatch, MIXED, "--top", "2", "--format", "csv")

    parsed = list(csv.DictReader(io.StringIO(result.stdout)))
    table, _ = run(monkeypatch, MIXED, "--top", "2")
    assert [row["pair"] for row in parsed] == printed_pairs(table)


def test_a_redirected_csv_stays_parseable_when_pairs_were_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hidden block never lands in the CSV stream.

    The same split ``score`` makes with its warnings: a flat format has no
    row-level place for a run-level statement, so the block goes to stderr and
    `fbe bias --format csv > monday.csv` still writes a file a parser can
    read. A heading in the middle of the rows would make the file unreadable
    at exactly the point the run had something to say.
    """
    result, _ = run(
        monkeypatch, CONVICTIONS, "--format", "csv", "--min-conviction", "medium"
    )

    assert result.exit_code == EXIT_OK
    parsed = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [row["pair"] for row in parsed] == ["USDJPY", "EURUSD"]
    assert "hidden by the filters" not in result.stdout
