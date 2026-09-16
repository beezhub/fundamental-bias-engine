"""Tests for the operator-entered source.

Hand-typed data is the most likely thing in this repository to be wrong and the
least likely to announce it, so almost every test here is about a refusal. The
failure this module guards against is the second row of the prime directive
table read one layer up: not a missing number, but a present one carrying a
label that does not describe it. A PMI typed as a percentage still scores, and
scores wrongly.

Nothing here reaches the network, and nothing here reads the repository's own
``data/manual``. Every case writes its own files under ``tmp_path``.

The expected sets are derived from `fbe.datasources.registry` rather than
written out, because the registry is what `missing` and `refs` are contracts
against. A written-out list would pass after a rename that broke the code.
"""

from __future__ import annotations

import socket
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fbe.config import DataConfig
from fbe.datasources.base import SourceError
from fbe.datasources.manual import FILE_GLOB, ManualSource
from fbe.datasources.registry import INDICATORS, SOURCE_MANUAL
from fbe.types import Frequency
from fbe.universe import G10

ASOF = date(2026, 9, 14)

# A monthly index with a manual ref for every currency, so a case needing a
# straightforward row does not also depend on a currency-specific quirk.
PMI = "pmi_composite"


def _config(tmp_path: Path, *, manual: str = "manual") -> DataConfig:
    return DataConfig(
        cache_dir=tmp_path / "cache",
        manual_dir=tmp_path / manual,
        offline=True,
    )


def _write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def _file(rows: str, *, meta: str = 'source: "operator"\n  updated: 2026-09-08') -> str:
    body = textwrap.indent(textwrap.dedent(rows).strip(), "  ")
    return f"meta:\n  {meta}\n\nobservations:\n{body}\n"


PMI_ROW = """
- indicator: pmi_composite
  currency: EUR
  value: 49.8
  period: 2026-08-01
"""


@pytest.fixture
def source(tmp_path: Path) -> ManualSource:
    return ManualSource(_config(tmp_path))


@pytest.fixture
def manual_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "manual"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# --- the happy path ---------------------------------------------------------


def test_a_documented_row_becomes_an_observation(
    source: ManualSource, manual_dir: Path
) -> None:
    """The schema in the module docstring and in docs/data-sources.md, parsed."""
    path = _write(manual_dir, "pmi.yaml", _file(PMI_ROW))

    emitted = source.load_file(path)

    assert len(emitted) == 1
    observation = emitted[0]
    assert observation.indicator == PMI
    assert observation.currency == "EUR"
    assert observation.value == 49.8
    assert observation.period == date(2026, 8, 1)


def test_the_source_key_says_the_number_was_typed(
    source: ManualSource, manual_dir: Path
) -> None:
    """A report has to be able to separate typed numbers from fetched ones, and
    `source` is the only field that can carry that."""
    path = _write(manual_dir, "pmi.yaml", _file(PMI_ROW))

    assert source.load_file(path)[0].source == SOURCE_MANUAL


def test_the_series_id_is_the_key_the_file_named(
    source: ManualSource, manual_dir: Path
) -> None:
    """A hand-typed number has no vendor identifier. The indicator key the
    operator wrote is the only identifier it has, and putting a fetched
    source's series ID on it would claim a provenance it does not have."""
    path = _write(manual_dir, "pmi.yaml", _file(PMI_ROW))

    assert source.load_file(path)[0].series_id == PMI


def test_the_unit_and_frequency_come_from_the_registry(
    source: ManualSource, manual_dir: Path
) -> None:
    """Not from the file, even when the file states them, so the four fields a
    pillar reads cannot drift from the registry that coverage is computed
    against."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          unit: index
          frequency: monthly
        """),
    )
    ref = INDICATORS[PMI].series["EUR"]

    observation = source.load_file(path)[0]

    assert observation.unit == ref.unit
    assert observation.frequency == ref.frequency


def test_a_full_row_carries_every_optional_field(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          unit: index
          frequency: monthly
          released_at: 2026-09-01T08:00:00Z
          revision: 2
          meta:
            release: flash
            source_url: "https://example.invalid/print"
        """),
    )

    observation = source.load_file(path)[0]

    assert observation.released_at == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert observation.revision == 2
    assert observation.meta["release"] == "flash"


def test_an_empty_observations_list_is_data_not_an_error(
    source: ManualSource, manual_dir: Path
) -> None:
    """A topic file the operator has not filled in yet holds no observations,
    which is a coverage gap and not a malformed file."""
    path = _write(
        manual_dir, "pmi.yaml", 'meta:\n  source: "operator"\n\nobservations: []\n'
    )

    assert list(source.load_file(path)) == []


# --- refusals: the registry decides what a row may say -----------------------


def test_an_unknown_indicator_raises(source: ManualSource, manual_dir: Path) -> None:
    """A typo that silently creates a new indicator is invisible until a pillar
    quietly reports missing data."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_manufacturing
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "pmi_manufacturing" in str(caught.value)
    assert "pmi.yaml" in str(caught.value)


def test_an_unknown_indicator_is_not_skipped_with_the_rest_kept(
    source: ManualSource, manual_dir: Path
) -> None:
    """Stated separately because returning the one good row would look like a
    working file and lose the bad one without saying so."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        - indicator: pmi_manufacturing
          currency: GBP
          value: 51.1
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


def test_a_currency_outside_the_universe_raises(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: ZAR
          value: 49.8
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "ZAR" in str(caught.value)


@pytest.mark.parametrize("currency", [*G10, "GLOBAL"])
def test_every_universe_currency_and_global_are_accepted(
    source: ManualSource, manual_dir: Path, currency: str
) -> None:
    """The converse of the test above, so the refusal is not passing because
    the currency check refuses everything."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: {currency}
          value: 49.8
          period: 2026-08-01
        """),
    )

    assert source.load_file(path)[0].currency == currency


def test_a_unit_that_contradicts_the_registry_raises(
    source: ManualSource, manual_dir: Path
) -> None:
    """A PMI entered as a percentage instead of an index will otherwise score,
    and score wrongly. This is the reason the source validates at all."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          unit: percent
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "percent" in str(caught.value)
    assert "index" in str(caught.value)


def test_a_frequency_that_contradicts_the_registry_raises(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          frequency: quarterly
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "quarterly" in str(caught.value)
    assert "monthly" in str(caught.value)


@pytest.mark.parametrize("field", ["indicator", "currency", "value", "period"])
def test_a_missing_required_field_raises(
    source: ManualSource, manual_dir: Path, field: str
) -> None:
    written = {
        "indicator": "pmi_composite",
        "currency": "EUR",
        "value": "49.8",
        "period": "2026-08-01",
    }
    del written[field]
    lines = [f"{key}: {value}" for key, value in written.items()]
    rows = "- " + "\n  ".join(lines)
    path = _write(manual_dir, "pmi.yaml", _file(rows))

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert field in str(caught.value)


def test_a_null_value_is_refused_rather_than_read_as_zero(
    source: ManualSource, manual_dir: Path
) -> None:
    """``value:`` with nothing after it is YAML null. Read as 0.0 it is a real
    number the operator never typed, which for an index centred on 50 is the
    most bearish print possible."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value:
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "value" in str(caught.value)


def test_a_non_numeric_value_is_refused(source: ManualSource, manual_dir: Path) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: "forty nine"
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


@pytest.mark.parametrize("written", ["nan", ".nan", ".inf", "-.inf"])
def test_a_non_finite_value_is_refused(
    source: ManualSource, manual_dir: Path, written: str
) -> None:
    """A nan reaching a cross-sectional pillar makes the mean and the standard
    deviation nan for all eight currencies at once, so every threshold
    comparison silently reads False and nothing shows as a coverage gap."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: EUR
          value: {written}
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


def test_an_unreadable_period_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: "August 2026"
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "period" in str(caught.value)


def test_malformed_yaml_raises_naming_the_file(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(manual_dir, "pmi.yaml", "observations: [oh dear\n")

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "pmi.yaml" in str(caught.value)


def test_a_file_without_an_observations_list_raises(
    source: ManualSource, manual_dir: Path
) -> None:
    """An empty list is a file with nothing in it yet. A missing key is a file
    whose shape is wrong, and the two need different answers."""
    path = _write(manual_dir, "pmi.yaml", 'meta:\n  source: "operator"\n')

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "observations" in str(caught.value)


def test_an_unknown_row_field_raises(source: ManualSource, manual_dir: Path) -> None:
    """``releasedat`` for ``released_at`` would otherwise be accepted and
    dropped, and the observation would silently carry no release date."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          releasedat: 2026-09-01T08:00:00Z
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "releasedat" in str(caught.value)


def test_a_differenced_indicator_takes_its_canonical_unit(
    source: ManualSource, manual_dir: Path
) -> None:
    """`yield_2y_chg_1m` for CHF has a ref publishing percent under an
    indicator declared in basis points, because the ref is the input a source
    differences and the indicator is the value after. An operator types the
    already-differenced figure, so the indicator's unit is the one that
    describes it.

    An earlier draft refused these outright and claimed `FredSource` does the
    same. It does not: FRED maps `yoy` to `pc1` and `diff` to `chg` and fetches
    them, and refuses `chg_1m` and `chg_3m` alone for a reason about FRED's own
    `chg` parameter that has no bearing on a typed number.
    """
    spec = INDICATORS["yield_2y_chg_1m"]
    assert spec.series["CHF"].unit == "percent"
    assert spec.unit == "basis_points"
    path = _write(
        manual_dir,
        "yields.yaml",
        _file("""
        - indicator: yield_2y_chg_1m
          currency: CHF
          value: -12.0
          period: 2026-08-03
        """),
    )

    assert source.load_file(path)[0].unit == "basis_points"


# --- released_at ------------------------------------------------------------


def test_released_at_is_none_when_the_file_omits_it(
    source: ManualSource, manual_dir: Path
) -> None:
    """Never substituted from ``period``. The period a figure describes and the
    day it was published are different facts, and conflating them is the
    look-ahead bias Phase 6 has to avoid."""
    path = _write(manual_dir, "pmi.yaml", _file(PMI_ROW))

    observation = source.load_file(path)[0]

    assert observation.released_at is None


def test_released_at_is_kept_when_the_file_gives_it(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          released_at: 2026-09-01T08:00:00Z
        """),
    )

    assert source.load_file(path)[0].released_at == datetime(
        2026, 9, 1, 8, 0, tzinfo=UTC
    )


def test_a_release_date_without_a_time_is_read_as_a_date(
    source: ManualSource, manual_dir: Path
) -> None:
    """YAML reads an unquoted ``2026-09-01`` as a date, not a datetime, and an
    operator will write it that way."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          released_at: 2026-09-01
        """),
    )

    released_at = source.load_file(path)[0].released_at

    assert released_at is not None
    assert released_at.date() == date(2026, 9, 1)


def test_an_unreadable_release_date_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          released_at: "last Tuesday"
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "released_at" in str(caught.value)


# --- file order and overriding ----------------------------------------------


def test_a_later_file_overrides_an_earlier_one(
    source: ManualSource, manual_dir: Path
) -> None:
    """The point of the rule: patch one value without editing a large file."""
    _write(
        manual_dir,
        "a-pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )
    _write(
        manual_dir,
        "b-overrides.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )

    emitted = source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)

    assert [o.value for o in emitted] == [50.4]


def test_the_override_is_by_filename_order_not_by_directory_order(
    source: ManualSource, manual_dir: Path
) -> None:
    """Written in the order that would win if the loader took the filesystem's
    order, so a loader that does not sort fails here."""
    _write(
        manual_dir,
        "z-late.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )
    _write(
        manual_dir,
        "a-early.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    emitted = source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)

    assert [o.value for o in emitted] == [50.4]


def test_an_override_replaces_only_the_same_indicator_currency_and_period(
    source: ManualSource, manual_dir: Path
) -> None:
    """A neighbouring period and a neighbouring currency both survive, so a
    loader keying the override too broadly loses rows and fails here."""
    _write(
        manual_dir,
        "a-pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        - indicator: pmi_composite
          currency: EUR
          value: 49.1
          period: 2026-07-01
        - indicator: pmi_composite
          currency: GBP
          value: 51.1
          period: 2026-08-01
        """),
    )
    _write(
        manual_dir,
        "b-overrides.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )

    emitted = source.fetch([PMI], ["EUR", "GBP"], date(2026, 7, 1), ASOF)
    by_key = {(o.currency, o.period): o.value for o in emitted}

    assert by_key[("EUR", date(2026, 8, 1))] == 50.4
    assert by_key[("EUR", date(2026, 7, 1))] == 49.1
    assert by_key[("GBP", date(2026, 8, 1))] == 51.1


def test_one_key_twice_in_one_file_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    """Across files that is the override rule. Inside one file it has no use
    and is a value typed twice, so resolving it by position would pick one of
    two numbers the operator believes are both entered."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)

    assert "twice" in str(caught.value)


def test_only_the_documented_glob_is_read(
    source: ManualSource, manual_dir: Path
) -> None:
    """A ``.yaml.bak`` an operator left behind must not quietly override the
    file they edited."""
    assert FILE_GLOB == "*.yaml"
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )
    _write(
        manual_dir,
        "pmi.yaml.bak",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 11.1
          period: 2026-08-01
        """),
    )

    assert [o.value for o in source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)] == [
        49.8
    ]


# --- fetch filters ----------------------------------------------------------


def _three_rows(manual_dir: Path) -> None:
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        - indicator: pmi_composite
          currency: GBP
          value: 51.1
          period: 2026-08-01
        - indicator: indpro_yoy
          currency: AUD
          value: 1.4
          period: 2026-04-01
        """),
    )


def test_fetch_returns_only_the_indicators_asked_for(
    source: ManualSource, manual_dir: Path
) -> None:
    _three_rows(manual_dir)

    emitted = source.fetch([PMI], list(G10), date(2026, 1, 1), ASOF)

    assert {o.indicator for o in emitted} == {PMI}


def test_fetch_returns_only_the_currencies_asked_for(
    source: ManualSource, manual_dir: Path
) -> None:
    _three_rows(manual_dir)

    emitted = source.fetch([PMI, "indpro_yoy"], ["EUR"], date(2026, 1, 1), ASOF)

    assert {o.currency for o in emitted} == {"EUR"}


def test_fetch_applies_the_window_inclusively(
    source: ManualSource, manual_dir: Path
) -> None:
    _three_rows(manual_dir)

    inside = source.fetch([PMI], ["EUR"], date(2026, 8, 1), date(2026, 8, 1))
    before = source.fetch([PMI], ["EUR"], date(2026, 8, 2), ASOF)
    after = source.fetch([PMI], ["EUR"], date(2026, 1, 1), date(2026, 7, 31))

    assert len(inside) == 1
    assert list(before) == []
    assert list(after) == []


def test_fetch_returns_nothing_for_an_empty_directory(
    source: ManualSource, manual_dir: Path
) -> None:
    """An operator who has typed nothing in yet has no observations, which is a
    coverage gap and not a failure."""
    assert list(source.fetch([PMI], ["EUR"], date(2026, 1, 1), ASOF)) == []


def test_fetch_raises_when_the_directory_does_not_exist(
    source: ManualSource, tmp_path: Path
) -> None:
    """A directory that is not there is a misconfiguration, not an empty one.
    Answering it with an empty sequence would report as a coverage gap and hide
    the cause, which is what `available()` exists to prevent."""
    assert not (tmp_path / "manual").exists()

    with pytest.raises(SourceError) as caught:
        source.fetch([PMI], ["EUR"], date(2026, 1, 1), ASOF)

    assert "manual" in str(caught.value)


def test_fetch_reaches_no_network(
    source: ManualSource, manual_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enforced guard rather than an assertion about a count.

    The first version of this asserted that two observations came back, which
    another test already covers and which nothing about the network could have
    changed. This fails the moment anything in the call sends a request.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("ManualSource reached the network")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    _three_rows(manual_dir)

    assert len(source.fetch([PMI], ["EUR", "GBP"], date(2026, 1, 1), ASOF)) == 2


# --- available --------------------------------------------------------------


def test_available_is_false_when_the_directory_does_not_exist(
    source: ManualSource, tmp_path: Path
) -> None:
    assert not (tmp_path / "manual").exists()
    assert source.available() is False


def test_available_is_true_for_an_empty_directory(
    source: ManualSource, manual_dir: Path
) -> None:
    """The correct state for a fresh checkout: the engine runs and the report
    shows the gaps, rather than refusing to start. The stub's summary line said
    "and holds at least one file", which contradicts its own body."""
    assert list(manual_dir.iterdir()) == []
    assert source.available() is True


def test_available_is_false_when_the_path_is_a_file(
    source: ManualSource, tmp_path: Path
) -> None:
    (tmp_path / "manual").write_text("not a directory", encoding="utf-8")

    assert source.available() is False


# --- refs -------------------------------------------------------------------


def test_refs_is_derived_from_the_registry(source: ManualSource) -> None:
    expected = {
        (spec.key, currency): ref
        for spec in INDICATORS.values()
        for currency, ref in spec.series.items()
        if ref.source == SOURCE_MANUAL
    }

    assert dict(source.refs()) == expected


def test_refs_is_not_empty(source: ManualSource) -> None:
    """Guards the test above, which would pass vacuously against a `refs` that
    returns nothing whatever the registry says."""
    # 16 manual refs since the RBNZ took NZD's three yield legs off this list.
    assert len(source.refs()) >= 16


def test_refs_names_no_other_source(source: ManualSource) -> None:
    assert {ref.source for ref in source.refs().values()} == {SOURCE_MANUAL}


# --- missing ----------------------------------------------------------------


def _expected_missing() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for spec in INDICATORS.values():
        for currency, ref in spec.series.items():
            if ref.source == SOURCE_MANUAL:
                out.setdefault(spec.key, []).append(currency)
    return {key: tuple(sorted(values)) for key, values in out.items()}


def test_an_empty_directory_reports_every_manual_ref(
    source: ManualSource, manual_dir: Path
) -> None:
    """The operator's to-do list on a fresh checkout is every manual ref there
    is, stated rather than left to show up as a low coverage figure later."""
    assert dict(source.missing(ASOF)) == _expected_missing()


def test_the_empty_directory_report_covers_pmi_for_all_eight(
    source: ManualSource, manual_dir: Path
) -> None:
    """Criterion 8's own case. It names `pmi_manufacturing`; the registry key
    is `pmi_composite`, which is what the fallback table the criterion cites
    uses, and what `registry.py` says it is deliberately called."""
    reported = source.missing(ASOF)

    assert set(reported[PMI]) == set(G10)
    assert set(reported["yield_2y"]) == {"CHF"}


def test_a_current_entry_clears_its_pair(
    source: ManualSource, manual_dir: Path
) -> None:
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    reported = source.missing(ASOF)

    assert "EUR" not in reported[PMI]
    assert set(reported[PMI]) == set(G10) - {"EUR"}


def test_an_indicator_fully_entered_leaves_the_report(
    source: ManualSource, manual_dir: Path
) -> None:
    rows = "\n".join(
        f"- indicator: pmi_composite\n  currency: {c}\n  value: 50.0\n"
        f"  period: 2026-08-01"
        for c in G10
    )
    _write(manual_dir, "pmi.yaml", _file(rows))

    assert PMI not in source.missing(ASOF)


def test_a_stale_entry_is_still_missing(source: ManualSource, manual_dir: Path) -> None:
    """`pmi_composite` carries a 75 day allowance. A March print read in
    September is not a current value, and counting it would turn a visible gap
    into an invisible one."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-03-01
        """),
    )

    assert "EUR" in source.missing(ASOF)[PMI]


def test_the_staleness_allowance_is_the_indicators_own(
    source: ManualSource, manual_dir: Path
) -> None:
    """Read from the `IndicatorSpec`, not from one number for the whole
    registry: the cadences here differ by an order of magnitude, and a
    quarterly figure five months old is routinely the most current there is."""
    allowance = INDICATORS[PMI].max_staleness_days
    assert allowance == 75
    fresh = ASOF - timedelta(days=allowance)
    stale = ASOF - timedelta(days=allowance + 1)

    _write(
        manual_dir,
        "pmi.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: {fresh.isoformat()}
        - indicator: pmi_composite
          currency: GBP
          value: 51.1
          period: {stale.isoformat()}
        """),
    )

    reported = source.missing(ASOF)

    assert "EUR" not in reported[PMI]
    assert "GBP" in reported[PMI]


def test_missing_raises_when_the_directory_does_not_exist(
    source: ManualSource, tmp_path: Path
) -> None:
    with pytest.raises(SourceError):
        source.missing(ASOF)


def test_missing_reports_currencies_in_a_stable_order(
    source: ManualSource, manual_dir: Path
) -> None:
    """So the operator's to-do list does not reshuffle between runs."""
    first = source.missing(ASOF)
    assert first[PMI] == tuple(sorted(first[PMI]))


# --- the documentation this issue corrects ----------------------------------


DOCS = Path(__file__).resolve().parents[1]


def test_the_docs_describe_the_manual_input_as_yaml() -> None:
    """`docs/roadmap.md` and `CLAUDE.md` both said CSV where the module
    docstring, `docs/data-sources.md` and now the loader all say YAML. A spec
    that disagrees with the code is a defect."""
    for name in ("docs/roadmap.md", "CLAUDE.md"):
        text = (DOCS / name).read_text(encoding="utf-8")
        for claim in (
            "manual CSV",
            "Hand-maintained CSV",
            "CSV loader",
            "`data/manual/` CSV",
        ):
            assert claim not in text, f"{name} still says {claim!r}"


# --- gaps the mutation sweep found ------------------------------------------


def test_the_files_are_read_in_sorted_filename_order(
    source: ManualSource, manual_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic, unlike writing two files and hoping the directory hands
    them back in creation order. `Path.glob` here yields the later name first,
    so a loader that does not sort takes the earlier value and fails.
    """
    _write(
        manual_dir,
        "a-early.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )
    _write(
        manual_dir,
        "z-late.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )
    real_glob = Path.glob
    monkeypatch.setattr(
        Path,
        "glob",
        lambda self, pattern, **kw: reversed(sorted(real_glob(self, pattern, **kw))),
    )

    emitted = source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)

    assert [o.value for o in emitted] == [50.4]


def test_the_other_yaml_spelling_is_refused_rather_than_skipped(
    source: ManualSource, manual_dir: Path
) -> None:
    """A ``.yml`` file is read by nobody. Skipping it silently would drop every
    observation in it and surface weeks later as coverage that never arrived,
    with nothing pointing at the cause."""
    _write(
        manual_dir,
        "pmi.yml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.fetch([PMI], ["EUR"], date(2026, 1, 1), ASOF)

    assert "pmi.yml" in str(caught.value)


# `yield_2y` for USD is a FRED ref at the `level` transform, so it is a pair an
# operator can legitimately correct by hand and the one case where the ref's
# `source` and `series_id` differ from what a manual row must carry.
OVERRIDE_ROW = """
- indicator: yield_2y
  currency: USD
  value: 3.61
  period: 2026-09-01
"""


def test_overriding_a_fetched_series_still_says_the_number_was_typed(
    source: ManualSource, manual_dir: Path
) -> None:
    """The registry ref for this pair names FRED. Copying that onto the
    observation would report a hand-typed correction as a vendor print, and the
    report's whole reason for carrying `source` is to keep those apart."""
    path = _write(manual_dir, "zz-overrides.yaml", _file(OVERRIDE_ROW))

    observation = source.load_file(path)[0]

    assert INDICATORS["yield_2y"].series["USD"].source == "fred"
    assert observation.source == SOURCE_MANUAL


def test_overriding_a_fetched_series_does_not_borrow_its_series_id(
    source: ManualSource, manual_dir: Path
) -> None:
    """``DGS2`` identifies a series on FRED. A number somebody typed is not
    that series, and labelling it so claims an audit trail that does not
    exist."""
    path = _write(manual_dir, "zz-overrides.yaml", _file(OVERRIDE_ROW))

    observation = source.load_file(path)[0]

    assert INDICATORS["yield_2y"].series["USD"].series_id == "DGS2"
    assert observation.series_id == "yield_2y"


def test_missing_reads_the_newest_entered_period_not_the_oldest(
    source: ManualSource, manual_dir: Path
) -> None:
    """An operator who has entered every month since March has a current value.
    Reading the oldest would report them as still owing it."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 48.2
          period: 2026-03-01
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    assert "EUR" not in source.missing(ASOF)[PMI]


def test_a_null_required_field_is_reported_as_missing_not_as_a_bad_type(
    source: ManualSource, manual_dir: Path
) -> None:
    """``period:`` with nothing after it is a field the operator meant to fill
    in. Telling them it is "NoneType rather than a date" describes the symptom;
    telling them the field is missing describes what to do."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period:
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "has no period" in str(caught.value)


# --- gaps the review pass found ---------------------------------------------


def test_the_staleness_allowance_moves_with_the_indicator(
    source: ManualSource, manual_dir: Path
) -> None:
    """Two indicators whose allowances differ by an order of magnitude, aged to
    the same day, so no single hardcoded number can satisfy both.

    The earlier version of this test used `pmi_composite` alone, whose
    allowance is 75, and read that 75 out of the registry before asserting
    against it. A literal 75 in the loader passed it.
    """
    assert INDICATORS[PMI].max_staleness_days == 75
    assert INDICATORS["employment_chg"].max_staleness_days == 270
    aged = ASOF - timedelta(days=100)
    _write(
        manual_dir,
        "entries.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: {aged.isoformat()}
        - indicator: employment_chg
          currency: EUR
          value: 180000
          period: {aged.isoformat()}
        """),
    )

    reported = source.missing(ASOF)

    # 100 days is past pmi_composite's 75 and inside employment_chg's 270.
    assert "EUR" in reported[PMI]
    assert "employment_chg" not in reported


def test_the_frequency_is_the_refs_and_not_the_indicators(
    source: ManualSource, manual_dir: Path
) -> None:
    """`IndicatorSpec.frequency` is the typical cadence across the universe and
    `SeriesRef.frequency` is what this currency actually publishes. They differ
    on five manual pairs, and the ref is the one that is right: Swiss
    industrial production is quarterly while the indicator is monthly.

    Every earlier unit and frequency test used `pmi_composite`, the one manual
    family where the two agree, so neither could tell which was read.
    """
    spec = INDICATORS["indpro_yoy"]
    assert spec.frequency is Frequency.MONTHLY
    assert spec.series["CHF"].frequency is Frequency.QUARTERLY
    path = _write(
        manual_dir,
        "indpro.yaml",
        _file("""
        - indicator: indpro_yoy
          currency: CHF
          value: 1.4
          period: 2026-04-01
        """),
    )

    assert source.load_file(path)[0].frequency is Frequency.QUARTERLY


def test_a_frequency_matching_the_indicator_but_not_the_ref_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    """The converse. An operator reading the indicator table rather than the
    per-currency one would write `monthly` here, and that is exactly the
    mismatch worth refusing."""
    path = _write(
        manual_dir,
        "indpro.yaml",
        _file("""
        - indicator: indpro_yoy
          currency: CHF
          value: 1.4
          period: 2026-04-01
          frequency: monthly
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


def test_staleness_is_measured_from_the_period_not_the_release(
    source: ManualSource, manual_dir: Path
) -> None:
    """A figure published yesterday for a quarter that ended four months ago is
    four months old to the model. Measuring from `released_at` would make every
    lagging series look current on the day it prints, which is the direction
    that flatters."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-03-01
          released_at: {ASOF.isoformat()}
        """),
    )

    assert "EUR" in source.missing(ASOF)[PMI]


def test_a_zoneless_release_timestamp_is_read_as_utc(
    source: ManualSource, manual_dir: Path
) -> None:
    """Stated in the docstring as a convention, so it is pinned here. A naive
    datetime compares unusably against an aware one and raises rather than
    quietly sorting wrong, but the convention is still a choice worth fixing in
    a test."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          released_at: 2026-09-01T08:00:00
        """),
    )

    assert source.load_file(path)[0].released_at == datetime(
        2026, 9, 1, 8, 0, tzinfo=UTC
    )


def test_a_bare_release_date_is_midnight_utc(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          released_at: 2026-09-01
        """),
    )

    assert source.load_file(path)[0].released_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_a_boolean_value_is_refused(source: ManualSource, manual_dir: Path) -> None:
    """``value: yes`` is a bool to YAML, and bool subclasses int, so an
    unguarded numeric check reads it as 1.0. For an index centred on 50 that is
    a real print nobody typed."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: yes
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "bool" in str(caught.value)


def test_a_boolean_revision_is_refused(source: ManualSource, manual_dir: Path) -> None:
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          revision: true
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


def test_a_meta_block_that_is_not_a_mapping_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    """``meta`` is free in its keys, not in its shape. Accepting a bare string
    and dropping it would discard the source URL, which is the only audit trail
    a hand-typed number has."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
          meta: "from investing.com"
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)


def test_an_empty_file_is_refused(source: ManualSource, manual_dir: Path) -> None:
    """Distinct from an empty `observations` list, which is a topic nobody has
    filled in yet. A zero-byte file is more likely a truncated write."""
    path = _write(manual_dir, "pmi.yaml", "")

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    # Named as empty rather than as a shape problem. Dropping the guard leaves
    # the next one to refuse it as "NoneType rather than a mapping", which
    # describes the symptom and not what the operator should do about it.
    assert "pmi.yaml" in str(caught.value)
    assert "empty" in str(caught.value)


def test_a_document_that_is_not_a_mapping_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    path = _write(manual_dir, "pmi.yaml", "- indicator: pmi_composite\n")

    with pytest.raises(SourceError):
        source.load_file(path)


def test_a_period_written_as_a_timestamp_is_read_as_its_date(
    source: ManualSource, manual_dir: Path
) -> None:
    """Otherwise a `datetime` lands in a field typed `date`, and two rows
    meaning the same month key differently, so an intended override silently
    does not apply."""
    _write(
        manual_dir,
        "a-pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01T00:00:00Z
        """),
    )
    _write(
        manual_dir,
        "b-overrides.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 50.4
          period: 2026-08-01
        """),
    )

    emitted = source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)

    assert [(o.period, o.value) for o in emitted] == [(date(2026, 8, 1), 50.4)]


def test_a_registry_that_contradicts_itself_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    """`commodity_price` for CAD is a `level` ref publishing dollars per barrel
    under an indicator declared as an index. With no transform between them
    there is no reading that makes both true, so there is no unit to put on a
    typed number and the registry is what needs fixing.

    The only such pair in the registry today, asserted here so this test tells
    anyone reading it why it exists rather than looking arbitrary.
    """
    spec = INDICATORS["commodity_price"]
    ref = spec.series["CAD"]
    assert ref.transform == "level"
    assert (ref.unit, spec.unit) == ("usd_per_barrel", "index")
    path = _write(
        manual_dir,
        "zz-overrides.yaml",
        _file("""
        - indicator: commodity_price
          currency: CAD
          value: 71.4
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "contradicts itself" in str(caught.value)


def test_a_non_string_indicator_is_refused_rather_than_stringified(
    source: ManualSource, manual_dir: Path
) -> None:
    """``indicator: 2026`` coerced with `str()` would become the key
    ``"2026"``, refused a line later for a reason that names the wrong
    problem."""
    path = _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: 2026
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.load_file(path)

    assert "string" in str(caught.value)


def test_fetch_returns_observations_in_a_stable_order(
    source: ManualSource, manual_dir: Path
) -> None:
    """A report rendering an unordered sequence is not reproducible run to
    run, and `_entries` is a dict keyed by a tuple."""
    _three_rows(manual_dir)

    emitted = source.fetch([PMI, "indpro_yoy"], list(G10), date(2026, 1, 1), ASOF)
    keys = [(o.indicator, o.currency, o.period) for o in emitted]

    assert keys == sorted(keys)


def test_the_refused_differenced_refs_are_still_on_the_to_do_list(
    source: ManualSource, manual_dir: Path
) -> None:
    """The two behaviours are in tension on purpose and neither is an
    accident: `missing` reports two entries that `load_file` will not accept.
    Pinned together so a later change cannot quietly drop one side. There were
    four until the RBNZ took NZD's two off the manual route."""
    reported = source.missing(ASOF)

    assert set(reported["yield_2y_chg_1m"]) == {"CHF"}
    assert set(reported["yield_2y_chg_3m"]) == {"CHF"}


# --- template ---------------------------------------------------------------


def test_a_template_loads_unedited(source: ManualSource, manual_dir: Path) -> None:
    """Criterion 9's round trip. The stanza is commented out under an empty
    `observations` list, which is the one shape that both loads and leaves
    `value` and `period` blank."""
    path = _write(manual_dir, "pmi.yaml", source.template(PMI, "EUR"))

    assert list(source.load_file(path)) == []


def test_a_template_fabricates_no_value(source: ManualSource) -> None:
    """The reason it is commented out rather than filled in. A template is the
    worst place in the repository to put a number nobody typed, because its
    whole purpose is to be copied."""
    rendered = source.template(PMI, "EUR")

    assert "#   value:\n" in rendered
    assert "#   period:\n" in rendered
    assert "observations: []" in rendered


def test_a_template_prefills_the_registrys_unit_and_frequency(
    source: ManualSource,
) -> None:
    """So an operator who uncomments the block cannot contradict them by
    accident. Asserted on a pair where the ref and the indicator disagree."""
    rendered = source.template("indpro_yoy", "CHF")

    assert "#   unit: percent" in rendered
    assert "#   frequency: quarterly" in rendered


def test_an_uncommented_template_is_accepted_once_it_is_filled_in(
    source: ManualSource, manual_dir: Path
) -> None:
    """The operator's actual workflow, so the commented form is not merely
    loadable but usable: uncomment, fill two fields, load."""
    rendered = source.template(PMI, "EUR")
    body = rendered.replace("observations: []", "observations:")
    body = "\n".join(
        line[2:] if line.startswith("# ") else line
        for line in body.splitlines()
        if not line.startswith("# Uncomment") and not line.startswith("# and")
    )
    body = body.replace("  value:", "  value: 49.8").replace(
        "  period:", "  period: 2026-08-01"
    )
    path = _write(manual_dir, "pmi.yaml", body)

    emitted = source.load_file(path)

    assert [o.value for o in emitted] == [49.8]


def test_a_template_for_an_unknown_indicator_raises_key_error(
    source: ManualSource,
) -> None:
    """The stub docstring says `KeyError`, not `SourceError`: nothing has been
    read, so this is a caller mistake rather than a bad file."""
    with pytest.raises(KeyError):
        source.template("pmi_manufacturing", "EUR")


def test_a_template_for_a_currency_outside_the_universe_raises(
    source: ManualSource,
) -> None:
    with pytest.raises(ValueError):
        source.template(PMI, "ZAR")


def test_a_template_is_refused_for_a_pair_load_file_would_reject(
    source: ManualSource,
) -> None:
    """Emitting a stanza this source refuses would waste the operator's typing
    and teach them the format is wrong."""
    with pytest.raises(ValueError):
        source.template("commodity_price", "CAD")


def test_a_template_for_a_differenced_indicator_gives_its_canonical_unit(
    source: ManualSource,
) -> None:
    """These are on `missing`'s to-do list, so an operator will ask for one."""
    rendered = source.template("yield_2y_chg_1m", "CHF")

    assert "#   unit: basis_points" in rendered


def test_a_future_dated_period_is_still_outstanding(
    source: ManualSource, manual_dir: Path
) -> None:
    """A mistyped year is the case this whole module exists for.

    Without a lower bound the age reads negative, so the pair drops off the
    to-do list while `fetch` still filters the row out of its window: the
    pillar loses the currency, coverage falls, and the one tool built to say
    why reports nothing to do.
    """
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2027-08-01
        """),
    )

    assert list(source.fetch([PMI], ["EUR"], date(2026, 1, 1), ASOF)) == []
    assert "EUR" in source.missing(ASOF)[PMI]


def test_a_period_on_the_run_date_is_not_outstanding(
    source: ManualSource, manual_dir: Path
) -> None:
    """The boundary between the two branches above, so the lower bound cannot
    be off by a day and report a value typed this morning as missing."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file(f"""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: {ASOF.isoformat()}
        """),
    )

    assert "EUR" not in source.missing(ASOF)[PMI]


def test_the_uppercase_extension_is_refused_too(
    source: ManualSource, manual_dir: Path
) -> None:
    """`pmi.YAML` is as unread as `pmi.yml` and as silent about it, and it
    has to be refused on every platform: ``Path.glob`` matches it on Windows
    and not on Linux, so without the exact suffix check the file would be
    loaded on the operator's machine and skipped in CI."""
    _write(
        manual_dir,
        "PMI.YAML",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )

    with pytest.raises(SourceError) as caught:
        source.fetch([PMI], ["EUR"], date(2026, 1, 1), ASOF)

    assert "PMI.YAML" in str(caught.value)


def test_a_backup_copy_is_left_alone(source: ManualSource, manual_dir: Path) -> None:
    """The other side of that rule. A file somebody kept a copy of is not one
    they expected to be loaded, and refusing it would make the directory
    unusable as a working area."""
    _write(
        manual_dir,
        "pmi.yaml",
        _file("""
        - indicator: pmi_composite
          currency: EUR
          value: 49.8
          period: 2026-08-01
        """),
    )
    _write(manual_dir, "pmi.yaml.bak", "anything at all, not even YAML: [\n")

    assert [o.value for o in source.fetch([PMI], ["EUR"], date(2026, 8, 1), ASOF)] == [
        49.8
    ]


def test_the_unit_is_the_indicators_and_not_the_refs(
    source: ManualSource, manual_dir: Path
) -> None:
    """Stated as its own test because it is the half of the labelling rule that
    the `pmi_composite` cases cannot see: there the ref and the indicator agree
    exactly, so neither can tell which was read."""
    spec = INDICATORS["yield_2y_chg_3m"]
    assert spec.series["NZD"].unit != spec.unit
    path = _write(
        manual_dir,
        "yields.yaml",
        _file("""
        - indicator: yield_2y_chg_3m
          currency: NZD
          value: 24.0
          period: 2026-08-03
        """),
    )

    assert source.load_file(path)[0].unit == spec.unit


def test_a_unit_matching_the_ref_but_not_the_indicator_is_refused(
    source: ManualSource, manual_dir: Path
) -> None:
    """The converse. An operator reading the per-currency source table rather
    than the indicator table would write `percent` here, and a value typed in
    basis points under that label is a hundredfold error."""
    path = _write(
        manual_dir,
        "yields.yaml",
        _file("""
        - indicator: yield_2y_chg_3m
          currency: NZD
          value: 24.0
          period: 2026-08-03
          unit: percent
        """),
    )

    with pytest.raises(SourceError):
        source.load_file(path)
