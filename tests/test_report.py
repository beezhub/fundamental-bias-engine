"""The dated report on disk: writing it, reading it back, and diffing two runs.

A report that exists only in a terminal is not a record. Phase 6 joins the
journal to these files, so everything here is about the two ways that join can
silently produce a wrong answer:

- A sidecar that round-trips *almost*. An enum returning as a bare string, a
  ``None`` returning as ``0.0``, a date returning as a string: each leaves a
  `fbe.types.BiasReport` that reads fine and compares wrong. Comparing two
  serialised blobs would pass on every one of them, so the round-trip test
  walks the dataclass field by field.
- A pair of files that disagree. The Markdown is for a reader and the sidecar
  is what ``--compare`` reads, so a run that writes one and not the other
  leaves a what-changed section that is empty forever with nothing raising to
  say why.
- A diff that treats an absent score as zero. A currency that dropped out of
  the universe and a currency that scored zero mean opposite things, and the
  first printed as a delta is a fundamental move that never happened.

Nothing here reaches the network. Every test writes under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

import fbe
from fbe.cli import _pillar_order as cli_pillar_order
from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.report import (
    SIDECAR_FORMAT,
    TEMPLATE_NAME,
    CurrencyChange,
    ReportDiff,
    _grid,
    _pillar_order,
    build_context,
    diff_reports,
    latest_report,
    load_report,
    render_report,
    write_report,
)
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
    PositionSize,
    TradeIdea,
)

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent

ASOF = date(2026, 6, 30)
YESTERDAY = date(2026, 6, 29)
GENERATED_AT = datetime(2026, 6, 30, 17, 0, tzinfo=UTC)
RELEASED_AT = datetime(2026, 6, 29, 12, 30, tzinfo=UTC)

CONTEXT_KEYS = {
    "report",
    "diff",
    "config",
    "grid",
    "pillar_order",
    "currencies",
    "pairs",
    "unknown_prefix",
}
"""The keys `fbe.report.build_context` documents.

A key added without a docstring line is a key one template reads and the other
does not know about, which is how two views of one run start telling different
stories. The set is what `build_context` supplies, not what either template
happens to read: ``unknown_prefix`` is read by the Markdown template and not,
yet, by the dashboard, and `build_context`'s own docstring is where that is
recorded."""


# --- fixtures ----------------------------------------------------------------


def observation(
    indicator: str = "yield_2y",
    currency: str = "USD",
    value: float = 4.25,
    released_at: datetime | None = RELEASED_AT,
    meta: dict[str, Any] | None = None,
) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=date(2026, 6, 1),
        source="fixture",
        series_id="FIXTURE",
        unit="percent",
        frequency=Frequency.DAILY,
        released_at=released_at,
        revision=0,
        meta=meta if meta is not None else {},
    )


def pillar_score(
    pillar: PillarName = PillarName.MONETARY,
    currency: str = "USD",
    score: float = 1.5,
    raw: float | None = 0.8,
    z: float | None = 1.1,
    weight: float = 0.30,
) -> PillarScore:
    return PillarScore(
        pillar=pillar,
        currency=currency,
        raw=raw,
        z=z,
        score=score,
        weight=weight,
        asof=ASOF,
        staleness_days=2,
        inputs=(observation(currency=currency),),
        notes=f"{pillar.value} working for {currency}",
        diagnostics={"emit_sd": 0.91, "contamination": 0.87},
        blend_divisor_path="rolling",
        freshness_factor=0.95,
    )


def currency_score(
    currency: str = "USD",
    composite: float = 1.20,
    rank: int | None = 1,
    pillars: dict[PillarName, PillarScore] | None = None,
) -> CurrencyScore:
    return CurrencyScore(
        currency=currency,
        composite=composite,
        pillars=(
            pillars
            if pillars is not None
            else {
                # Deliberately not the composite: a renderer that recomputed
                # the headline from the pillars would then print the right
                # number for the wrong reason, and the perturbation test below
                # could not tell.
                name: pillar_score(name, currency, score=1.5 - 0.1 * index)
                for index, name in enumerate(PillarName)
            }
        ),
        asof=ASOF,
        rank=rank,
        dispersion=0.44,
        coverage=0.86,
    )


def pair_bias(
    pair: str = "EURUSD",
    spread: float = -1.42,
    direction: Direction = Direction.SHORT,
    conviction: Conviction = Conviction.MEDIUM,
    tradeable: bool = True,
    blockers: tuple[str, ...] = ("cost:unchecked", "event:unchecked"),
    base_score: float = -0.22,
    quote_score: float = 1.20,
) -> PairBias:
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=spread,
        direction=direction,
        conviction=conviction,
        asof=ASOF,
        base_score=base_score,
        quote_score=quote_score,
        agreement=0.70,
        tradeable=tradeable,
        blockers=blockers,
    )


def position_size(pair: str = "EURUSD") -> PositionSize:
    return PositionSize(
        pair=pair,
        account_currency="ZAR",
        account_balance=2000.0,
        risk_fraction=0.015,
        risk_amount=30.0,
        realised_risk_amount=28.4,
        entry=1.0850,
        stop=1.0900,
        stop_distance_pips=50.0,
        units=1200.0,
        lots=0.01,
        notional=22500.0,
        warnings=("lot step rounded the size down",),
    )


def bias_report(
    asof: date = ASOF,
    currencies: tuple[CurrencyScore, ...] | None = None,
    pairs: tuple[PairBias, ...] | None = None,
    shortlist: tuple[TradeIdea, ...] | None = None,
    warnings: tuple[str, ...] = ("POSITIONING had no data for any currency",),
    config_digest: str = "abc123",
) -> BiasReport:
    if currencies is None:
        currencies = (
            currency_score("USD", 1.20, rank=1),
            currency_score("EUR", -0.22, rank=2),
        )
    if pairs is None:
        pairs = (pair_bias(),)
    if shortlist is None:
        shortlist = (
            TradeIdea(
                bias=pairs[0],
                size=position_size(pairs[0].pair),
                blackout_until=None,
                rationale="The rates gap is the widest in the universe.",
            ),
        )
    return BiasReport(
        asof=asof,
        generated_at=GENERATED_AT,
        currencies=currencies,
        pairs=pairs,
        events=(),
        shortlist=shortlist,
        warnings=warnings,
        config_digest=config_digest,
    )


def config_with(**weights: float) -> Config:
    """A config whose pillar weights are exactly the ones given."""
    return Config(
        risk=RiskConfig(),
        scoring=ScoringConfig(
            weights={PillarName(name): value for name, value in weights.items()}
        ),
        data=DataConfig(),
    )


def sidecar_of(markdown: Path) -> Path:
    return markdown.with_suffix(".json")


# --- _pillar_order -----------------------------------------------------------


def test_pillars_come_back_heaviest_weight_first() -> None:
    """The column order is the reading order, so it follows the weights.

    Weights chosen so the answer is not `PillarName`'s declaration order and
    not its reverse either. Either of those would pass against a function that
    ignored the config entirely.
    """
    config = config_with(
        monetary=0.05,
        inflation=0.40,
        growth=0.05,
        employment=0.20,
        external=0.05,
        positioning=0.05,
        risk=0.20,
    )

    assert tuple(_pillar_order(config)) == (
        PillarName.INFLATION,
        PillarName.EMPLOYMENT,
        PillarName.RISK,
        PillarName.MONETARY,
        PillarName.GROWTH,
        PillarName.EXTERNAL,
        PillarName.POSITIONING,
    )


def test_no_config_gives_the_declaration_order() -> None:
    """``None`` means no weights are known, which is not the same as flat.

    The declaration order runs from the fastest and heaviest driver to the
    slowest, so it is the honest fallback. Sorting a map that does not exist
    would be a claim about weights nobody supplied.
    """
    assert tuple(_pillar_order(None)) == tuple(PillarName)


def test_every_pillar_appears_exactly_once() -> None:
    flat = config_with(**{name.value: 0.1 for name in PillarName})

    assert sorted(_pillar_order(flat)) == sorted(PillarName)


def test_the_report_and_the_cli_agree_on_the_order() -> None:
    """Two copies of one rule, and the pair matters more than either copy.

    `fbe.cli._pillar_order` orders the ``--pillars`` columns and this one
    orders the report's. A reader comparing a terminal table with the morning
    Markdown would see the same seven numbers under different headings, and
    nothing on either page would say which was which.
    """
    config = config_with(
        monetary=0.05,
        inflation=0.40,
        growth=0.05,
        employment=0.20,
        external=0.05,
        positioning=0.05,
        risk=0.20,
    )

    assert tuple(_pillar_order(config)) == cli_pillar_order(config.scoring)


def test_a_weight_missing_from_the_config_raises() -> None:
    """A pillar with no weight is a config mistake, not a pillar ranked last.

    `fbe.scoring.score_currencies` raises on the same lookup, so a quiet
    default here would only move where the operator meets it, and move it to
    the one place that prints a table rather than stopping.
    """
    with pytest.raises(KeyError):
        _pillar_order(config_with(monetary=0.3))


# --- the sidecar round trip --------------------------------------------------


def test_a_report_round_trips_field_by_field(tmp_path: Path) -> None:
    """Field by field, not blob against blob.

    Comparing two serialised dictionaries passes whenever the encoder and the
    decoder share a mistake, which is exactly the mistake a single author
    makes. Comparing the reconstructed dataclass against the original catches
    it, and naming the field in the assertion says which one drifted.
    """
    original = bias_report()

    loaded = load_report(write_report(original, tmp_path))

    for item in fields(BiasReport):
        assert getattr(loaded, item.name) == getattr(original, item.name), item.name
    assert loaded == original


def test_enums_return_as_enums_and_not_as_strings(tmp_path: Path) -> None:
    """A `StrEnum` compares equal to its own value, which hides this entirely.

    ``Direction.SHORT == "short"`` is true, so an equality assertion on the
    field passes against a decoder that never rebuilt the enum. The next
    consumer to call ``.value`` on it gets an ``AttributeError`` days later,
    on a Phase 6 join nobody is watching.
    """
    loaded = load_report(write_report(bias_report(), tmp_path))

    row = loaded.pairs[0]
    assert isinstance(row.direction, Direction)
    assert isinstance(row.conviction, Conviction)
    assert all(isinstance(name, PillarName) for name in loaded.currencies[0].pillars)
    assert isinstance(
        loaded.currencies[0].pillars[PillarName.MONETARY].inputs[0].frequency,
        Frequency,
    )


def test_dates_and_datetimes_return_as_dates_and_datetimes(tmp_path: Path) -> None:
    """``asof`` is a date and ``generated_at`` is an instant, and they differ.

    A date decoded as a datetime sorts and compares against a real date in
    ways that look right until a report is compared with one written on the
    same day by a different run.
    """
    loaded = load_report(write_report(bias_report(), tmp_path))

    assert type(loaded.asof) is date
    assert isinstance(loaded.generated_at, datetime)
    assert loaded.generated_at.utcoffset() is not None
    assert loaded.generated_at == GENERATED_AT


def test_an_absent_number_does_not_return_as_zero(tmp_path: Path) -> None:
    """The distinction the whole package is built on, checked on the wire.

    ``raw`` and ``z`` are ``None`` when a pillar formed no opinion and a real
    number when it formed one of zero. A codec that writes ``None`` and reads
    back ``0.0`` turns "no evidence" into "evidence of neutral" on every
    report ever written, and nothing downstream can tell.
    """
    absent = replace(pillar_score(), raw=None, z=None, freshness_factor=None, score=0.0)
    original = bias_report(
        currencies=(currency_score("USD", 0.0, pillars={PillarName.MONETARY: absent}),)
    )

    loaded = load_report(write_report(original, tmp_path))

    restored = loaded.currencies[0].pillars[PillarName.MONETARY]
    assert restored.raw is None
    assert restored.z is None
    assert restored.freshness_factor is None
    assert restored.score == 0.0


def test_a_zero_and_an_absence_do_not_collapse_into_each_other(tmp_path: Path) -> None:
    """The other half of the same rule, from the side that reads zero."""
    scored = replace(pillar_score(), raw=0.0, z=0.0, freshness_factor=0.0)
    original = bias_report(
        currencies=(currency_score("USD", 0.0, pillars={PillarName.MONETARY: scored}),)
    )

    restored = (
        load_report(write_report(original, tmp_path))
        .currencies[0]
        .pillars[PillarName.MONETARY]
    )

    assert restored.raw == 0.0
    assert restored.z == 0.0
    assert restored.freshness_factor == 0.0


def test_the_config_digest_survives(tmp_path: Path) -> None:
    """Without it a report cannot be tied to the weights that produced it."""
    loaded = load_report(write_report(bias_report(config_digest="d4f0c1"), tmp_path))

    assert loaded.config_digest == "d4f0c1"


def test_every_pair_survives_in_order(tmp_path: Path) -> None:
    """Order is meaning here: `fbe.universe.ALL_PAIRS` fixes the convention."""
    pairs = (
        pair_bias("EURUSD", spread=-1.42, direction=Direction.SHORT),
        pair_bias("GBPUSD", spread=0.35, direction=Direction.LONG),
        pair_bias("AUDJPY", spread=2.10, direction=Direction.LONG),
    )
    original = bias_report(pairs=pairs, shortlist=())

    loaded = load_report(write_report(original, tmp_path))

    assert tuple(row.pair for row in loaded.pairs) == ("EURUSD", "GBPUSD", "AUDJPY")
    assert tuple(loaded.pairs) == pairs


def test_the_seven_pillars_survive_for_every_currency(tmp_path: Path) -> None:
    original = bias_report()

    loaded = load_report(write_report(original, tmp_path))

    for score in loaded.currencies:
        assert set(score.pillars) == set(PillarName)


def test_an_observation_keeps_its_untyped_meta(tmp_path: Path) -> None:
    """``Observation.meta`` is ``Mapping[str, Any]`` and carries source detail.

    Dropping it loses the only record of how a number arrived, and the loss is
    invisible: the score it produced is still on the page.
    """
    input_with_meta = observation(meta={"vintage": "2026-06-29", "revised": True})
    scored = replace(pillar_score(), inputs=(input_with_meta,))
    original = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    restored = (
        load_report(write_report(original, tmp_path))
        .currencies[0]
        .pillars[PillarName.MONETARY]
    )

    assert restored.inputs[0].meta == {"vintage": "2026-06-29", "revised": True}


def test_an_observation_with_no_release_stamp_round_trips_as_absent(
    tmp_path: Path,
) -> None:
    """``released_at`` is ``None`` for sources that publish no timestamp.

    Decoded as anything else it would claim a release date the source never
    gave, which is the look-ahead bug wearing a serialisation costume.
    """
    unstamped = observation(released_at=None)
    scored = replace(pillar_score(), inputs=(unstamped,))
    original = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    restored = (
        load_report(write_report(original, tmp_path))
        .currencies[0]
        .pillars[PillarName.MONETARY]
    )

    assert restored.inputs[0].released_at is None


def test_the_shortlist_and_its_sizes_survive(tmp_path: Path) -> None:
    """`TradeIdea` carries the realised risk Phase 6 measures an R against."""
    loaded = load_report(write_report(bias_report(), tmp_path))

    idea = loaded.shortlist[0]
    assert isinstance(idea, TradeIdea)
    assert idea.size is not None
    assert idea.size.realised_risk_amount == 28.4
    assert idea.rationale == "The rates gap is the widest in the universe."


def test_load_report_accepts_the_markdown_path(tmp_path: Path) -> None:
    """The caller holds the Markdown path, because that is what `write_report`
    returns, and asking it to derive the sidecar name would put
    `SIDECAR_FORMAT` in two places."""
    markdown = write_report(bias_report(), tmp_path)

    assert load_report(markdown) == load_report(sidecar_of(markdown))


@pytest.mark.parametrize(
    "content",
    ["not json at all", "[]", '"a string"', "17"],
)
def test_a_sidecar_that_is_not_a_report_object_raises(
    tmp_path: Path, content: str
) -> None:
    """Every one of these would otherwise fail later, somewhere else."""
    path = tmp_path / SIDECAR_FORMAT.format(asof=ASOF)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=str(path.name)):
        load_report(path)


def test_a_missing_field_in_the_sidecar_raises(tmp_path: Path) -> None:
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["config_digest"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="config_digest"):
        load_report(path)


def test_an_unknown_field_in_the_sidecar_raises(tmp_path: Path) -> None:
    """A field this version does not know is a report from another version.

    Ignored, it would load a report missing whatever that field carried, and
    the diff against it would describe a change that was a version difference.
    """
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sharpe_ratio"] = 1.4
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sharpe_ratio"):
        load_report(path)


def test_an_unknown_enum_value_raises_and_names_it(tmp_path: Path) -> None:
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pairs"][0]["direction"] = "buy"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="buy"):
        load_report(path)


def test_a_timestamp_that_is_not_a_date_raises(tmp_path: Path) -> None:
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["asof"] = "the thirtieth"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="the thirtieth"):
        load_report(path)


def test_a_date_field_holding_an_instant_raises(tmp_path: Path) -> None:
    """``asof`` is a day. A day that secretly carries a time sorts differently
    from one that does not, and two runs on one date stop comparing equal."""
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["asof"] = "2026-06-30T17:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Matched on the field. `load_report` wraps everything in ValueError, so a
    # bare raises passes for any unrelated decode failure in the same file.
    with pytest.raises(ValueError, match="asof"):
        load_report(path)


def test_a_number_field_holding_text_raises(tmp_path: Path) -> None:
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pairs"][0]["spread"] = "wide"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="spread"):
        load_report(path)


def test_a_pillar_map_keyed_by_something_that_is_not_a_pillar_raises(
    tmp_path: Path,
) -> None:
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    pillars = payload["currencies"][0]["pillars"]
    pillars["sentiment"] = pillars.pop("monetary")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sentiment"):
        load_report(path)


def test_the_sidecar_is_a_json_object_keyed_by_field_name(tmp_path: Path) -> None:
    """Pinned because Phase 6 and any external reader parse this by hand."""
    markdown = write_report(bias_report(), tmp_path)

    payload = json.loads(sidecar_of(markdown).read_text(encoding="utf-8"))

    assert set(payload) == {item.name for item in fields(BiasReport)}
    # Nested too, and `PositionSize` specifically. It is the one type in the
    # report carrying a derived accessor, and the sidecar is the committed
    # record that cannot be regenerated: a derived value written into it
    # could be read back out of step with the two numbers it came from.
    assert set(payload["shortlist"][0]["size"]) == {
        item.name for item in fields(PositionSize)
    }
    assert payload["asof"] == "2026-06-30"
    assert payload["pairs"][0]["direction"] == "short"
    assert "monetary" in payload["currencies"][0]["pillars"]


# --- write_report ------------------------------------------------------------


def test_both_files_are_written_with_a_shared_stem(tmp_path: Path) -> None:
    markdown = write_report(bias_report(), tmp_path)

    assert markdown.name == "bias-2026-06-30.md"
    assert sidecar_of(markdown).exists()
    assert markdown.exists()


def test_write_report_returns_the_markdown_path(tmp_path: Path) -> None:
    assert write_report(bias_report(), tmp_path).suffix == ".md"


def test_the_out_directory_is_created_when_absent(tmp_path: Path) -> None:
    """The normal case on a routine host, which clones and has no reports dir."""
    target = tmp_path / "nested" / "reports"

    markdown = write_report(bias_report(), target)

    assert markdown.parent == target
    assert sidecar_of(markdown).exists()


def test_the_markdown_failing_to_write_leaves_no_sidecar(tmp_path: Path) -> None:
    """Both files or neither, and this is the half that fails silently.

    A sidecar without its Markdown is invisible to a reader and perfectly
    readable to ``--compare``, so the run looks complete and the audit trail
    has a hole in it that only shows up when someone goes looking months
    later.
    """
    (tmp_path / "bias-2026-06-30.md").mkdir()

    with pytest.raises(OSError):
        write_report(bias_report(), tmp_path)

    assert not (tmp_path / "bias-2026-06-30.json").exists()


def test_a_failure_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    (tmp_path / "bias-2026-06-30.md").mkdir()

    with pytest.raises(OSError):
        write_report(bias_report(), tmp_path)

    assert sorted(item.name for item in tmp_path.iterdir()) == ["bias-2026-06-30.md"]


def test_overwrite_false_refuses_when_the_markdown_exists(tmp_path: Path) -> None:
    write_report(bias_report(), tmp_path)

    with pytest.raises(FileExistsError, match="bias-2026-06-30"):
        write_report(bias_report(), tmp_path, overwrite=False)


def test_overwrite_false_refuses_when_only_the_sidecar_exists(tmp_path: Path) -> None:
    """Governed together, so a run can never pair this run's Markdown with the
    last run's sidecar."""
    (tmp_path / "bias-2026-06-30.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="bias-2026-06-30"):
        write_report(bias_report(), tmp_path, overwrite=False)


def test_overwrite_false_changes_neither_file(tmp_path: Path) -> None:
    markdown = write_report(bias_report(config_digest="first"), tmp_path)
    before = (
        markdown.read_bytes(),
        sidecar_of(markdown).read_bytes(),
    )

    with pytest.raises(FileExistsError):
        write_report(bias_report(config_digest="second"), tmp_path, overwrite=False)

    assert (markdown.read_bytes(), sidecar_of(markdown).read_bytes()) == before


def test_overwrite_true_replaces_both(tmp_path: Path) -> None:
    """The normal case: a later run saw more data than the earlier one."""
    write_report(bias_report(config_digest="first"), tmp_path)

    markdown = write_report(bias_report(config_digest="second"), tmp_path)

    assert load_report(markdown).config_digest == "second"
    assert "second" in markdown.read_text(encoding="utf-8")


def test_the_diff_reaches_the_markdown_and_not_the_sidecar(tmp_path: Path) -> None:
    """The sidecar is the run. A diff is a statement about two runs, and
    storing it would make the second load of one file depend on which other
    file it was compared against the first time."""
    diff = diff_reports(bias_report(asof=YESTERDAY), bias_report())

    markdown = write_report(bias_report(), tmp_path, diff=diff)

    assert "Against 2026-06-29" in markdown.read_text(encoding="utf-8")
    assert set(json.loads(sidecar_of(markdown).read_text(encoding="utf-8"))) == {
        item.name for item in fields(BiasReport)
    }


# --- latest_report -----------------------------------------------------------


def write_dated(directory: Path, *days: date) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for day in days:
        write_report(bias_report(asof=day), directory)


def test_the_newest_report_is_returned(tmp_path: Path) -> None:
    write_dated(tmp_path, date(2026, 6, 26), date(2026, 6, 30), date(2026, 6, 29))

    found = latest_report(tmp_path)

    assert found is not None
    assert found.name == "bias-2026-06-30.json"


def test_an_empty_directory_holds_no_report(tmp_path: Path) -> None:
    assert latest_report(tmp_path) is None


def test_a_directory_that_does_not_exist_holds_no_report(tmp_path: Path) -> None:
    """A fresh clone with ``--out`` pointed somewhere new. No reports yet is
    the honest answer, and it is the same answer as an empty directory."""
    assert latest_report(tmp_path / "never-created") is None


def test_a_markdown_file_on_its_own_is_not_a_report(tmp_path: Path) -> None:
    """``--compare`` reads the sidecar, so a Markdown with no sidecar is a
    report nothing can read back."""
    (tmp_path / "bias-2026-06-30.md").write_text("# not a sidecar", encoding="utf-8")

    assert latest_report(tmp_path) is None


def test_before_excludes_a_report_on_its_own_date(tmp_path: Path) -> None:
    """A backfilled run compares against the run that actually preceded it,
    which is never itself."""
    write_dated(tmp_path, date(2026, 6, 26), date(2026, 6, 29), date(2026, 6, 30))

    found = latest_report(tmp_path, before=date(2026, 6, 29))

    assert found is not None
    assert found.name == "bias-2026-06-26.json"


def test_before_earlier_than_every_report_finds_nothing(tmp_path: Path) -> None:
    write_dated(tmp_path, date(2026, 6, 30))

    assert latest_report(tmp_path, before=date(2026, 1, 1)) is None


def test_a_sidecar_name_carrying_no_date_raises_and_names_the_file(
    tmp_path: Path,
) -> None:
    """``bias-*.json`` matches more than the dated files.

    ``bias-backup.json`` sorts after every dated name, so a newest-by-name
    rule hands it back and ``--compare`` reads a file that is not a report.
    Skipping it quietly is the other half of the same problem: the newest
    report is then whatever the glob happened to leave behind.
    """
    write_dated(tmp_path, date(2026, 6, 30))
    (tmp_path / "bias-backup.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="bias-backup.json"):
        latest_report(tmp_path)


# --- diff_reports ------------------------------------------------------------


def two_runs(
    previous_currencies: tuple[CurrencyScore, ...],
    current_currencies: tuple[CurrencyScore, ...],
    previous_pairs: tuple[PairBias, ...] = (),
    current_pairs: tuple[PairBias, ...] = (),
    previous_digest: str = "abc123",
    current_digest: str = "abc123",
    previous_warnings: tuple[str, ...] = (),
    current_warnings: tuple[str, ...] = (),
    previous_shortlist: tuple[TradeIdea, ...] = (),
    current_shortlist: tuple[TradeIdea, ...] = (),
) -> ReportDiff:
    return diff_reports(
        bias_report(
            asof=YESTERDAY,
            currencies=previous_currencies,
            pairs=previous_pairs,
            shortlist=previous_shortlist,
            warnings=previous_warnings,
            config_digest=previous_digest,
        ),
        bias_report(
            asof=ASOF,
            currencies=current_currencies,
            pairs=current_pairs,
            shortlist=current_shortlist,
            warnings=current_warnings,
            config_digest=current_digest,
        ),
    )


def test_the_two_asof_dates_are_carried() -> None:
    diff = two_runs((currency_score("USD", 1.0),), (currency_score("USD", 1.0),))

    assert diff.previous_asof == YESTERDAY
    assert diff.current_asof == ASOF


def test_a_currency_that_moved_carries_its_delta() -> None:
    diff = two_runs(
        (currency_score("USD", 1.00, rank=2),),
        (currency_score("USD", 1.75, rank=1),),
    )

    change = diff.currencies[0]
    assert change == CurrencyChange(
        currency="USD",
        previous_composite=1.00,
        current_composite=1.75,
        previous_rank=2,
        current_rank=1,
        delta=0.75,
    )


def test_a_currency_absent_from_the_baseline_has_no_delta() -> None:
    """A currency that scored nothing yesterday and +1.2 today did not move
    +1.2. It appeared. Reported as a delta it is a fundamental move nobody
    can find in the data."""
    diff = two_runs((), (currency_score("USD", 1.20),))

    change = diff.currencies[0]
    assert change.previous_composite is None
    assert change.delta is None
    assert change.current_composite == 1.20


def test_a_currency_absent_from_the_current_run_has_no_delta() -> None:
    diff = two_runs((currency_score("USD", 1.20),), ())

    change = diff.currencies[0]
    assert change.current_composite is None
    assert change.delta is None
    assert change.previous_composite == 1.20


def test_currencies_are_ordered_by_the_size_of_the_move() -> None:
    """The biggest move is the one worth checking against the input."""
    diff = two_runs(
        (
            currency_score("USD", 1.00),
            currency_score("EUR", 0.00),
            currency_score("JPY", -1.00),
        ),
        (
            currency_score("USD", 1.10),
            currency_score("EUR", -0.90),
            currency_score("JPY", -1.40),
        ),
    )

    assert [change.currency for change in diff.currencies] == ["EUR", "JPY", "USD"]


def test_a_currency_with_no_delta_sorts_after_every_measured_move() -> None:
    """An appearance is not a move of unknown size, so it does not compete
    with the measured ones for the top of the list."""
    diff = two_runs(
        (currency_score("USD", 1.00),),
        (currency_score("USD", 1.05), currency_score("CHF", 2.00)),
    )

    assert [change.currency for change in diff.currencies] == ["USD", "CHF"]


def test_a_pair_that_changed_direction_is_reported() -> None:
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("AUDJPY", 0.05, Direction.LONG),),
        current_pairs=(pair_bias("AUDJPY", -0.04, Direction.SHORT),),
    )

    change = diff.pairs[0]
    assert change.pair == "AUDJPY"
    assert change.previous_direction is Direction.LONG
    assert change.current_direction is Direction.SHORT
    assert change.flipped is True


def test_a_flip_is_recorded_even_when_both_spreads_are_tiny() -> None:
    """A flip is a change in the story, not only in the number. Gating it on
    the size of the move would silence exactly the case where the model has
    changed its mind on thin evidence, which is the one worth seeing."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", 0.001, Direction.LONG),),
        current_pairs=(pair_bias("EURUSD", -0.001, Direction.SHORT),),
    )

    assert diff.pairs[0].flipped is True


def test_neutral_to_long_is_a_change_but_not_a_flip() -> None:
    """Neutral has no side, so there is nothing for it to have flipped from.
    Counting it would put a run that simply formed an opinion in the same
    sentence as one that reversed."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", 0.0, Direction.NEUTRAL),),
        current_pairs=(pair_bias("EURUSD", 0.9, Direction.LONG),),
    )

    change = diff.pairs[0]
    assert change.flipped is False
    assert change.current_direction is Direction.LONG


def test_a_conviction_change_alone_is_reported() -> None:
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", -1.4, Direction.SHORT, Conviction.LOW),),
        current_pairs=(pair_bias("EURUSD", -1.4, Direction.SHORT, Conviction.HIGH),),
    )

    change = diff.pairs[0]
    assert change.previous_conviction is Conviction.LOW
    assert change.current_conviction is Conviction.HIGH
    assert change.flipped is False


def test_a_pair_whose_story_did_not_change_is_not_listed() -> None:
    """Twenty-eight unchanged rows under "what changed" is a section nobody
    reads, and the flip in the middle of it is the thing it exists to show."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", -1.40),),
        current_pairs=(pair_bias("EURUSD", -1.31),),
    )

    assert diff.pairs == ()


def test_a_reported_pair_carries_both_spreads() -> None:
    """So the reader can see how far the number moved to produce the flip."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", 0.40, Direction.LONG),),
        current_pairs=(pair_bias("EURUSD", -0.60, Direction.SHORT),),
    )

    change = diff.pairs[0]
    assert change.previous_spread == 0.40
    assert change.current_spread == -0.60


def test_a_pair_the_current_run_does_not_hold_is_reported_as_absent() -> None:
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(pair_bias("EURUSD", -1.4, Direction.SHORT),),
        current_pairs=(),
    )

    change = diff.pairs[0]
    assert change.current_direction is None
    assert change.current_spread is None
    assert change.previous_direction is Direction.SHORT


def test_flips_are_listed_before_other_changes() -> None:
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_pairs=(
            pair_bias("EURUSD", -2.0, Direction.SHORT, Conviction.LOW),
            pair_bias("AUDJPY", 0.05, Direction.LONG, Conviction.LOW),
        ),
        current_pairs=(
            pair_bias("EURUSD", -2.0, Direction.SHORT, Conviction.HIGH),
            pair_bias("AUDJPY", -0.04, Direction.SHORT, Conviction.LOW),
        ),
    )

    assert [change.pair for change in diff.pairs] == ["AUDJPY", "EURUSD"]


def test_a_changed_digest_is_reported_as_a_changed_config() -> None:
    """Re-weighting moves every currency at once. Read as a market move it is
    the fastest way to talk yourself into a trade that is not there."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.4),),
        previous_digest="abc123",
        current_digest="ff0099",
    )

    assert diff.config_changed is True


def test_an_unchanged_digest_leaves_config_changed_false() -> None:
    diff = two_runs((currency_score("USD", 1.0),), (currency_score("USD", 1.4),))

    assert diff.config_changed is False


def test_shortlist_arrivals_and_departures_are_named() -> None:
    before = TradeIdea(bias=pair_bias("EURUSD"))
    after = TradeIdea(bias=pair_bias("GBPJPY"))
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_shortlist=(before,),
        current_shortlist=(after,),
    )

    assert diff.shortlist_added == ("GBPJPY",)
    assert [pair for pair, _ in diff.shortlist_removed] == ["EURUSD"]


def test_a_pair_that_left_the_shortlist_says_why_from_the_current_run() -> None:
    """The reason is read off the current run's own row, which is data both
    reports carry. Anything else would be a reason invented by the renderer."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        current_pairs=(
            pair_bias(
                "EURUSD",
                tradeable=False,
                blockers=("cost", "event:unchecked"),
            ),
        ),
        previous_shortlist=(TradeIdea(bias=pair_bias("EURUSD")),),
    )

    _, reason = diff.shortlist_removed[0]
    assert "cost" in reason
    assert "event:unchecked" not in reason


def test_a_pair_that_left_with_the_run_says_so() -> None:
    """A pair the current run does not hold at all has no row to read a
    reason from, and saying nothing would read as a pair that was simply
    passed over."""
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        current_pairs=(),
        previous_shortlist=(TradeIdea(bias=pair_bias("EURUSD")),),
    )

    _, reason = diff.shortlist_removed[0]
    assert reason
    assert "current run" in reason


def test_a_shortlist_that_did_not_change_produces_no_entries() -> None:
    idea = TradeIdea(bias=pair_bias("EURUSD"))
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_shortlist=(idea,),
        current_shortlist=(idea,),
    )

    assert diff.shortlist_added == ()
    assert diff.shortlist_removed == ()


def test_new_and_resolved_warnings_are_separated() -> None:
    diff = two_runs(
        (currency_score("USD", 1.0),),
        (currency_score("USD", 1.0),),
        previous_warnings=("cftc unreachable", "POSITIONING had no data"),
        current_warnings=("POSITIONING had no data", "fred returned 503"),
    )

    assert diff.new_warnings == ("fred returned 503",)
    assert diff.resolved_warnings == ("cftc unreachable",)


# --- build_context -----------------------------------------------------------


def test_the_context_carries_exactly_the_documented_keys() -> None:
    """The dashboard renders the same mapping. A key added here and not
    documented is a key one template reads and the other does not know about,
    which is how two views of one run start telling different stories."""
    context = build_context(
        bias_report(), config=config_with(**{name.value: 0.1 for name in PillarName})
    )

    assert set(context) == CONTEXT_KEYS


def test_the_context_hands_the_template_the_report_itself() -> None:
    """Not a copy and not a summary: the template reads fields off it, and a
    reshaped object here would be a second place for the layout to be decided."""
    report = bias_report()

    assert build_context(report)["report"] is report


def test_currencies_come_back_strongest_first() -> None:
    report = bias_report(
        currencies=(
            currency_score("EUR", -0.22, rank=3),
            currency_score("USD", 1.20, rank=1),
            currency_score("JPY", 0.40, rank=2),
        ),
        pairs=(),
        shortlist=(),
    )

    order = [score.currency for score in build_context(report)["currencies"]]

    assert order == ["USD", "JPY", "EUR"]


def test_currencies_with_equal_composites_keep_a_stable_order() -> None:
    """`fbe.scoring.score_currencies` breaks ties by ISO code so that two runs
    on the same data produce the same table. The renderer has to agree, or the
    rank column prints out of order against a list it did not sort the same
    way."""
    report = bias_report(
        currencies=(
            currency_score("JPY", 1.00, rank=2),
            currency_score("CHF", 1.00, rank=1),
        ),
        pairs=(),
        shortlist=(),
    )

    order = [score.currency for score in build_context(report)["currencies"]]

    assert order == ["CHF", "JPY"]


def test_pairs_come_back_widest_disagreement_first() -> None:
    """By absolute spread. Sorting on the signed spread puts every short pair
    below every long one and buries the widest disagreement in the run."""
    report = bias_report(
        pairs=(
            pair_bias("EURUSD", -0.30, Direction.SHORT),
            pair_bias("GBPUSD", 1.80, Direction.LONG),
            pair_bias("AUDJPY", -2.40, Direction.SHORT),
        ),
        shortlist=(),
    )

    order = [row.pair for row in build_context(report)["pairs"]]

    assert order == ["AUDJPY", "GBPUSD", "EURUSD"]


def test_the_grid_is_the_one_the_grid_helper_builds() -> None:
    """`_grid` is the single place mirrored cells are produced. A second
    orientation rule here would invert the lower triangle of the matrix while
    every number on screen stayed plausible.

    The expectation is `_grid`'s own output rather than a literal grid, which
    would normally mean comparing the implementation with itself. It is
    defensible here because `_grid` has its own suite in
    ``tests/test_grid.py``: what this asserts is that `build_context` calls
    it rather than orienting cells a second way.
    """
    report = bias_report(pairs=(pair_bias("EURUSD", -1.42, Direction.SHORT),))

    assert build_context(report)["grid"] == _grid(report.pairs)


def test_the_pillar_columns_follow_the_configured_weights() -> None:
    config = config_with(
        monetary=0.05,
        inflation=0.40,
        growth=0.05,
        employment=0.20,
        external=0.05,
        positioning=0.05,
        risk=0.20,
    )

    context = build_context(bias_report(), config=config)

    # The literal order, not `_pillar_order(config)`. Delegating the
    # expectation to the implementation compares it with itself and passes for
    # any ordering at all, including none.
    assert tuple(context["pillar_order"]) == (
        PillarName.INFLATION,
        PillarName.EMPLOYMENT,
        PillarName.RISK,
        PillarName.MONETARY,
        PillarName.GROWTH,
        PillarName.EXTERNAL,
        PillarName.POSITIONING,
    )


def test_no_config_leaves_the_weights_block_with_nothing_to_render() -> None:
    context = build_context(bias_report())

    assert context["config"] is None
    assert tuple(context["pillar_order"]) == tuple(PillarName)


def test_the_diff_is_passed_through_untouched() -> None:
    diff = diff_reports(bias_report(asof=YESTERDAY), bias_report())

    assert build_context(bias_report(), diff=diff)["diff"] is diff


# --- render_report -----------------------------------------------------------


def test_the_rendered_report_carries_the_config_digest() -> None:
    """The one line that ties a call to the weights that produced it."""
    rendered = render_report(bias_report(config_digest="d4f0c1"))

    assert "d4f0c1" in rendered


def test_the_rendered_report_carries_the_asof_date() -> None:
    assert "2026-06-30" in render_report(bias_report())


def test_a_composite_on_the_page_moves_only_when_the_field_moves() -> None:
    """The perturbation test, and the reason the renderer is allowed no
    arithmetic of its own. A renderer that recomputed a composite from the
    pillars would keep printing the old number after the field changed, and
    the page would disagree with the object every consumer downstream reads.
    """
    report = bias_report(
        currencies=(currency_score("USD", 1.23, rank=1),), pairs=(), shortlist=()
    )
    before = render_report(report)

    moved = replace(
        report, currencies=(replace(report.currencies[0], composite=-0.35),)
    )
    after = render_report(moved)

    assert "+1.23" in before
    assert "+1.23" not in after
    assert "-0.35" in after


def test_a_pillar_with_no_score_prints_a_marker_rather_than_a_number() -> None:
    """A pillar that formed no opinion and a pillar that formed one of zero
    are different facts, and ``0.00`` in the column says the second."""
    report = bias_report(
        currencies=(
            currency_score(
                "USD",
                1.20,
                pillars={PillarName.MONETARY: pillar_score(PillarName.MONETARY)},
            ),
        ),
        pairs=(),
        shortlist=(),
    )

    rendered = render_report(
        report, config=config_with(**{name.value: 0.1 for name in PillarName})
    )

    ranking = rendered.split("## 1.")[1].split("## 2.")[0]
    row = next(line for line in ranking.splitlines() if line.startswith("| 1 |"))
    # Both halves. Asserting only the absence of the number passes against a
    # template that dropped the pillar columns entirely.
    assert "+0.00" not in row
    # Counted on " . |" rather than "| . |": adjacent cells share a pipe, so
    # the second form finds three of six.
    assert row.count(" . |") == len(PillarName) - 1


def test_the_report_claims_no_measured_edge() -> None:
    """Nothing in this project has been validated out of sample yet, and the
    standing instruction in ``CLAUDE.md`` forbids the claim anywhere, a
    rendered page included."""
    rendered = render_report(bias_report()).lower()

    for forbidden in ("backtested", "win rate", "hit rate", "proven", "profitable"):
        assert forbidden not in rendered


def test_no_baseline_renders_the_first_run_line() -> None:
    rendered = render_report(bias_report())

    assert "No baseline report" in rendered


def changed_config_diff() -> ReportDiff:
    """Two runs on different weights, one of them with a direction flip."""
    return diff_reports(
        bias_report(
            asof=YESTERDAY,
            currencies=(currency_score("USD", 1.00, rank=1),),
            pairs=(pair_bias("EURUSD", 0.40, Direction.LONG),),
            shortlist=(),
            config_digest="abc123",
        ),
        bias_report(
            currencies=(currency_score("USD", 1.90, rank=1),),
            pairs=(pair_bias("EURUSD", -0.60, Direction.SHORT),),
            shortlist=(),
            config_digest="ff0099",
        ),
    )


def test_a_changed_config_withholds_the_score_table() -> None:
    """Re-weighting moves every currency at once. Printed as a delta it is a
    market move that did not happen, on the section a reader turns to first."""
    rendered = render_report(
        bias_report(config_digest="ff0099"), diff=changed_config_diff()
    )

    changed = rendered.split("## 6.")[1]
    assert "digest changed" in changed
    assert "| CCY | Then | Now | Delta | Rank |" not in changed


def test_a_changed_config_still_shows_a_direction_flip() -> None:
    """A flip is a change in the story, not a score movement, and
    `diff_reports` calls it the loudest thing this engine can say. A
    re-weighting must not be able to silence it: the pillars were re-weighted
    and the model also reversed its side, and the second fact is still true.
    """
    rendered = render_report(
        bias_report(config_digest="ff0099"), diff=changed_config_diff()
    )

    changed = rendered.split("## 6.")[1]
    assert "EURUSD: long to short" in changed
    assert "(flip)" in changed


def test_a_reported_pair_shows_both_its_spreads_on_the_page() -> None:
    """ "long to short" alone does not say whether the model moved a long way
    or barely crossed zero, and those call for different amounts of attention.
    """
    diff = diff_reports(
        bias_report(
            asof=YESTERDAY,
            pairs=(pair_bias("EURUSD", 0.40, Direction.LONG),),
            shortlist=(),
        ),
        bias_report(pairs=(pair_bias("EURUSD", -0.60, Direction.SHORT),), shortlist=()),
    )

    rendered = render_report(bias_report(), diff=diff)

    assert "spread +0.40 to -0.60" in rendered


def test_a_changed_config_withholds_the_spreads_too() -> None:
    """A spread is ``composite(base) - composite(quote)``, so it is a score
    delta in another shape.

    Printing one beside a withheld currency table invites exactly the
    comparison the table was withheld to prevent: a re-weighting moves both
    legs, and the reader is handed the difference and told the section is not
    comparable in the same breath.
    """
    rendered = render_report(
        bias_report(config_digest="ff0099"), diff=changed_config_diff()
    )

    changed = rendered.split("## 6.")[1]
    assert "spreads not comparable" in changed
    assert "spread +0.40 to -0.60" not in changed


def test_a_template_directory_override_is_used(tmp_path: Path) -> None:
    """So a template change can be tested without touching the installed
    package."""
    (tmp_path / "report.md.j2").write_text(
        "digest {{ report.config_digest }}", encoding="utf-8"
    )

    rendered = render_report(bias_report(config_digest="d4f0c1"), template_dir=tmp_path)

    assert rendered == "digest d4f0c1"


def test_a_template_reading_a_key_the_context_lacks_raises(tmp_path: Path) -> None:
    """``StrictUndefined``, pinned. Jinja's default renders an unknown name as
    an empty string, so a renamed context key would empty a table rather than
    fail, and the report would still look like a report.
    """
    (tmp_path / "report.md.j2").write_text("{{ nothing_supplies_this }}", "utf-8")

    with pytest.raises(UndefinedError):
        render_report(bias_report(), template_dir=tmp_path)


# --- the codec's refusals ----------------------------------------------------


def test_an_absent_sidecar_raises_rather_than_reading_as_an_empty_run(
    tmp_path: Path,
) -> None:
    """A report whose numbers cannot be read back is not a report. An empty
    one returned here is a run with no opinions, which is the quiet default
    the whole package is written against."""
    with pytest.raises(FileNotFoundError):
        load_report(tmp_path / "bias-2026-06-30.json")


def test_a_not_a_number_is_refused_rather_than_written(tmp_path: Path) -> None:
    """A NaN arrives from a division by zero upstream. Written out it reads
    back as a number and poisons any average computed over the column."""
    broken = bias_report(
        pairs=(pair_bias("EURUSD", spread=float("nan")),), shortlist=()
    )

    with pytest.raises(ValueError, match="[Nn]a[Nn]"):
        write_report(broken, tmp_path)


def test_a_mapping_key_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    """JSON objects are keyed by strings. A key coerced with ``str`` comes back
    as a string and stops matching the key the writer used, so a lookup that
    worked before the round trip returns nothing after it."""
    scored = replace(pillar_score(), inputs=(observation(meta={7: "seven"}),))
    broken = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    with pytest.raises(TypeError, match="keyed by"):
        write_report(broken, tmp_path)


def test_a_date_in_free_form_provenance_is_refused_rather_than_flattened(
    tmp_path: Path,
) -> None:
    """``meta`` comes from ``data/manual/*.yaml`` through ``yaml.safe_load``,
    and an unquoted ``vintage: 2026-06-29`` is a `datetime.date`, not a string.

    JSON has no date, and `_decoded` has only `typing.Any` to go on, so a date
    written as ``"2026-06-29"`` reads back as text and the report no longer
    equals itself. The loss is invisible on the page and in the file, so it is
    refused at the one point where both are in hand.
    """
    scored = replace(
        pillar_score(), inputs=(observation(meta={"vintage": date(2026, 6, 29)}),)
    )
    broken = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    with pytest.raises(TypeError, match="quote it"):
        write_report(broken, tmp_path)


def test_a_sequence_in_free_form_provenance_round_trips_as_a_list(
    tmp_path: Path,
) -> None:
    """A YAML list is the one nested shape JSON does carry, so it is kept
    rather than refused, and it comes back as a list because that is what it
    was written as."""
    scored = replace(
        pillar_score(), inputs=(observation(meta={"tags": ["revised", "final"]}),)
    )
    original = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    restored = load_report(write_report(original, tmp_path))

    assert restored == original


@pytest.mark.parametrize(
    "field_path, value",
    [
        (("pairs", 0, "spread"), True),
        (("pairs", 0, "tradeable"), 1),
        (("currencies", 0, "rank"), True),
    ],
)
def test_a_boolean_is_not_a_number_and_a_number_is_not_a_boolean(
    tmp_path: Path, field_path: tuple[str, int, str], value: object
) -> None:
    """``bool`` is a subclass of ``int`` in Python, so ``true`` in a risk
    figure passes an ``isinstance`` check and is then arithmetic."""
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    section, index, name = field_path
    payload[section][index][name] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=name):
        load_report(path)


def test_a_scalar_where_a_list_belongs_raises(tmp_path: Path) -> None:
    """Wrapped in a one-element tuple instead, a single blocker string would
    become 14 one-character blockers and every one of them would render."""
    markdown = write_report(bias_report(), tmp_path)
    path = sidecar_of(markdown)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pairs"][0]["blockers"] = "cost:unchecked"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="blockers"):
        load_report(path)


def test_a_sidecar_that_cannot_be_placed_takes_the_markdown_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of both-or-neither, and the half that fails silently.

    A Markdown with no sidecar is invisible to a reader looking for a problem
    and perfectly readable to ``--compare``, so the run reports success and the
    what-changed section is empty forever.
    """
    import fbe.report as report_module

    real_replace = report_module.os.replace
    calls: list[int] = []

    def failing_replace(source: object, destination: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("the sidecar could not be put in place")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(report_module.os, "replace", failing_replace)

    with pytest.raises(OSError, match="sidecar"):
        write_report(bias_report(), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_the_newest_report_is_chosen_by_date_and_not_by_name(tmp_path: Path) -> None:
    """``bias-*.json`` matches a basic-format ISO date too, and ``'0' > '-'``,
    so a name sort puts ``bias-20260101.json`` after every extended-format
    name. ``--compare last`` would then diff against a six-month-old run and
    report half a year of drift as the overnight move."""
    write_dated(tmp_path, date(2026, 6, 30))
    (tmp_path / "bias-20260101.json").write_text("{}", encoding="utf-8")

    found = latest_report(tmp_path)

    assert found is not None
    assert found.name == "bias-2026-06-30.json"


# --- criterion 6: the renderers derive nothing --------------------------------


RENDERED_TEMPLATES = (
    PACKAGE_ROOT / "templates" / TEMPLATE_NAME,
    PACKAGE_ROOT / "dashboard" / "templates" / "dashboard.html.j2",
)
"""Both views of one run. The dashboard is the page the owner reads on a phone
during the session, so a criterion met in the report and broken here is met
where nobody is looking and broken where they are."""


def jinja_expressions(template: Path) -> list[str]:
    """Return every ``{{ ... }}`` and ``{% ... %}`` body, string literals removed.

    Only what Jinja evaluates. Prose around it is not a derivation, and the
    dashboard's own header comment contains the words "heat-p1..p4 / n1..n4",
    which a substring search over the whole file reads as arithmetic. Quoted
    text inside an expression is dropped for the same reason: a format spec or
    a ``join('/')`` separator is a string, not an operator.

    ``{# ... #}`` comments are not matched at all, since neither opener begins
    one.
    """
    bodies = re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", template.read_text("utf-8"), re.S)
    return [
        re.sub(r"'[^']*'|\"[^\"]*\"", "", expression or statement or "")
        for expression, statement in bodies
    ]


@pytest.mark.parametrize("template", RENDERED_TEMPLATES, ids=lambda path: path.name)
def test_a_renderer_derives_no_number_by_division(template: Path) -> None:
    """Acceptance criterion 6: every number on the page is read off a field.

    Both templates carried the same expression:

        {{ '%.2f%%' | format(idea.size.realised_risk_amount
                             / idea.size.account_balance * 100) }}

    Two things followed. The percentage existed nowhere but the render, so
    nothing downstream could check the figure the owner reads against the
    plan's rule, which the plan states in both money and percentage and the
    engine held only in money. And the expression was unguarded, so a zero
    balance would have raised a `ZeroDivisionError` out of a Jinja template
    with no line of Python in the traceback. Latent rather than an incident:
    `fbe report` attaches no sizes yet.

    Both now read `fbe.types.PositionSize.realised_risk_fraction`.

    Multiplying a fraction by 100 is a unit conversion rather than a
    derivation: the value is on the object and only its scale changes, so it
    does not fail this.

    This replaces the strict xfail that stood here while the change was
    waiting on an architect ruling, and it covers both files rather than one,
    because the xfail named only the Markdown template and the violation was
    in two.
    """
    dividing = [body for body in jinja_expressions(template) if "/" in body]

    assert dividing == []


def test_the_expression_scan_reads_statements_and_not_only_expressions(
    tmp_path: Path,
) -> None:
    """A division hidden in ``{% set %}`` is still a derivation.

    Both templates happen to put every arithmetic expression in ``{{ }}``
    today, so the statement arm of `jinja_expressions` is exercised by
    nothing in the tree and a later simplification of the helper would narrow
    criterion 6 to ``{{ }}`` without failing anything.

    Also pins the two exclusions the helper's docstring claims, since both
    are load-bearing rather than defensive: a comment carrying a slash, which
    both real templates have in their header, and a slash inside a string
    literal, which `report.md.j2` has nine of as ``'n/a'``.
    """
    template = tmp_path / "probe.md.j2"
    template.write_text(
        "{# heat-p1..p4 / n1..n4 #}\n"
        "{{ 'n/a' if x is none else x }}\n"
        "{% set share = a.realised_risk_amount / a.account_balance %}\n",
        encoding="utf-8",
    )

    dividing = [body for body in jinja_expressions(template) if "/" in body]

    assert len(dividing) == 1
    assert "realised_risk_amount" in dividing[0]


def test_the_report_prints_the_realised_fraction_it_was_handed() -> None:
    """Perturbation, so the page cannot be right for the wrong reason.

    A renderer that recomputed the share from the two money fields would
    print the same number for a `PositionSize` whose realised amount had
    moved, and the page would disagree with the object every consumer
    downstream reads.
    """
    idea = TradeIdea(bias=pair_bias(), size=position_size(), rationale="Wide gap.")
    rendered = render_report(bias_report(shortlist=(idea,)))

    assert "1.42%" in rendered

    moved = replace(idea, size=replace(idea.size, realised_risk_amount=10.0))
    after = render_report(bias_report(shortlist=(moved,)))

    assert "1.42%" not in after
    assert "0.50%" in after


def render_dashboard(idea: TradeIdea) -> str:
    """Render the dashboard card for one idea, without going through `build`.

    `fbe.dashboard.build.render_dashboard` is still scaffolded, so the
    template is driven directly with the context `build_context` produces
    plus the `view` namespace the dashboard layer adds. The same approach and
    the same reason as ``tests/test_blockers.py``.

    Only the members the template actually calls are supplied, so a template
    that starts reading a new one fails here rather than rendering a blank.
    """
    environment = Environment(
        loader=FileSystemLoader(PACKAGE_ROOT / "dashboard" / "templates"),
        undefined=StrictUndefined,
        autoescape=True,
        keep_trailing_newline=True,
    )
    context = build_context(bias_report(shortlist=(idea,)))
    context["view"] = SimpleNamespace(
        title="FX bias",
        bar_pct=lambda value: 50.0,
        heat=lambda spread: "heat-p1",
        at_pct=lambda when: 50.0,
        span_pct=lambda a, b: 10.0,
        legend=(),
        hour_marks=(),
        blackouts=(),
    )
    return environment.get_template("dashboard.html.j2").render(**context)


def test_the_dashboard_prints_the_realised_fraction_it_was_handed() -> None:
    """The same perturbation as the report, on the page read during a session.

    Proving the dashboard does not divide is only half of criterion 2. A
    dashboard reading `risk_fraction` instead passes the division test and
    prints the intended share where the realised one belongs, on a card whose
    own words are "of balance) against an intended", so the same figure
    appears twice under two different labels.

    That is this project's opening failure mode: a plausible number, no
    exception, and the report correct while the phone is wrong.
    """
    idea = TradeIdea(bias=pair_bias(), size=position_size(), rationale="Wide gap.")

    rendered = render_dashboard(idea)

    assert "1.42%" in rendered
    # The intended fraction is 1.5%. Asserted absent so a card reading
    # `risk_fraction` fails here rather than printing a plausible figure.
    assert "1.50%" not in rendered

    moved = replace(idea, size=replace(idea.size, realised_risk_amount=10.0))
    after = render_dashboard(moved)

    assert "1.42%" not in after
    assert "0.50%" in after


def test_a_zero_balance_cannot_reach_a_renderer() -> None:
    """The guard is in `fbe.risk.position_size`, not in the property.

    Recorded here as well as in ``tests/test_position_size.py`` because this
    is the file that names the criterion: the reason the renderers need no
    branch around the division is that the object carrying a zero denominator
    cannot be built by the only thing that builds one.
    """
    from fbe.config import BrokerConfig, RiskConfig
    from fbe.risk import position_size as size_a_position

    with pytest.raises(ValueError, match="account_balance"):
        size_a_position(
            "EURUSD",
            1.0850,
            1.0825,
            RiskConfig(account_balance=0.0),
            {"USDZAR": 18.50},
            broker=BrokerConfig(),
            risk_fraction=0.01,
        )


# --- the write path under failure --------------------------------------------


def failing_second_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the sidecar's `os.replace` fail and leave the Markdown's alone."""
    import fbe.report as report_module

    real_replace = report_module.os.replace
    calls: list[int] = []

    def replace_or_fail(source: object, destination: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("the sidecar could not be put in place")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(report_module.os, "replace", replace_or_fail)


def test_a_failed_sidecar_puts_the_previous_run_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlinking the new Markdown is not a rollback when one was already there.

    By the time the sidecar fails, `os.replace` has overwritten yesterday's
    Markdown, so removing the new file removes the old one with it. That
    leaves a sidecar with no Markdown, one of the two half states this
    function exists to prevent, and it destroys an artefact `CLAUDE.md` says
    no rerun can recreate.
    """
    write_report(bias_report(config_digest="first"), tmp_path)
    markdown = tmp_path / "bias-2026-06-30.md"
    before = (markdown.read_bytes(), sidecar_of(markdown).read_bytes())
    failing_second_replace(monkeypatch)

    with pytest.raises(OSError, match="sidecar"):
        write_report(bias_report(config_digest="second"), tmp_path)

    assert (markdown.read_bytes(), sidecar_of(markdown).read_bytes()) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "bias-2026-06-30.json",
        "bias-2026-06-30.md",
    ]


def test_two_runs_for_one_date_cannot_be_glued_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temporary names shared between runs pair one run's page with another's
    numbers.

    With a name derived from the as-of date, two runs for one date in one
    directory write to the same two temporary files. Interleaved, the second
    run's Markdown ends up beside the first run's sidecar, nothing raises in
    the first run, and every figure on both sides is plausible.

    Reproduced here by starting a second write from inside the first, between
    its two temporary writes.
    """
    import fbe.report as report_module

    real_write_text = Path.write_text
    started = False

    def interleave(
        self: Path, data: str, encoding: str | None = None, **kwargs: object
    ) -> int:
        nonlocal started
        written = real_write_text(self, data, encoding=encoding)  # type: ignore[arg-type]
        if not started and self.name.endswith(".part") and ".md." in self.name:
            started = True
            report_module.write_report(bias_report(config_digest="run-b"), tmp_path)
        return written

    monkeypatch.setattr(Path, "write_text", interleave)

    write_report(bias_report(config_digest="run-a"), tmp_path)

    markdown = tmp_path / "bias-2026-06-30.md"
    assert load_report(markdown).config_digest in {"run-a", "run-b"}
    assert load_report(markdown).config_digest in markdown.read_text(encoding="utf-8")


def test_a_tuple_in_free_form_provenance_is_refused(tmp_path: Path) -> None:
    """JSON has no tuple, so one written out reads back as a list and the
    report stops equalling itself. Nothing produces a tuple in ``meta``
    today; the point is that the guarantee `load_report` states holds for
    whatever does."""
    scored = replace(
        pillar_score(), inputs=(observation(meta={"tags": ("revised", "final")}),)
    )
    broken = bias_report(
        currencies=(currency_score("USD", 1.2, pillars={PillarName.MONETARY: scored}),)
    )

    with pytest.raises(TypeError, match="tuple"):
        write_report(broken, tmp_path)
