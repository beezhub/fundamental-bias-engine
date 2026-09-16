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
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from fbe.cli import (
    EXIT_OK,
    EXIT_UNUSABLE,
    NO_BLOCKERS,
    _bias_columns,
    _pillar_order,
    app,
)
from fbe.config import ScoringConfig, load_config
from fbe.datasources import ALL_SOURCES
from fbe.datasources.collect import CollectionResult, lookback_start
from fbe.types import (
    Conviction,
    CurrencyScore,
    Direction,
    Frequency,
    Observation,
    PairBias,
    PillarScore,
)
from fbe.universe import ALL_PAIRS, G10, MAJORS

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
    scores: Sequence[CurrencyScore] = (),
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

    # Recorded, and asserted by the wire tests below. Capturing without
    # asserting is what let `collect(config.data, ...)` without the offline
    # override survive every test in this file: the command going to the
    # network in a suite whose header promises it never does.

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
        # The whole call, not only the pair. Handing `apply_filters` an empty
        # score map loses the coverage and no_coverage blockers entirely, and
        # handing it today's date moves the event window off the run, and
        # recording only the pair could see neither.
        captured["filtered"].append((bias_in.pair, tuple(sorted(scores_in)), asof_in))
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
    assert json.loads(parsed["EURUSD"]["blockers"]) == [
        "coverage",
        "event:unchecked",
    ]
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
    assert [call[0] for call in captured["filtered"]] == [  # type: ignore[union-attr]
        bias.pair for bias in MIXED
    ]


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
    # And it has to be somewhere. Asserting only its absence from stdout
    # passes against a command that dropped the block entirely, which is the
    # whole reason redirecting this format is safe.
    assert "hidden by the filters" in result.stderr
    assert "AUDUSD" in result.stderr
    assert "NZDUSD" in result.stderr


# --- what the review pass found the tests above could not see ---------------
#
# Every test above replaces `apply_filters` with the identity so the fixture's
# own `tradeable` and `blockers` survive. That is right for the rendering
# tests and it is also why none of them had ever seen what this command prints
# in a real run. The tests here close that gap.


def test_a_neutral_pair_keeps_its_columns_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``neutral`` is the longest value the direction column holds.

    At seven characters it exactly filled a seven-wide field, so the cell ran
    into the conviction beside it and printed ``neutralnone``. `direction_for`
    returns NEUTRAL for any spread inside the medium threshold, which is
    roughly half the pairs on a typical run, so this was the common row.

    Asserting membership is what missed it: ``"neutral" in row`` is true of
    ``neutralnone`` too. This asserts the separation.
    """
    biases = (
        pair_bias(
            "AUDUSD", 0.04, direction=Direction.NEUTRAL, conviction=Conviction.NONE
        ),
    )

    result, _ = run(monkeypatch, biases)

    row = rows(result)[0]
    assert "neutralnone" not in row
    assert f"{Direction.NEUTRAL.value} {Conviction.NONE.value}" in row


def test_a_hidden_reason_names_only_the_blockers_that_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two of the eight kinds say a check never ran. Neither removed anything.

    `apply_filters` records ``cost:unchecked`` and ``event:unchecked`` on
    every pair of every run of this command, so listing the whole tuple after
    the word "blocked" names two checks that did not happen as reasons the
    pair was dropped. The Notes column still prints all of them, which is a
    different question: what the pair carries, not why it went.
    """
    biases = (
        pair_bias("USDJPY", 2.60, blockers=("cost:unchecked", "event:unchecked")),
        pair_bias(
            "EURUSD",
            2.31,
            tradeable=False,
            blockers=("coverage", "cost:unchecked", "event:unchecked"),
        ),
    )

    result, _ = run(monkeypatch, biases, "--tradeable-only")

    listed = hidden_block(result)
    assert len(listed) == 1
    assert "blocked: coverage" in listed[0]
    assert "unchecked" not in listed[0]
    kept = next(line for line in rows(result) if line.startswith("USDJPY"))
    assert "cost:unchecked" in kept


def test_a_blocked_pair_with_no_blocker_recorded_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``blocked:`` with nothing after it explains nothing.

    Not reachable from `apply_filters`, which always records why, but the
    renderer should not produce a dangling clause if it ever became so.
    """
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias("EURUSD", 2.31, tradeable=False, blockers=()),
    )

    result, _ = run(monkeypatch, biases, "--tradeable-only")

    listed = hidden_block(result)
    assert len(listed) == 1
    assert not listed[0].rstrip().endswith("blocked:")
    assert "no blocker recorded" in listed[0]


def test_the_csv_blockers_field_survives_a_reason_carrying_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one blocker a reader most needs intact is the one that has a payload.

    ``event`` is emitted as ``"event: <reason>"`` and the reason names the
    release and its scheduled time, so it holds spaces and can hold a comma.
    A space-joined field turned one blocker into five, four of which were
    ``FOMC``, ``at``, ``18:00`` and ``UTC``.
    """
    blockers = ("coverage", "event: FOMC at 18:00 UTC")
    biases = (pair_bias("USDJPY", 2.60, tradeable=False, blockers=blockers),)

    result, _ = run(monkeypatch, biases, "--format", "csv")

    parsed = list(csv.DictReader(io.StringIO(result.stdout)))
    assert json.loads(parsed[0]["blockers"]) == list(blockers)


def test_the_run_says_the_calendar_was_never_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 24-hour conviction cap does not run, and the tier does not say so.

    `apply_filters` marks its own two absences on the row. The cap in
    `build_pair_biases` leaves no marker at all, so a pair can print ``high``
    on an FOMC evening and look exactly like a pair checked and cleared. The
    check cannot run yet, so the run says which look was not taken.
    """
    result, _ = run(monkeypatch, MIXED)

    assert "No calendar was consulted" in result.stdout
    assert "conviction cap did not run" in result.stdout


def test_a_run_that_scored_nothing_refuses_to_be_traded_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every currency at zero coverage differences to 28 spreads of +0.00.

    The rows still print, because the absence is what there is to see, but
    `docs/interfaces.md` reserves exit 1 for coverage collapsing and a script
    chaining this command reads a zero exit as a working engine with no
    opinions. ``score`` refuses the same run for the same reason.
    """
    flat = tuple(
        CurrencyScore(
            currency=code,
            composite=0.0,
            pillars={},
            asof=ASOF,
            rank=index + 1,
            dispersion=0.0,
            coverage=0.0,
        )
        for index, code in enumerate(("USD", "EUR", "JPY"))
    )

    result, _ = run(monkeypatch, MIXED, scores=flat)

    assert result.exit_code == EXIT_UNUSABLE
    assert printed_pairs(result) != []


def test_a_thin_but_real_run_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One currency with no data is a thin run, not a collapsed one."""
    mixed_coverage = (
        CurrencyScore(
            currency="USD",
            composite=1.42,
            pillars={},
            asof=ASOF,
            rank=1,
            dispersion=0.6,
            coverage=0.9,
        ),
        CurrencyScore(
            currency="JPY",
            composite=0.0,
            pillars={},
            asof=ASOF,
            rank=2,
            dispersion=0.0,
            coverage=0.0,
        ),
    )

    result, _ = run(monkeypatch, MIXED, scores=mixed_coverage)

    assert result.exit_code == EXIT_OK


# --- the real bias layer, once ----------------------------------------------


def g10_scores(composites: Mapping[str, float]) -> tuple[CurrencyScore, ...]:
    """One score per G10 currency at full coverage.

    `build_pair_biases` raises on a missing leg, so the whole universe has to
    be present even when only a few composites matter to the assertion.
    """
    return tuple(
        CurrencyScore(
            currency=code,
            composite=composites.get(code, 0.0),
            pillars={},
            asof=ASOF,
            rank=index + 1,
            dispersion=0.4,
            coverage=1.0,
        )
        for index, code in enumerate(sorted(G10))
    )


def run_for_real(
    monkeypatch: pytest.MonkeyPatch,
    composites: Mapping[str, float],
    *args: str,
) -> Result:
    """Drive the command with the real bias layer, replacing only the scorer.

    Every other test here replaces `fbe.bias.apply_filters` with the identity,
    which is what lets a fixture carry the ``tradeable`` and ``blockers`` an
    assertion needs. The cost is that no other test has seen what this command
    actually prints, and two claims in `docs/interfaces.md` survived weeks of
    green tests because of it: that a row's Notes column can read ``-``, and
    that a reason can name a blocker the run cannot produce.
    """

    def fake_collect(_config: object, **_kwargs: object) -> CollectionResult:
        return CollectionResult(observations=(OBSERVATION,), outcomes=(), gaps={})

    def fake_score_currencies(
        *_args: object, **_kwargs: object
    ) -> Sequence[CurrencyScore]:
        return g10_scores(composites)

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)
    return runner.invoke(app, ["bias", "--asof", "2026-09-09", *args])


def test_a_real_run_marks_the_two_checks_that_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row of every run of this command carries both unchecked markers.

    The command passes neither a `CalendarGuard` nor a ``cost_ratio``, because
    `fbe.calendar_guard` is scaffolded and no execution layer supplies a cost
    yet. `apply_filters` records that rather than staying silent, so the Notes
    column can never read ``-`` as the published example claimed it could.
    """
    result = run_for_real(monkeypatch, {"USD": 1.42, "JPY": -1.18})

    assert result.exit_code == EXIT_OK
    for line in rows(result):
        assert "cost:unchecked" in line
        assert "event:unchecked" in line
    assert not any(line.rstrip().endswith(f"  {NO_BLOCKERS}") for line in rows(result))


def test_a_real_run_agrees_with_the_bias_layer_about_every_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The printed row is the row `fbe.bias` produced, field by field.

    Computed independently here from the composites rather than read back from
    the implementation: USDJPY's spread is ``1.42 - (-1.18)``, which is
    ``2.60``, above `ScoringConfig.min_spread_high`, and the legs print as
    given.
    """
    result = run_for_real(monkeypatch, {"USD": 1.42, "JPY": -1.18}, "--format", "json")

    payload = json.loads(result.stdout)
    usdjpy = next(row for row in payload["pairs"] if row["pair"] == "USDJPY")
    assert usdjpy["spread"] == pytest.approx(2.60)
    assert usdjpy["base_score"] == pytest.approx(1.42)
    assert usdjpy["quote_score"] == pytest.approx(-1.18)
    assert usdjpy["direction"] == Direction.LONG.value
    assert usdjpy["blockers"] == ["cost:unchecked", "event:unchecked"]
    assert usdjpy["tradeable"] is True
    assert payload["pairs"][0]["pair"] == "USDJPY"


def test_a_real_run_never_names_an_unchecked_marker_as_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the identity-stubbed tests could not reach.

    With every currency at the same composite, every pair has no edge and is
    removed by ``--tradeable-only`` carrying three blockers, two of which did
    not block. The hidden block must name only ``no_edge``.
    """
    result = run_for_real(monkeypatch, {}, "--tradeable-only")

    listed = hidden_block(result)
    assert listed
    for line in listed:
        assert "blocked: no_edge" in line
        assert "unchecked" not in line


# --- the wire into the data and bias layers ---------------------------------


def test_the_command_reads_the_cache_and_never_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one assertion this file's own header promised and did not make.

    `score` documents why: passing the config through unchanged would let a
    rolled-over TTL refetch mid-session, so two runs at the same ``--asof``
    and the same digest could print different tables with nothing on screen to
    explain it. Replacing `collect` in the test keeps the socket shut whatever
    the command asks for, so only this assertion can tell the two apart.
    """
    _, captured = run(monkeypatch, MIXED)

    data_config = captured["data_config"]
    assert data_config.offline is True


def test_the_collection_window_comes_from_the_run_date_and_the_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ends of the window, and the lookback proved on the wire.

    ``end`` on today's date rather than the run's is look-ahead: a historical
    run would read observations released after the date it claims to represent.
    ``start`` ignoring ``lookback_years`` silently shortens every history.
    """
    _, captured = run(monkeypatch, MIXED, "--asof", "2026-09-09")

    window = captured["collect"]
    assert isinstance(window, dict)
    assert window["end"] == ASOF
    assert window["start"] == lookback_start(ASOF, load_config().scoring.lookback_years)
    assert window["sources"] == ALL_SOURCES


def test_the_lookback_is_read_from_config_and_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override it and the window has to move, or the config is decoration."""
    monkeypatch.setenv("FBE_SCORING_LOOKBACK_YEARS", "2")

    _, captured = run(monkeypatch, MIXED, "--asof", "2026-09-09")

    window = captured["collect"]
    assert isinstance(window, dict)
    assert window["start"] == lookback_start(ASOF, 2)
    # Against the shipped default, not against `load_config()`, which reads the
    # same environment variable and would be comparing the override to itself.
    assert ScoringConfig().lookback_years != 2
    assert window["start"] != lookback_start(ASOF, ScoringConfig().lookback_years)


def test_the_scorer_and_the_filters_are_asked_about_the_runs_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every stage sees the run's date. One stage on today's is a silent mix."""
    _, captured = run(monkeypatch, MIXED, "--asof", "2026-09-09")

    scored = captured["score"]
    assert isinstance(scored, dict)
    assert scored["asof"] == ASOF
    calls = captured["filtered"]
    assert isinstance(calls, list)
    assert {call[2] for call in calls} == {ASOF}


def test_the_filters_are_handed_the_whole_universe_of_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty score map loses the coverage blockers without any error.

    `apply_filters` reads each leg's coverage out of this mapping. Handed
    nothing it raises, handed a partial map it raises on the missing leg, and
    handed a map built from a different run it answers about that run. The
    mapping is the whole point of the argument and nothing checked it arrived.
    """
    universe = g10_scores({"USD": 1.42, "JPY": -1.18})

    _, captured = run(monkeypatch, MIXED, scores=universe)

    calls = captured["filtered"]
    assert isinstance(calls, list)
    assert calls
    for _pair, codes, _asof in calls:
        assert set(codes) == set(G10)


# --- the machine payload ----------------------------------------------------


def test_the_payload_carries_the_legs_the_right_way_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair convention is load-bearing and the payload publishes both legs.

    A consumer rebuilding a pair string from ``base`` and ``quote`` gets
    USDEUR for EURUSD if the two are swapped, and a report that inverts a pair
    inverts its bias without saying so. Nothing asserted these two fields.
    """
    result, _ = run(monkeypatch, MIXED, "--format", "json")

    payload = json.loads(result.stdout)
    assert payload["pairs"]
    for row in payload["pairs"]:
        assert row["base"] + row["quote"] == row["pair"]
    eurusd = next(row for row in payload["pairs"] if row["pair"] == "EURUSD")
    assert eurusd["base"] == "EUR"
    assert eurusd["quote"] == "USD"


def test_the_payload_legs_are_not_derived_from_the_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derivation the table is already protected against, one layer down."""
    biases = (pair_bias("USDJPY", 2.60, base_score=1.42, quote_score=-0.31),)

    result, _ = run(monkeypatch, biases, "--format", "json")

    row = json.loads(result.stdout)["pairs"][0]
    assert row["base_score"] == pytest.approx(1.42)
    assert row["quote_score"] == pytest.approx(-0.31)


def test_the_payload_carries_the_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping it entirely survived every other assertion in this file."""
    biases = (pair_bias("USDJPY", 2.60, agreement=0.7),)

    result, _ = run(monkeypatch, biases, "--format", "json")

    assert json.loads(result.stdout)["pairs"][0]["agreement"] == pytest.approx(0.7)


# --- the agreement column ---------------------------------------------------


def test_agreement_is_floored_and_not_rounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounding up flatters the run at the threshold that decides conviction.

    `ScoringConfig.min_agreement` is the number standing between a pair and a
    higher tier, so 69.9% printing as 70% tells the reader the pair cleared a
    bar it did not.
    """
    biases = (pair_bias("USDJPY", 2.60, agreement=0.699),)

    result, _ = run(monkeypatch, biases)

    row = rows(result)[0]
    assert "69%" in row
    assert "70%" not in row


def test_agreement_absorbs_binary_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0.29 * 100`` is ``28.999999999999996``, and flooring that gives 28.

    The epsilon exists for this and nothing exercised it on this column.
    """
    biases = (pair_bias("USDJPY", 2.60, agreement=0.29),)

    result, _ = run(monkeypatch, biases)

    row = rows(result)[0]
    assert "29%" in row
    assert "28%" not in row


def test_agreement_is_a_share_of_weight_and_not_a_count_of_pillars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docstring promises a percentage. A count out of seven survived.

    0.5 is not a whole number of sevenths, so a renderer printing ``n/7``
    cannot produce 50% from it.
    """
    biases = (pair_bias("USDJPY", 2.60, agreement=0.5),)

    result, _ = run(monkeypatch, biases)

    assert "50%" in rows(result)[0]


# --- the hidden block, exactly ----------------------------------------------


def test_a_pair_removed_by_both_filters_says_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented, and nothing asserted it.

    Joining only the first clause, only the last, or suppressing the tradeable
    clause when a conviction clause is present all survived the composing test,
    which hid such a pair without ever reading its reason line.
    """
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias(
            "EURUSD",
            2.31,
            conviction=Conviction.LOW,
            tradeable=False,
            blockers=("coverage",),
        ),
    )

    result, _ = run(
        monkeypatch, biases, "--min-conviction", "medium", "--tradeable-only"
    )

    listed = hidden_block(result)
    assert len(listed) == 1
    assert "conviction low, below medium" in listed[0]
    assert "blocked: coverage" in listed[0]


def test_a_hidden_line_reads_exactly_as_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One exact string, because every part of it was separately droppable.

    The spread, its sign, the floor named after the conviction and the two
    spaces of indent were each removable without failing anything.
    """
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias("USDCAD", 1.47, conviction=Conviction.LOW),
    )

    result, _ = run(monkeypatch, biases, "--min-conviction", "medium")

    assert hidden_block(result) == [
        "  USDCAD  spread +1.47, conviction low, below medium"
    ]


def test_a_hidden_short_pair_keeps_the_minus_on_its_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sign is the same fact here that it is in the table."""
    biases = (
        pair_bias("USDJPY", 2.60),
        pair_bias("GBPUSD", -1.11, conviction=Conviction.LOW),
    )

    result, _ = run(monkeypatch, biases, "--min-conviction", "medium")

    assert "spread -1.11" in hidden_block(result)[0]


def test_the_hidden_heading_counts_and_names_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count starts the line, so a substring match cannot pass on a digit.

    ``str(len(listed)) in heading`` was satisfied by "13 majors hidden" when
    three were hidden, because "13" contains "3".
    """
    universe = tuple(
        pair_bias(
            pair,
            2.60 - index * 0.05,
            conviction=Conviction.HIGH if pair in MAJORS[:2] else Conviction.LOW,
        )
        for index, pair in enumerate(ALL_PAIRS)
    )

    narrowed, _ = run(monkeypatch, universe, "--majors", "--min-conviction", "medium")
    everything, _ = run(monkeypatch, universe, "--min-conviction", "medium")

    assert hidden_heading(narrowed).startswith(f"{len(hidden_block(narrowed))} majors ")
    assert hidden_heading(everything).startswith(
        f"{len(hidden_block(everything))} pairs "
    )


# --- the hidden pairs in JSON -----------------------------------------------


def test_json_carries_the_hidden_pairs_with_their_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer should not have to rerun the command without its filters."""
    result, _ = run(
        monkeypatch, CONVICTIONS, "--min-conviction", "medium", "--format", "json"
    )

    payload = json.loads(result.stdout)
    hidden = {row["pair"]: row for row in payload["hidden"]}
    assert set(hidden) == {"AUDUSD", "NZDUSD"}
    assert "conviction low, below medium" in hidden["AUDUSD"]["reason"]
    assert payload["pool"] == "all"


def test_json_names_the_pool_when_it_was_narrowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heading's pool word has a machine-readable twin, and it moved."""
    universe = tuple(
        pair_bias(pair, 0.5 + index * 0.01) for index, pair in enumerate(ALL_PAIRS)
    )

    result, _ = run(monkeypatch, universe, "--majors", "--format", "json")

    assert json.loads(result.stdout)["pool"] == "majors"


def test_top_leaves_the_json_hidden_list_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--top`` shortens the view. The machine half has the same rule."""
    without, _ = run(
        monkeypatch, CONVICTIONS, "--min-conviction", "medium", "--format", "json"
    )
    with_top, _ = run(
        monkeypatch,
        CONVICTIONS,
        "--min-conviction",
        "medium",
        "--top",
        "1",
        "--format",
        "json",
    )

    full = json.loads(without.stdout)
    topped = json.loads(with_top.stdout)
    assert len(topped["pairs"]) == 1
    assert len(topped["hidden"]) == len(full["hidden"]) == 2


# --- the table's own furniture ----------------------------------------------


def test_the_column_headings_are_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the heading line failed nothing, and it names every column."""
    result, _ = run(monkeypatch, MIXED)

    assert _bias_columns() in result.stdout.splitlines()


def test_a_long_pair_prints_its_spread_with_the_plus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both signs are explicit. Only the minus was pinned."""
    biases = (pair_bias("USDJPY", 2.60),)

    result, _ = run(monkeypatch, biases)

    assert "+2.60" in rows(result)[0]


def test_matrix_refuses_with_the_parameter_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 2 and not 1. A chaining script reads the two differently.

    1 means the run happened and should not be traded on. 2 means the command
    was asked for something it cannot do.
    """
    result, _ = run(monkeypatch, MIXED, "--matrix")

    assert result.exit_code == 2


# --- the published example --------------------------------------------------


def published_block(heading: str) -> list[str]:
    """The lines of one console block in ``docs/interfaces.md``."""
    text = Path("docs/interfaces.md").read_text(encoding="utf-8")
    return text.split(heading + "\n")[1].split("```")[0].splitlines()


def scores_from_the_published_score_table() -> tuple[CurrencyScore, ...]:
    """Rebuild the run behind the ``fbe score --pillars`` example.

    The document publishes one run in two console blocks. This reads the first
    and hands it to the command so the second can be checked against it, which
    is the only way the bias block's own numbers are pinned to anything: a test
    that rebuilds the bias block from its own cells reproduces whatever the
    document says, including a sign error, which is how the first version of
    this test passed against a GBPUSD spread published with the wrong sign.
    """
    order = _pillar_order(load_config().scoring)
    weights = load_config().scoring.weights
    rows_out: list[CurrencyScore] = []
    for line in published_block("$ fbe score --pillars"):
        parts = line.split()
        if len(parts) != 5 + len(order) or not parts[0].isdigit():
            continue
        code = parts[1]
        rows_out.append(
            CurrencyScore(
                currency=code,
                composite=float(parts[2]),
                pillars={
                    name: PillarScore(
                        pillar=name,
                        currency=code,
                        raw=float(parts[5 + index]) * 10.0,
                        z=float(parts[5 + index]),
                        score=float(parts[5 + index]),
                        weight=weights[name],
                        asof=ASOF,
                    )
                    for index, name in enumerate(order)
                },
                asof=ASOF,
                rank=int(parts[0]),
                dispersion=float(parts[3]),
                coverage=int(parts[4].rstrip("%")) / 100,
            )
        )
    return tuple(rows_out)


def test_the_published_bias_example_is_the_published_score_example_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document publishes one run in two blocks, and they have to agree.

    Everything below the composites is derived: the spreads, the directions,
    the convictions, the agreement percentages, the hidden pairs and the reason
    each one went. So the bias block is checkable against the score block, and
    the first version of it was wrong in three ways that no test could see.
    GBPUSD's spread was published as ``+1.11`` when ``0.31 - 1.42`` is
    ``-1.11``. GBPUSD was said to be blocked on ``coverage`` at 86% coverage,
    which `ScoringConfig.min_coverage` at 0.60 cannot produce. Every row's
    Notes column read ``-``, which no run of this command can emit, because it
    passes neither a calendar guard nor a dealing cost and `apply_filters`
    records both absences.

    The digest line is checked separately. The document carries an
    illustrative digest across all its examples, and pinning the real one here
    would make every weight change fail this test for the wrong reason.
    """
    scores = scores_from_the_published_score_table()
    assert len(scores) == len(G10)

    def fake_collect(_config: object, **_kwargs: object) -> CollectionResult:
        return CollectionResult(observations=(OBSERVATION,), outcomes=(), gaps={})

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", lambda *a, **k: scores)

    result = runner.invoke(
        app,
        [
            "bias",
            "--asof",
            ASOF.isoformat(),
            "--majors",
            "--min-conviction",
            "medium",
            "--tradeable-only",
        ],
    )
    assert result.exit_code == EXIT_OK

    published = published_block(
        "$ fbe bias --majors --min-conviction medium --tradeable-only"
    )
    printed = result.stdout.rstrip().splitlines()
    assert len(printed) == len(published)
    assert printed[0].startswith(f"asof {ASOF.isoformat()}   config ")
    assert published[0].startswith(f"asof {ASOF.isoformat()}   config ")
    for got, want in zip(printed[1:], published[1:], strict=True):
        assert got.rstrip() == want.rstrip()
