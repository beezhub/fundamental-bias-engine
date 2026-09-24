"""The pair matrix and the expansion under it: issue #253.

Three defects shape this file, and none of them raises.

The first is the mirrored cell. `fbe.report._grid` orients every cell for the
row it sits in, and both renderers print what they are handed. A view that
negates, swaps or relabels a cell inverts the whole lower triangle a second
time, and every number on screen stays plausible. So the first test compares
all 56 rendered cells against the grid that produced them rather than checking
that cells exist.

The second is the marker nobody reads. An offline run carries
``event:unchecked`` on all 28 pairs, and a marker repeated down 28 rows is one
the morning review scrolls past: `docs/decisions/0002-representing-not-known.md`
rule 3 says a marker that is not rendered does not exist, and
``docs/interfaces.md`` records the converse, that a marker rendered on every row
arrives at the same place by the other route. The all-rows test is strict, so
27 of 28 stays on the rows.

The third is the printed zero. A currency the run could not score has no pillar
to show, and a ``+0.00`` in its place reads as two economies level rather than
as no data. The expansion prints a marker there, and this file pins it on JPY,
which the fixture scores on nothing at all.

Everything renders from ``tests/fixtures/dashboard_report.json``, the same
committed run ``tests/test_dashboard_render.py`` uses, so the two halves of one
page cannot be tested against two different runs. Nothing here reaches the
network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from fbe.bias import UNCHECKED_SUFFIX
from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.dashboard.build import check_constraints, render_dashboard
from fbe.report import build_context, load_report
from fbe.types import BiasReport, CurrencyScore, PillarName

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"

FORBIDDEN = ("backtest", "backtested", "proven", "win rate", "hit rate", "edge")
"""Language `CLAUDE.md` forbids, checked again on the sections this issue adds."""

CELL = re.compile(
    r'<td class="num ?(?P<classes>[^"]*)"\s+'
    r'title="(?P<base>\w{3}) vs (?P<quote>\w{3})[^"]*">\s*'
    r"(?P<printed>[+-][\d.]+)\s*</td>",
    flags=re.S,
)
"""One populated matrix cell: its classes, its legs and the number it prints."""

DETAIL = re.compile(r'<details class="pair">(?P<body>.*?)</details>', flags=re.S)
"""One pair's expansion, summary and all."""


@pytest.fixture(scope="module")
def report() -> BiasReport:
    return load_report(FIXTURE)


@pytest.fixture(scope="module")
def page(report: BiasReport) -> str:
    return render_dashboard(report)


def cells(page: str) -> dict[str, tuple[str, str]]:
    """Every populated cell as ``pair -> (classes, printed number)``.

    Keyed by the cell's own legs rather than by market convention, because that
    is what the reader sees: on USD's row the EUR column is USDEUR.
    """
    found = {
        f"{match['base']}{match['quote']}": (match["classes"], match["printed"])
        for match in CELL.finditer(page)
    }
    assert len(found) == 56, f"expected 56 populated cells, parsed {len(found)}"
    return found


def details(page: str) -> list[str]:
    """Every pair expansion on the page, in the order it renders."""
    return [match["body"] for match in DETAIL.finditer(page)]


def _pair_name(body: str) -> str:
    """The pair one expansion is about, from its own summary."""
    match = re.search(r'<span class="pair-name">(\w{6})</span>', body)
    assert match is not None, f"an expansion names no pair:\n{body[:200]}"
    return match.group(1)


def detail_for(page: str, pair: str) -> str:
    """One pair's expansion, found by the pair it names in its summary."""
    for body in details(page):
        if re.search(rf'<span class="pair-name">{pair}</span>', body):
            return body
    raise AssertionError(f"no expansion rendered for {pair}")


def with_blockers(report: BiasReport, count: int, marker: str) -> BiasReport:
    """The same run with ``marker`` on the first ``count`` pairs.

    The pairs keep everything else, so the only thing that moves between the
    all-rows case and the 27-of-28 case is how many rows carry the marker.
    """
    pairs = tuple(
        replace(row, blockers=(marker,)) if index < count else row
        for index, row in enumerate(report.pairs)
    )
    return replace(report, pairs=pairs)


# --- criterion 1: the cell is coloured and printed exactly as handed over ----


def test_every_cell_prints_the_number_the_grid_supplied(
    report: BiasReport, page: str
) -> None:
    """All 56, against the grid that produced them.

    A test that only counts cells passes on a view that negated the lower
    triangle, which is the one defect this criterion exists for.
    """
    grid = build_context(report)["grid"]
    printed = cells(page)

    for base, row in grid.items():
        for quote, cell in row.items():
            if cell is None:
                continue
            assert printed[f"{base}{quote}"][1] == f"{cell.spread:+.1f}"


# --- criterion 4: the diagonal is a placeholder, not a zero -----------------


def test_the_diagonal_prints_the_documented_placeholder(page: str) -> None:
    """``docs/interfaces.md``: the diagonal prints ``.``.

    An empty cell is not the same statement. A currency has no bias against
    itself, and a blank reads as a cell the run failed to fill, which is what
    `fbe.report._grid` uses ``None`` for elsewhere in the same grid.
    """
    diagonal = re.findall(r'<td class="diag"[^>]*>(.*?)</td>', page, flags=re.S)

    assert len(diagonal) == 8
    for printed in diagonal:
        assert printed.strip() == "."
        assert "0.00" not in printed
        assert "+0.0" not in printed


# --- criterion 5: the page shows its working --------------------------------


def test_every_pair_in_the_run_has_an_expansion(report: BiasReport, page: str) -> None:
    """28 expansions, widest spread first, which is the ranked list as well.

    The matrix note says the numbers are available without reading the colours;
    this is what carries them, so its order is the report's own and not the
    grid's alphabetical one.
    """
    names = [_pair_name(body) for body in details(page)]

    assert names == [row.pair for row in build_context(report)["pairs"]]
    assert len(names) == 28


def test_a_pair_expands_to_its_seven_pillar_contributions(
    report: BiasReport, page: str
) -> None:
    """Criterion 5, against the report's own breakdown rather than a count.

    Seven rows present proves nothing: the same seven names render whichever
    leg's score is printed in which column, and the whole point of the panel is
    which currency holds which view.
    """
    body = detail_for(page, "EURUSD")
    scores = {row.currency: row for row in report.currencies}
    base = scores["EUR"]
    quote = scores["USD"]

    for pillar in PillarName:
        row = re.search(
            rf'<th scope="row">{pillar.value}</th>\s*'
            r'<td class="num">([^<]*)</td>\s*'
            r'<td class="num">([^<]*)</td>\s*'
            r'<td class="num">([^<]*)</td>',
            body,
        )
        assert row is not None, f"{pillar.value} missing from the EURUSD expansion"
        assert row.group(1).strip() == f"{base.pillars[pillar].score:+.2f}"
        assert row.group(2).strip() == f"{quote.pillars[pillar].score:+.2f}"
        difference = base.pillars[pillar].score - quote.pillars[pillar].score
        assert row.group(3).strip() == f"{difference:+.2f}"


def test_the_expansion_lists_its_pillars_in_the_configured_order(
    report: BiasReport,
) -> None:
    """Tests the wire: move the weights and the rows move with them.

    The order is the one `fbe.report.build_context` computes, so the expansion
    and the Markdown report's pillar table read the same way down and a reader
    comparing the two is comparing rows rather than hunting for them. A view
    using `fbe.types.PillarName`'s declaration order instead agrees with the
    report on the shipped weights and disagrees on any other, which is a
    disagreement nobody would see until the weights moved.
    """
    weights = dict(ScoringConfig().weights)
    reversed_weights = {
        pillar: weight
        for pillar, weight in zip(
            weights, reversed(list(weights.values())), strict=True
        )
    }
    config = Config(
        risk=RiskConfig(),
        scoring=ScoringConfig(weights=reversed_weights),
        data=DataConfig(),
    )
    page = render_dashboard(report, config=config)
    rows = re.findall(r'<th scope="row">(\w+)</th>', detail_for(page, "EURUSD"))
    expected = [
        pillar.value for pillar in build_context(report, config=config)["pillar_order"]
    ]

    assert rows == expected
    assert rows != [pillar.value for pillar in PillarName]


def test_the_expansion_carries_the_observations_behind_the_pillars(
    report: BiasReport, page: str
) -> None:
    """The second half of criterion 5, which the pillar scores alone do not give.

    A pillar score is a normalised number. What a reader checks it against is
    the series behind it, its value, its period and when it was published, and
    the release date is the one a historical read filters on.
    """
    body = detail_for(page, "EURUSD")
    scores = {row.currency: row for row in report.currencies}
    observations = [
        observation
        for code in ("EUR", "USD")
        for pillar in PillarName
        for observation in scores[code].pillars[pillar].inputs
    ]

    assert observations, "the fixture carries no observations to show"
    for observation in observations:
        assert observation.indicator in body
        assert observation.series_id in body
        assert observation.unit in body
        assert str(observation.period) in body
        assert f"{observation.value:g}" in body


def test_a_pillar_the_run_could_not_score_is_marked_rather_than_printed_as_zero(
    report: BiasReport, page: str
) -> None:
    """JPY scored on nothing, so every JPY expansion has a column with no score.

    ``+0.00`` there is the expensive answer: it reads as a pillar that measured
    the economy and found it in the middle of its band, which is the opposite of
    having no data. ADR 0002 rule 1, inside one table cell.
    """
    scores = {row.currency: row for row in report.currencies}
    assert scores["JPY"].pillars == {}, "the fixture's JPY leg now carries pillars"

    body = detail_for(page, "EURJPY")
    rows = re.findall(
        r'<th scope="row">(\w+)</th>\s*'
        r'<td class="num">([^<]*)</td>\s*'
        r'<td class="num">([^<]*)</td>\s*'
        r'<td class="num">([^<]*)</td>',
        body,
    )

    assert len(rows) == len(PillarName)
    for _, base, quote, difference in rows:
        assert quote.strip() == "."
        assert difference.strip() == "."
        assert base.strip() != "."


def test_an_observation_with_no_release_date_says_so(report: BiasReport) -> None:
    """A missing release date is a fact about the series, not a blank.

    ``released_at`` is optional on `fbe.types.Observation` because not every
    source publishes one. Rendered as nothing, the reader reads the period
    beside it as the release date, which is the look-ahead this repository
    filters against everywhere else.
    """
    scores = []
    for row in report.currencies:
        pillars = {
            name: replace(
                score,
                inputs=tuple(replace(item, released_at=None) for item in score.inputs),
            )
            for name, score in row.pillars.items()
        }
        scores.append(replace(row, pillars=pillars))

    page = render_dashboard(replace(report, currencies=tuple(scores)))

    assert "release date not recorded" in detail_for(page, "EURUSD")


# --- criterion 6: the expansion is in the page ------------------------------


def test_the_expansion_needs_no_network_and_no_external_script(page: str) -> None:
    """Criterion 6, through the guard rather than by eye.

    The data is inlined and the disclosure is native ``<details>``, so there is
    nothing to fetch and nothing to load. A script that failed to load would
    leave the panel closed with no way to open it, and a closed panel reads as
    a pair with nothing behind it.
    """
    assert check_constraints(page) == []
    assert "<script src" not in page
    assert '<details class="pair">' in page

    bodies = details(page)

    assert len(bodies) == 28
    for body in bodies:
        assert "<summary" in body
        assert "<script" not in body
        assert "<img" not in body


# --- criteria 8 and 9: what the grid says it does not know ------------------


def test_a_pair_carrying_a_blocker_is_marked_in_the_grid(report: BiasReport) -> None:
    """One pair blocked, so the marker separates it from the other 27."""
    marked = with_blockers(report, 1, "cost")
    blocked = marked.pairs[0].pair
    printed = cells(render_dashboard(marked))

    assert "marked" in printed[blocked][0]
    assert "marked" in printed[blocked[3:] + blocked[:3]][0]
    others = [
        pair
        for pair, (classes, _) in printed.items()
        if pair not in {blocked, blocked[3:] + blocked[:3]}
    ]
    assert len(others) == 54
    for pair in others:
        assert "marked" not in printed[pair][0]


def test_a_blocker_on_every_pair_is_a_run_condition_rather_than_28_rows(
    report: BiasReport,
) -> None:
    """The all-rows rule: printed once in the header, not on the cells.

    ``docs/interfaces.md`` states it for both renderers. An offline run carries
    ``event:unchecked`` on all 28 pairs, and repeating it on every cell is the
    density at which the morning review carries on in form and stops in
    substance.
    """
    marker = "event" + UNCHECKED_SUFFIX
    page = render_dashboard(with_blockers(report, 28, marker))
    conditions = re.search(
        r'<p class="meta run-conditions">(.*?)</p>', page, flags=re.S
    )

    assert conditions is not None, "no run conditions rendered"
    assert f"{marker}, 28 of 28" in conditions.group(1)
    for classes, _ in cells(page).values():
        assert "marked" not in classes


def test_a_run_condition_is_not_repeated_in_the_pair_detail_either(
    report: BiasReport,
) -> None:
    """The rule is about the marker, not about the grid.

    Repeating a run condition on all 28 detail panels is the same failure as
    repeating it on all 28 cells, one scroll further down: the reader learns
    nothing from a marker that separates no pair from any other.
    """
    marker = "event" + UNCHECKED_SUFFIX
    every = render_dashboard(with_blockers(report, 28, marker))
    all_but_one = render_dashboard(with_blockers(report, 27, marker))

    for body in details(every):
        assert marker not in body
        assert "marked" not in body

    marked = [body for body in details(all_but_one) if marker in body]
    assert len(marked) == 27


def test_a_blocker_on_all_but_one_pair_stays_on_the_rows(report: BiasReport) -> None:
    """27 of 28 is not every pair, and the pair that differs is the one to see.

    A threshold here would be a free parameter deciding what the reader is not
    told. This is the strict half of the same rule as the test above, and the
    two together are the reason the rule is not "most rows".
    """
    marker = "event" + UNCHECKED_SUFFIX
    page = render_dashboard(with_blockers(report, 27, marker))
    printed = cells(page)
    conditions = re.search(
        r'<p class="meta run-conditions">(.*?)</p>', page, flags=re.S
    )

    assert conditions is not None
    assert f"{marker}, 27 of 28" in conditions.group(1)
    assert sum("marked" in classes for classes, _ in printed.values()) == 54


def test_every_blocker_kind_present_carries_its_count_against_the_total(
    report: BiasReport,
) -> None:
    """Two kinds at different counts, so one count cannot stand for both."""
    pairs = tuple(
        replace(row, blockers=("cost",) if index < 3 else ("no_edge",))
        for index, row in enumerate(report.pairs)
    )
    page = render_dashboard(replace(report, pairs=pairs))
    conditions = re.search(
        r'<p class="meta run-conditions">(.*?)</p>', page, flags=re.S
    )

    assert conditions is not None
    assert "cost, 3 of 28" in conditions.group(1)
    assert "no_edge, 25 of 28" in conditions.group(1)


def test_a_pair_carrying_two_markers_of_one_kind_counts_once(
    report: BiasReport,
) -> None:
    """The figure answers how many pairs are affected, not how many strings.

    A run whose calendar names two releases on one pair carries two ``event``
    markers there. Counted as strings the header reads 29 of 28, which is a
    wrong number in the one panel a reader checks the run against.
    """
    pairs = tuple(
        replace(row, blockers=("event: Core CPI at 12:30", "event: FOMC at 18:00"))
        if index == 0
        else row
        for index, row in enumerate(report.pairs)
    )
    page = render_dashboard(replace(report, pairs=pairs))
    conditions = re.search(
        r'<p class="meta run-conditions">(.*?)</p>', page, flags=re.S
    )

    assert conditions is not None
    assert "event, 1 of 28" in conditions.group(1)


def test_an_observation_prints_the_date_it_was_released(
    report: BiasReport, page: str
) -> None:
    """The release date is what a historical read filters on, so it is shown.

    Printed beside the period rather than instead of it: the period is what the
    figure describes and the release date is when anyone could have acted on
    it, and a reader given one of the two will read it as the other.
    """
    scores = {row.currency: row for row in report.currencies}
    observation = scores["EUR"].pillars[PillarName.MONETARY].inputs[0]

    assert observation.released_at is not None
    body = detail_for(page, "EURUSD")

    assert f"released {observation.released_at:%Y-%m-%d}" in body
    assert str(observation.period) in body


def test_a_pair_whose_legs_the_run_did_not_score_says_so(report: BiasReport) -> None:
    """No coverage figure is not a coverage figure of zero, or of one.

    A run can hold a pair whose currencies carry no `fbe.types.CurrencyScore`
    at all. Printing ``100%`` there claims a full picture nobody has, printing
    ``0%`` claims a measurement that was never taken, and fading the cell says
    the run knows the coverage is low. It says neither instead.
    """
    unscored = replace(report, currencies=())
    page = render_dashboard(unscored)
    body = detail_for(page, "EURUSD")

    assert "EUR not scored" in body
    assert "USD not scored" in body
    assert "thin" not in cells(page)["EURUSD"][0]


def test_a_run_with_no_blockers_renders_no_run_conditions(report: BiasReport) -> None:
    """An empty panel headed "run conditions" reads as a condition nobody named.

    Asserted against the same run carrying one blocker, so the test cannot pass
    by there being no such panel on any page at all.
    """
    clean = replace(
        report, pairs=tuple(replace(row, blockers=()) for row in report.pairs)
    )

    assert "run-conditions" not in render_dashboard(clean)
    assert "run-conditions" in render_dashboard(with_blockers(clean, 1, "cost"))


def test_a_pair_at_reduced_coverage_shows_its_coverage(
    report: BiasReport, page: str
) -> None:
    """Criterion 9, with two different figures so one cannot stand for both.

    The fixture puts GBP at 60% and AUD at 86% deliberately: a page printing one
    thin currency's coverage everywhere passes a test that looks at one pair.
    """
    scores: dict[str, CurrencyScore] = {row.currency: row for row in report.currencies}
    assert scores["GBP"].coverage == 0.6
    assert scores["AUD"].coverage == 0.86

    assert "GBP 60%" in detail_for(page, "GBPUSD")
    assert "AUD 86%" in detail_for(page, "AUDUSD")
    assert "GBP 60%" in detail_for(page, "GBPAUD")
    assert "AUD 86%" in detail_for(page, "GBPAUD")


def test_a_cell_whose_leg_is_below_full_coverage_is_marked(
    report: BiasReport, page: str
) -> None:
    """The mark is in the grid, where the reader is looking, not only below it."""
    printed = cells(page)

    assert "thin" in printed["GBPUSD"][0]
    assert "thin" in printed["USDGBP"][0]
    assert "thin" in printed["GBPAUD"][0]
    assert "thin" not in printed["USDNZD"][0]
    assert "thin" not in printed["NZDUSD"][0]


# --- the standing rule on claims -------------------------------------------


@pytest.mark.parametrize("word", FORBIDDEN)
def test_the_expansion_claims_no_measured_result(page: str, word: str) -> None:
    """The sections this issue adds are checked on their own.

    The whole-page test lives in ``tests/test_dashboard_render.py``. This one
    fails on the expansion alone, so a phrase added there is not hidden by a
    page-wide assertion that someone later narrows.
    """
    bodies = details(page)

    assert bodies, "no expansion rendered, so this asserts nothing"
    for body in bodies:
        assert word not in body.lower()
