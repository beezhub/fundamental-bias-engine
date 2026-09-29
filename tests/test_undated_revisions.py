"""A revision must carry its release date: issue #121, ADR 0007.

The engine is careful never to read a number before it was published, and that
check has two paths. With a ``released_at`` it uses the fact. Without one it
assumes the figure appeared a fixed number of days after the period it
describes, which is what `fbe.datasources.registry.publication_lag` resolves.

The second path cannot tell two vintages of one period apart. A June CPI print
and its September revision both describe June, so both become visible on the
same day, and `BasePillar._newest_vintages` then prefers the revision on its
``revision`` number. A run dated 16 July reads a figure that did not exist until
September, and the direction is the one that matters: the engine is honest
exactly when a source stamps its data and flatters when it does not.

The ruling on #121 differs from the issue's own proposal and this file tests the
ruling. Dropping every unstamped revision downstream would be right for a
backtest and wrong for a live run, where the newest vintage is the only one that
matters and a source republishing a correction without a stamp would have it
ignored for ever. So the shape is refused where observations are built, loudly,
and the pillar's visibility rule refuses it a second time in case something
bypassed the first: downstream must not depend on upstream validation having
run, and the two halves fail in the same direction.

Nothing here reaches the network. The manual cases write their own files under
``tmp_path``.
"""

from __future__ import annotations

import inspect
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbe.config import DataConfig
from fbe.datasources import registry
from fbe.datasources.base import (
    SourceError,
    UndatedRevisionError,
    checked_vintage,
)
from fbe.datasources.manual import ManualSource
from fbe.datasources.registry import DEFAULT_PUBLICATION_LAG_DAYS
from fbe.pillars.base import BasePillar
from fbe.pillars.inflation import InflationPillar
from fbe.types import Frequency, Observation

INDICATOR = "cpi_yoy"
CURRENCY = "EUR"
PERIOD = date(2026, 6, 1)
ASOF = date(2026, 7, 16)
"""The run date from the issue: ``PERIOD`` plus the leg's 45-day monthly lag.

Both vintages are visible under the assumed-lag rule on exactly this date, which
is what makes the leak reproducible rather than occasional.
"""

REVISED_ON = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
"""When the September revision was really published, two months after the run."""

ORIGINAL = 2.0
REVISED = 3.5

DOCS = Path(__file__).resolve().parents[1] / "docs" / "data-sources.md"


def observation(
    *,
    value: float,
    revision: int = 0,
    released_at: datetime | None = None,
) -> Observation:
    """One vintage of the issue's June CPI print."""
    ref = registry.series_for(INDICATOR, CURRENCY)
    return Observation(
        indicator=INDICATOR,
        currency=CURRENCY,
        value=value,
        period=PERIOD,
        source=ref.source,
        series_id=ref.series_id,
        unit=ref.unit,
        frequency=Frequency.MONTHLY,
        released_at=released_at,
        revision=revision,
    )


def flat(text: str | None) -> str:
    """One line, whitespace collapsed.

    Docstrings wrap at 88 columns, so a phrase under test can be split across a
    line break. The contract is the sentence, not where it wrapped.
    """
    return " ".join((text or "").split())


def attribute_entry(docstring: str | None, name: str) -> str:
    """One ``Attributes:`` entry out of a Google-style docstring.

    Scoped to the entry rather than run over the whole docstring, because both
    words this rule is about appear elsewhere in `Observation`'s and a docstring
    that mentions each somewhere says nothing about the rule between them.
    """
    text = docstring or ""
    assert f"{name}:" in text, name
    after = text.split(f"{name}:", 1)[1]
    lines: list[str] = []
    for line in after.splitlines():
        if lines and line.strip() and not line.startswith(" " * 8):
            break
        lines.append(line)
    return " ".join(lines)


def attached_docstring(source: str, marker: str) -> str:
    """The docstring written under ``marker`` in ``source``.

    Reading the attribute docstring rather than the module's, because a rule
    stated in a module header is not what a reader looking up one constant or
    one field arrives at.
    """
    assert marker in source, marker
    return source.split(marker, 1)[1].split('"""')[1]


def extracted(*observations: Observation) -> list[tuple[float, int]]:
    """``(value, revision)`` for each June CPI vintage the pillar admits.

    Through the real `InflationPillar`, whose ``_extract`` is the base class's,
    rather than through a double: the rule under test is the base class's own
    and a double would only be testing the copy in this file.
    """
    series = InflationPillar()._extract(observations, (CURRENCY,), ASOF)
    return [(row.value, row.revision) for row in series[CURRENCY][INDICATOR]]


def manual_file(rows: str) -> str:
    """One manual YAML file holding ``rows``."""
    body = textwrap.indent(textwrap.dedent(rows).strip(), "  ")
    return (
        f'meta:\n  source: "operator"\n  updated: 2026-06-30\n\nobservations:\n{body}\n'
    )


def write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# --- criteria 4 to 6: the vintage a run could actually read ------------------


def test_an_unstamped_revision_is_not_read_at_the_original_print_date() -> None:
    """The issue's own reproduction, to its numbers.

    Revision 2 describes June and was published in September. Admitted at the
    original print's date it reaches `MonetaryPillar._transform` as a real
    policy rate 1.5 percentage points away from anything a July run could have
    computed, inside a component holding 0.15 of the heaviest pillar, and in the
    flattering direction.
    """
    admitted = extracted(
        observation(value=ORIGINAL),
        observation(value=REVISED, revision=2),
    )

    assert admitted == [(ORIGINAL, 0)]


def test_a_revision_stamped_after_the_run_is_still_not_read() -> None:
    """The case that already worked, kept working.

    With the real September stamp the ordinary visibility rule does the work:
    the revision is simply not published yet on 16 July. If this breaks, the fix
    has replaced a working rule rather than filled the hole beside it.
    """
    admitted = extracted(
        observation(value=ORIGINAL),
        observation(value=REVISED, revision=2, released_at=REVISED_ON),
    )

    assert admitted == [(ORIGINAL, 0)]


def test_a_revision_published_before_the_run_is_read() -> None:
    """The live shape, which a blanket refusal of revisions would break.

    On a live run the newest vintage is the only one that matters. A source that
    republishes a corrected figure, dated, must have the correction used, or the
    engine scores today's currency off a superseded number and says nothing
    about it. That is the defect the ruling rejected the issue's proposal for.
    """
    admitted = extracted(
        observation(value=ORIGINAL),
        observation(
            value=REVISED,
            revision=2,
            released_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        ),
    )

    assert admitted == [(REVISED, 2)]


def test_the_original_is_still_read_when_it_is_the_only_vintage() -> None:
    """The refusal is of the shape, not of the period.

    A rule that dropped the period along with its undated revision would answer
    the look-ahead question by having no data, which is the other way to get a
    wrong number: the original print is a figure the run could genuinely read.
    """
    admitted = extracted(observation(value=ORIGINAL, revision=0))

    assert admitted == [(ORIGINAL, 0)]


# --- the second half: refused, and counted rather than silently dropped -----


def test_the_visibility_rule_itself_refuses_the_shape() -> None:
    """The predicate where the rule lives, not only through a pillar.

    `_extract` asks `_visible` and then classifies the refusal, so this is the
    assertion that says which of the two decided. An overriding pillar calls
    `_visible` directly, and RISK already does through ``super()``.
    """
    undated = observation(value=REVISED, revision=2)
    dated = observation(value=REVISED, revision=2, released_at=REVISED_ON)
    original = observation(value=ORIGINAL)

    assert BasePillar._visible(undated, ASOF) is False
    assert BasePillar._visible(undated, date(2030, 1, 1)) is False
    assert BasePillar._visible(dated, date(2026, 9, 16)) is True
    assert BasePillar._visible(original, ASOF) is True


def test_a_refused_revision_is_counted_on_the_score() -> None:
    """Belt and braces from the ruling: reaching here at all is reported.

    The source boundary is where this shape is supposed to die. If one gets
    past it, the pillar refuses it and says how many it refused, because a
    coverage gap that nothing reports is a gap nobody can act on and the two
    halves have to fail in the same direction.
    """
    pillar = InflationPillar()
    universe = (CURRENCY,)
    observations = (
        observation(value=ORIGINAL),
        observation(value=REVISED, revision=2),
        observation(value=REVISED, revision=3),
    )

    scores = pillar.compute(observations, universe, ASOF)

    assert scores[CURRENCY].diagnostics["undated_revisions"] == pytest.approx(2.0)


def test_a_clean_run_reports_zero_refusals_rather_than_no_key() -> None:
    """Always present, like ``assumed_lag_inputs`` beside it.

    A key that disappears at zero cannot be read as zero by anything
    downstream, which is the absence-versus-value confusion the pillar module
    exists to avoid.
    """
    scores = InflationPillar().compute(
        (observation(value=ORIGINAL),), (CURRENCY,), ASOF
    )

    assert scores[CURRENCY].diagnostics["undated_revisions"] == 0.0


# --- criterion 1: the source boundary, with its own exception type ----------


def test_the_gate_refuses_a_revision_with_no_release_date() -> None:
    """Criterion 1. One gate, called by every construction path there is."""
    with pytest.raises(UndatedRevisionError, match="revision 2"):
        checked_vintage(observation(value=REVISED, revision=2))


def test_the_gate_accepts_a_dated_revision() -> None:
    """A revision is a legitimate thing for a source to publish, with a date."""
    built = checked_vintage(
        observation(value=REVISED, revision=2, released_at=REVISED_ON)
    )

    assert (built.revision, built.released_at) == (2, REVISED_ON)


def test_the_gate_leaves_an_unrevised_observation_alone() -> None:
    """Most sources publish no stamp at all, and that path is untouched.

    An original print with no ``released_at`` is admitted on the assumed lag and
    counted in ``assumed_lag_inputs``. Refusing it here would refuse most of the
    universe.
    """
    built = checked_vintage(observation(value=ORIGINAL))

    assert (built.revision, built.released_at) == (0, None)


def test_a_fetching_source_builds_through_the_gate(tmp_path: Path) -> None:
    """The shared builder, not only the validator, is on the gated path.

    Six sources call `BaseDataSource._observation` and none of them sets a
    revision today. The gate is there so the seventh, or the first of these to
    start reading a revised series, cannot express the shape without a date.
    """
    source = ManualSource(
        DataConfig(cache_dir=tmp_path / "cache", manual_dir=tmp_path, offline=True)
    )
    ref = registry.series_for(INDICATOR, CURRENCY)

    with pytest.raises(UndatedRevisionError, match="revision 3"):
        source._observation(
            INDICATOR, CURRENCY, ref, PERIOD, REVISED, released_at=None, revision=3
        )

    built = source._observation(
        INDICATOR, CURRENCY, ref, PERIOD, REVISED, released_at=REVISED_ON, revision=3
    )

    assert (built.revision, built.released_at) == (3, REVISED_ON)


def test_the_refusal_has_its_own_type_that_a_narrow_except_cannot_swallow() -> None:
    """Criterion 1's second half, which is about what catches it.

    A bare ``ValueError`` is swallowed by every parser that wraps a bad row, and
    an operator mistake would then read as a malformed number. This carries its
    own name, and it is a `SourceError` because it is a fact about what a source
    supplied: the collector reports a source's failure rather than dropping it,
    which is how this reaches a person.
    """
    assert issubclass(UndatedRevisionError, SourceError)
    assert not issubclass(UndatedRevisionError, ValueError)
    assert not issubclass(UndatedRevisionError, KeyError)


# --- criterion 2: the hand-typed route in ----------------------------------


def test_a_manual_row_claiming_a_revision_with_no_date_is_refused(
    tmp_path: Path,
) -> None:
    """Criterion 2. The message names the file and the row, as the others do.

    ``data/manual`` is the only place in ``src/fbe`` that sets ``revision`` at
    all, so this is the live route into the defect. A correction typed without a
    date is an operator mistake, and the only response that gets the date into
    the file is to say so.
    """
    manual = tmp_path / "manual"
    path = write(
        manual,
        "cpi.yaml",
        manual_file(
            f"""
            - indicator: {INDICATOR}
              currency: {CURRENCY}
              value: {REVISED}
              period: {PERIOD}
              revision: 2
            """
        ),
    )
    source = ManualSource(
        DataConfig(cache_dir=tmp_path / "cache", manual_dir=manual, offline=True)
    )

    with pytest.raises(UndatedRevisionError) as error:
        list(source.load_file(path))

    assert "cpi.yaml observation 0" in str(error.value)
    assert "released_at" in str(error.value)


def test_a_manual_row_with_a_dated_revision_loads(tmp_path: Path) -> None:
    """The fix is not a ban on typed corrections, only on undated ones."""
    manual = tmp_path / "manual"
    path = write(
        manual,
        "cpi.yaml",
        manual_file(
            f"""
            - indicator: {INDICATOR}
              currency: {CURRENCY}
              value: {REVISED}
              period: {PERIOD}
              revision: 2
              released_at: 2026-09-15T09:00:00Z
            """
        ),
    )
    source = ManualSource(
        DataConfig(cache_dir=tmp_path / "cache", manual_dir=manual, offline=True)
    )

    rows = list(source.load_file(path))

    assert [(row.value, row.revision) for row in rows] == [(REVISED, 2)]


def test_a_manual_row_with_no_revision_still_needs_no_date(tmp_path: Path) -> None:
    """The ordinary hand-typed row, which is most of ``data/manual``."""
    manual = tmp_path / "manual"
    path = write(
        manual,
        "cpi.yaml",
        manual_file(
            f"""
            - indicator: {INDICATOR}
              currency: {CURRENCY}
              value: {ORIGINAL}
              period: {PERIOD}
            """
        ),
    )
    source = ManualSource(
        DataConfig(cache_dir=tmp_path / "cache", manual_dir=manual, offline=True)
    )

    rows = list(source.load_file(path))

    assert [(row.value, row.revision, row.released_at) for row in rows] == [
        (ORIGINAL, 0, None)
    ]


# --- criteria 3, 7 and 9: the rule is written where it is read --------------


def test_the_visibility_rule_states_that_an_undated_revision_is_not_visible() -> None:
    """Criterion 3. The hole was in the rule, so the rule has to say it is closed.

    A reader implementing a pillar reads ``_extract``'s docstring, and the
    previous version of it named this hole as open and pointed at #121.
    """
    rule = flat(inspect.getdoc(BasePillar._extract))
    visible = flat(inspect.getdoc(BasePillar._visible))

    for text in (rule, visible):
        assert "revision" in text
        assert "not visible" in text
        assert "undated_revisions" in text
    assert "which is issue #121" not in rule, "the rule still calls the hole open"


def test_the_lag_table_says_what_it_can_and_cannot_date() -> None:
    """Criterion 3's second half, on the number the assumption comes from.

    The two rules were individually correct and jointly wrong, and this is the
    sentence whose absence allowed that: an estimate of when a print appeared
    cannot date a correction to it. Read off the constant's own docstring,
    because that is what a reader following the visibility rule arrives at.
    """
    docstring = flat(
        attached_docstring(
            inspect.getsource(registry), "DEFAULT_PUBLICATION_LAG_DAYS: Mapping"
        )
    )

    assert "original print" in docstring
    assert "revision" in docstring
    assert set(DEFAULT_PUBLICATION_LAG_DAYS) == set(Frequency)


def test_the_observation_contract_states_the_requirement() -> None:
    """Criterion 9. Every source reads this docstring, including future ones.

    Docstring only: the field, its type and its default are untouched, so no
    consumer needs updating in the same commit.
    """
    entry = flat(attribute_entry(inspect.getdoc(Observation), "released_at"))

    assert "revision" in entry
    assert "original print" in entry
    assert "must carry one" in entry


def test_the_data_sources_document_states_the_requirement() -> None:
    """Criterion 7. The operator typing a correction reads this file.

    Asserted on one paragraph rather than on the whole document: both words
    appear in `docs/data-sources.md` already, several sections apart, and a
    document that mentions each somewhere says nothing about the rule between
    them.
    """
    paragraphs = [
        block
        for block in DOCS.read_text(encoding="utf-8").split("\n\n")
        if "revision" in block and "released_at" in block
    ]

    assert paragraphs, "no paragraph states the rule for a typed correction"
    assert any("must" in block for block in paragraphs)
