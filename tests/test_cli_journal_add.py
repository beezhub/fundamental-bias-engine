"""``fbe journal add``: the record is written while the trade is being taken.

The command exists for one reason, and every test here is downstream of it. A
journal that asks the trader to type in the bias records the bias they
remember, and the whole question the journal is kept to answer, whether the
losing trades were the ones taken against the engine, against the plan, or
neither, is answered from memory and therefore not answered at all. So the
engine's view of that pair on that date is attached by the command, and the
tests assert it matches the chain's output rather than merely being non-empty.

The second thing worth pinning is the R-multiple. It is measured against the
realised risk of the position, what the lots actually expose once the broker's
lot step has rounded them, not against the intended risk the sizing rule asked
for. The fixture is built so the two differ: at MEDIUM the ladder asks for 1.5%
of a 2,000 balance, which is 30.00, and a 38 pip stop turns that into 0.0789
lots, which the 0.01 step rounds down to 0.07 and 26.60 of realised risk. Every
money assertion below is that arithmetic, written out as literals rather than
recomputed from the implementation's own formula, because a test that restates
the formula holds for any sign and any factor.

Nothing here reaches the network: the collection, the scorer and the bias layer
are replaced in every test, the same way ``tests/test_cli_bias.py`` does it.
The fakes are deliberately not identities. The bias layer returns a decoy row
ahead of the one being journalled, and the filter demotes the conviction one
rung, so a command that took the first row it was handed or skipped the filter
fails rather than passes unnoticed.

Nothing writes to `fbe.journal.JOURNAL_PATH` either. That file holds the
owner's real entry prices and profit and loss on a live account and is
git-ignored for that reason, so every test points the command at ``tmp_path``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fbe import cli as cli_module
from fbe import config as config_module
from fbe.cli import app
from fbe.datasources.collect import CollectionResult
from fbe.journal import TradeRecord, load
from fbe.types import (
    Conviction,
    CurrencyScore,
    Direction,
    Observation,
    PairBias,
    PillarName,
    PillarScore,
)

runner = CliRunner()

USAGE_ERROR = 2
"""Click's exit code for a value the parser or the command refused."""

UNUSABLE = 1
"""`fbe.cli.EXIT_UNUSABLE`, for a run that cannot produce an honest record."""

OPENED = "2026-09-21 09:00"
"""The open timestamp every test passes, so the trade id is deterministic."""

TRADE_ID = "EURUSD-20260921T0900"
"""Derived from the pair and the open timestamp, and not an option.

`docs/risk-and-execution.md` makes corrections an append with the same id, and
the command has no ``--trade-id``, so the id has to be a function of what the
caller gave. Pair plus the minute the trade was opened is the form
``tests/test_journal.py`` already uses.
"""

LOTS = "0.07"
"""The size traded, and the one the lot step produced.

At MEDIUM the ladder asks for 1.5% of 2,000, which is 30.00. A 38 pip stop on
EURUSD costs 0.0001 per unit per pip, so 30.00 buys 7,894 units, which is
0.0789 lots and rounds down to 0.07 on a 0.01 step. The account therefore risks
26.60 and not 30.00, and the R-multiple criterion on the issue is exactly the
requirement that the record carries the first number.
"""

RISK = 26.60
"""Realised risk of `LOTS` on a 38 pip stop, in the account currency."""

INTENDED = 30.00
"""What the ladder asked for at MEDIUM on a 2,000 balance. Not what was taken."""

WIN = 41.30
"""A short from 1.0850 to 1.0791, 59 pips at 0.70 a pip."""

ENGINE = PairBias(
    pair="EURUSD",
    base="EUR",
    quote="USD",
    spread=-2.31,
    direction=Direction.SHORT,
    conviction=Conviction.HIGH,
    asof=date(2026, 9, 21),
    base_score=-0.95,
    quote_score=1.36,
    agreement=0.78,
    tradeable=True,
    blockers=(),
)
"""The engine's unfiltered view of EURUSD on the day.

Short, which is what the worked example in ``docs/interfaces.md`` prints. The
conviction here is the one `build_pair_biases` produced, before the filters ran
on it, which is why it is HIGH and every assertion below expects MEDIUM.
"""

DECOY = PairBias(
    pair="GBPUSD",
    base="GBP",
    quote="USD",
    spread=1.84,
    direction=Direction.LONG,
    conviction=Conviction.LOW,
    asof=date(2026, 9, 21),
    base_score=0.48,
    quote_score=-1.36,
    agreement=0.55,
    tradeable=True,
    blockers=(),
)
"""A second row the chain produces, returned ahead of the one being journalled.

Every field a record copies differs from `ENGINE`, so a command that takes the
first row rather than the row for the pair records visibly wrong numbers
instead of coincidentally right ones.
"""

DEMOTED: Mapping[Conviction, Conviction] = {
    Conviction.HIGH: Conviction.MEDIUM,
    Conviction.MEDIUM: Conviction.LOW,
    Conviction.LOW: Conviction.NONE,
    Conviction.NONE: Conviction.NONE,
}
"""What the stand-in filter does to a conviction.

`apply_filters` demotes on stale data and on a calendar blackout, so a record
carrying the unfiltered conviction claims the engine held a view it had already
withdrawn. An identity fake cannot tell the two apart, so this one always moves
the value and the tests assert the moved one.
"""


def pillars(currency: str, composite: float) -> Mapping[PillarName, PillarScore]:
    """Seven pillar scores, spread around the composite and keyed by currency.

    The values differ per currency and per pillar so that a record copying the
    wrong leg, or the same leg twice, is visible rather than plausible.
    """
    return {
        name: PillarScore(
            pillar=name,
            currency=currency,
            raw=None,
            z=None,
            score=round(composite + index * 0.1, 2),
            weight=0.1,
            asof=date(2026, 9, 21),
        )
        for index, name in enumerate(PillarName)
    }


def currency_score(code: str, composite: float, coverage: float = 1.0) -> CurrencyScore:
    """One leg's score, carrying the seven pillar values the record copies."""
    return CurrencyScore(
        currency=code,
        composite=composite,
        pillars=pillars(code, composite),
        asof=date(2026, 9, 21),
        rank=1,
        dispersion=0.40,
        coverage=coverage,
    )


OBSERVATION = Observation(
    indicator="policy_rate",
    currency="EUR",
    value=2.15,
    period=date(2026, 8, 1),
    source="fred",
    series_id="ECBDFR",
    unit="percent",
    released_at=datetime(2026, 9, 11, 12, 15, tzinfo=UTC),
)
"""One reading, so the run the fakes describe is a run that found something.

`CollectionResult.usable` is false on an empty observation set, and the command
refuses on it, so a fake returning nothing would describe an outage rather than
a working chain and every test built on it would be testing the refusal.
"""


def config_file(
    tmp_path: Path,
    account_currency: str = "ZAR",
    contract_size: float | None = None,
    account_balance: float = 2000.0,
) -> Path:
    """A config pointing every directory at ``tmp_path``.

    ``account_currency`` is a parameter because the money figures need a route
    from the pair's quote currency to the account's, and the engine has no
    source for one to ZAR: no G10 cross has a ZAR leg, there is no USDZAR
    series in `fbe.datasources.prices`, and nothing in ``data/manual`` supplies
    one. An account denominated in the pair's quote currency needs no
    conversion at all, so it is the only kind this suite can compute a money
    outcome for today. That is a real gap, it is raised on the issue, and the
    ZAR case below asserts what the command does instead of papering over it.

    ``contract_size`` is a parameter so one test can prove the figure comes
    from the config rather than from a literal that happens to equal its
    default.
    """
    path = tmp_path / "config.yaml"
    broker = (
        "" if contract_size is None else f"broker:\n  contract_size: {contract_size}\n"
    )
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
        f"risk:\n"
        f"  account_currency: {account_currency}\n"
        f"  account_balance: {account_balance}\n" + broker,
        encoding="utf-8",
    )
    return path


def add(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *args: str,
    engine: PairBias | None = None,
    account_currency: str = "ZAR",
    contract_size: float | None = None,
    account_balance: float = 2000.0,
    empty_cache: bool = False,
    coverage: float = 1.0,
    rows_raise: bool = False,
    seen: dict[str, object] | None = None,
) -> tuple[int, str, Path]:
    """Run ``fbe journal add`` with the chain replaced and the journal in tmp.

    Returns the exit code, the combined output, and the journal path, so a test
    can assert on what was written as well as on what was printed. Pass ``seen``
    to collect what each stand-in was handed, which is how the tests pin the
    date the chain was run for and that the cache was read offline.
    ``empty_cache``, ``coverage`` and ``rows_raise`` each describe one of the
    three ways the chain can come back with nothing worth recording.
    """
    journal = tmp_path / "trades.jsonl"
    config_path = config_file(
        tmp_path, account_currency, contract_size, account_balance
    )
    row = ENGINE if engine is None else engine
    noted: dict[str, object] = {} if seen is None else seen

    def fake_collect(config_in: object, **kwargs: object) -> CollectionResult:
        noted["offline"] = getattr(config_in, "offline", None)
        noted["end"] = kwargs.get("end")
        return CollectionResult(
            observations=() if empty_cache else (OBSERVATION,),
            outcomes=(),
            gaps={},
        )

    def fake_score_currencies(
        observations: object, pillars_in: object, scoring: object, asof: date
    ) -> Sequence[CurrencyScore]:
        noted["scored_asof"] = asof
        composites = {row.base: row.base_score, row.quote: row.quote_score}
        composites.setdefault(DECOY.base, DECOY.base_score)
        composites.setdefault(DECOY.quote, DECOY.quote_score)
        return tuple(
            currency_score(code, value, coverage) for code, value in composites.items()
        )

    def fake_build_pair_biases(
        scores: Sequence[CurrencyScore],
        config_in: object,
        asof: date,
        guard: object = None,
    ) -> Sequence[PairBias]:
        noted["biased_asof"] = asof
        if rows_raise:
            raise KeyError(f"{row.base} has no CurrencyScore, so {row.pair} ...")
        return (DECOY, row)

    def fake_apply_filters(
        bias_row: PairBias,
        scores: Mapping[str, CurrencyScore],
        config_in: object,
        asof: date,
        **kwargs: object,
    ) -> PairBias:
        noted["filtered_pair"] = bias_row.pair
        return replace(bias_row, conviction=DEMOTED[bias_row.conviction])

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)
    monkeypatch.setattr("fbe.cli.build_pair_biases", fake_build_pair_biases)
    monkeypatch.setattr("fbe.cli.apply_filters", fake_apply_filters)
    monkeypatch.setattr("fbe.journal.JOURNAL_PATH", journal)

    result = runner.invoke(app, ["--config", str(config_path), "journal", "add", *args])
    output = result.output
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        # Surfaced rather than swallowed. A CliRunner failure with an empty
        # output is the least informative thing a test can report, and this
        # is the line that turns it into the traceback.
        output = f"{output}\n{result.exception!r}"
    return result.exit_code, output, journal


def taken(**overrides: str) -> list[str]:
    """The arguments for one complete trade, with any part replaced."""
    fields = {
        "pair": "EURUSD",
        "--direction": "short",
        "--entry": "1.0850",
        "--stop": "1.0888",
        "--lots": LOTS,
        "--opened": OPENED,
    }
    fields.update(overrides)
    arguments: list[str] = [fields.pop("pair")]
    for name, value in fields.items():
        arguments.extend([name, value])
    return arguments


def only_record(journal: Path) -> TradeRecord:
    """The one record on disk, refusing anything else."""
    records = load(path=journal)
    assert len(records) == 1, [record.trade_id for record in records]
    return records[0]


# --- one record, with what the caller gave ------------------------------------


def test_one_invocation_writes_exactly_one_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through `journal.append`, so the file stays append-only and one line."""
    code, output, journal = add(monkeypatch, tmp_path, *taken())

    assert code == 0, output
    assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_the_record_carries_what_the_caller_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pair, direction, entry, stop, lots and the open timestamp, unchanged.

    The trader's own numbers are the half of the record the engine must not
    touch, because they are the only part it cannot reconstruct.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    record = only_record(journal)

    assert record.pair == "EURUSD"
    assert record.direction is Direction.SHORT
    assert record.entry == pytest.approx(1.0850)
    assert record.stop == pytest.approx(1.0888)
    assert record.lots == pytest.approx(0.07)
    assert record.units == pytest.approx(7000.0)
    assert record.opened_at == datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    # The venue is part of what was traded, not decoration. Spreads and fills
    # move when the owner changes broker, and a book that does not say which
    # one a trade was taken on cannot separate the two.
    assert record.broker == "generic-retail-micro"


def test_the_trade_id_is_derived_from_the_pair_and_the_minute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """There is no ``--trade-id``, so the id has to be a function of the input.

    Corrections are an append with the same id, so two adds describing one
    trade have to agree on it without the trader remembering a string.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    assert only_record(journal).trade_id == TRADE_ID


def test_the_id_comes_from_the_current_minute_when_the_open_is_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default path, and therefore the common one.

    An id built from the wall clock at some later point in the command rather
    than from the recorded ``opened_at`` would drift by a minute now and then,
    and the correction the trader appends would land as a second trade.
    """
    arguments = taken()
    index = arguments.index("--opened")
    before = datetime.now(UTC)

    _, output, journal = add(
        monkeypatch, tmp_path, *arguments[:index], *arguments[index + 2 :]
    )

    after = datetime.now(UTC)
    record = only_record(journal)

    assert record.trade_id == f"EURUSD-{record.opened_at:%Y%m%dT%H%M}", output
    assert record.trade_id in {
        f"EURUSD-{before:%Y%m%dT%H%M}",
        f"EURUSD-{after:%Y%m%dT%H%M}",
    }


# --- the engine's view, attached rather than asked for ------------------------


def test_the_engine_view_is_attached_from_the_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The criterion the command exists for.

    Asserted against the values the chain returned for that pair, not against
    "not blank". A command that accepted the bias as a flag passes a non-blank
    assertion and fails this one, and so does one that recorded the first pair
    the chain produced: the stand-in returns `DECOY` ahead of the EURUSD row
    and every score here differs from it.
    """
    seen: dict[str, object] = {}

    _, _, journal = add(monkeypatch, tmp_path, *taken(), seen=seen)

    record = only_record(journal)

    assert seen["filtered_pair"] == "EURUSD"
    assert record.base_score == pytest.approx(ENGINE.base_score)
    assert record.quote_score == pytest.approx(ENGINE.quote_score)
    assert record.spread_score == pytest.approx(ENGINE.spread)
    assert record.base_score != pytest.approx(DECOY.base_score)


def test_the_recorded_conviction_is_the_filtered_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`apply_filters` is what withdraws a view, so its answer is the record's.

    The filters demote on stale inputs and on a calendar blackout. A record
    carrying the conviction `build_pair_biases` produced claims the engine held
    a view it had already taken back, and the review would then score the
    ladder on rungs the engine was not standing on.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    record = only_record(journal)

    assert ENGINE.conviction is Conviction.HIGH
    assert record.conviction is Conviction.MEDIUM


def test_the_chain_is_run_for_the_date_the_trade_was_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not for today. A trade written up in the evening is still that day's.

    Backfilling a trade from last week with this week's bias is the failure
    this command exists to remove, and it is silent: the record looks complete
    and carries a view the trader never saw.
    """
    seen: dict[str, object] = {}

    add(monkeypatch, tmp_path, *taken(), seen=seen)

    assert seen["end"] == date(2026, 9, 21)
    assert seen["scored_asof"] == date(2026, 9, 21)
    assert seen["biased_asof"] == date(2026, 9, 21)

    older: dict[str, object] = {}

    add(monkeypatch, tmp_path, *taken(**{"--opened": "2020-01-06 09:00"}), seen=older)

    assert older["end"] == date(2020, 1, 6)
    assert older["scored_asof"] == date(2020, 1, 6)


def test_the_chain_reads_the_cache_and_never_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Offline, for the reason ``fbe bias`` gives at the same call.

    A TTL rolling over mid-session would let the journal record a different
    view from the one the trader was looking at when they took the trade, and
    the record would be wrong in the one way nothing downstream can detect.
    """
    seen: dict[str, object] = {}

    add(monkeypatch, tmp_path, *taken(), seen=seen)

    assert seen["offline"] is True


def test_a_pair_the_chain_has_no_row_for_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A G10 pair the run produced nothing for. Recorded, it carries no view.

    Which is the one thing the command is for, so it exits rather than writing
    a record whose bias fields would all be blank.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"pair": "AUDNZD"}))

    assert code == UNUSABLE, output
    assert "AUDNZD" in output
    assert not journal.exists()


def test_the_config_digest_is_the_running_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The digest is what makes the recorded scores reproducible later.

    A record carrying scores and no digest cannot be checked against anything,
    because the weights that produced them may have changed since.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    expected = config_module.load_config(config_file(tmp_path)).digest()

    assert only_record(journal).config_digest == expected


def test_a_trade_with_the_engine_is_recorded_as_agreeing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Short on a pair the engine called short."""
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    assert only_record(journal).agreed_with_bias is True


def test_a_trade_against_the_engine_is_recorded_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The journal is a record, not a gate.

    An override is legitimate and it is exactly the thing a later review needs
    to count. A command that refused it would leave the discretionary trades
    out of the book and flatter the model's record by removing the trades it
    did not ask for.
    """
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(**{"--direction": "long"})
    )

    record = only_record(journal)

    assert code == 0, output
    assert record.direction is Direction.LONG
    assert record.agreed_with_bias is False


def test_a_pair_the_engine_has_no_view_on_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`Conviction.NONE` is a view, and the trade still happened.

    Refusing here would drop precisely the trades a review most wants: the
    ones taken where the model had nothing to say.
    """
    flat = PairBias(
        pair="EURUSD",
        base="EUR",
        quote="USD",
        spread=0.10,
        direction=Direction.NEUTRAL,
        conviction=Conviction.NONE,
        asof=date(2026, 9, 21),
        base_score=0.05,
        quote_score=-0.05,
        agreement=0.10,
        tradeable=False,
        blockers=("no_edge",),
    )

    code, output, journal = add(monkeypatch, tmp_path, *taken(), engine=flat)

    record = only_record(journal)

    assert code == 0, output
    assert record.conviction is Conviction.NONE
    assert record.agreed_with_bias is False


# --- a run with nothing in it is refused, never recorded ----------------------


def test_an_empty_cache_is_refused_rather_than_recorded_as_no_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The quietest way this command can be wrong.

    The chain does not fail on an empty cache. Every currency scores zero,
    `build_pair_biases` still returns all 28 rows, and the record lands with
    both composites at zero, seven zeroed pillars each, a conviction of none
    and ``agreed_with_bias`` false on any real direction. It reads afterwards
    as an engine that had no opinion, not as a run that had no data, and the
    discipline split that counts trades taken against the engine is poisoned
    in the direction that blames the trader.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(), empty_cache=True)

    assert code == UNUSABLE, output
    assert "refresh" in output
    assert not journal.exists()


def test_a_run_that_scored_on_no_data_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Observations present, coverage zero. The same fabricated record.

    ``fbe score`` and ``fbe bias`` print their rows and exit 1, because the
    zeros are on screen with their reasons beside them. This command writes to
    an append-only file instead, so it writes nothing at all.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(), coverage=0.0)

    assert code == UNUSABLE, output
    assert not journal.exists()


def test_a_chain_that_cannot_build_its_rows_exits_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`build_pair_biases` raises `KeyError` for a leg it has no score for.

    Uncaught, that is a traceback where the command's `Raises:` section
    promises an exit code, and a trader reading it has no way to tell a broken
    install from a thin cache.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(), rows_raise=True)

    assert code == UNUSABLE, output
    assert "CurrencyScore" in output
    # The helper prints the repr of anything that is not a SystemExit, so this
    # is what separates a handled failure from one that reached the runner.
    # Both exit 1, and only one of them is the documented behaviour.
    assert "KeyError" not in output
    assert not journal.exists()


def test_a_journal_that_cannot_be_read_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Because an add that cannot read the book cannot tell a correction.

    Treating an unreadable file as a first entry would recompute the bias and
    overwrite the entry snapshot of whatever trade is already under that id.
    """
    journal = tmp_path / "trades.jsonl"
    journal.write_text("{not json\n", encoding="utf-8")

    code, output, _ = add(monkeypatch, tmp_path, *taken())

    assert code == UNUSABLE, output
    assert "could not be read" in output
    assert journal.read_text(encoding="utf-8") == "{not json\n"


# --- a correction carries the entry's view, it does not re-derive it ----------


def test_a_correction_keeps_the_bias_recorded_at_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The record `load` keeps has to be the one written at entry.

    `journal.load` keeps the last line per id, so the close is what survives.
    Recomputing the chain at close time makes ``conviction``, which
    `TradeRecord` calls the single most important field for evaluation, a
    reading of the cache three days after the trade. Macro series get revised
    and the normalisation is cross-sectional, so any other currency's newer
    data moves this pair's scores. The trade would then be bucketed under a
    conviction the engine never held when it was taken.
    """
    moved = replace(
        ENGINE,
        spread=0.44,
        direction=Direction.LONG,
        conviction=Conviction.LOW,
        base_score=0.22,
        quote_score=-0.22,
    )

    add(monkeypatch, tmp_path, *taken())
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(**{"--exit": "1.0791"}), engine=moved
    )

    record = only_record(journal)

    assert code == 0, output
    assert record.exit_price == pytest.approx(1.0791)
    assert record.conviction is Conviction.MEDIUM
    assert record.base_score == pytest.approx(ENGINE.base_score)
    assert record.quote_score == pytest.approx(ENGINE.quote_score)
    assert record.spread_score == pytest.approx(ENGINE.spread)
    assert record.agreed_with_bias is True
    assert dict(record.base_pillars) == pytest.approx(
        {
            name: pillar.score
            for name, pillar in pillars("EUR", ENGINE.base_score).items()
        }
    )
    assert "Engine at entry, carried: medium conviction, spread -2.31." in output


def test_a_correction_that_changes_the_side_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Because the record does not hold the engine's own side.

    Only whether the trade agreed with it. So a correction that swaps the side
    cannot say whether the new one agrees, and the two ways out are both worse
    than refusing: carrying the old flag writes a bool that is now wrong, and
    recomputing the chain answers with today's view of the pair, which is the
    thing the correction path exists to avoid.
    """
    add(monkeypatch, tmp_path, *taken())
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(**{"--direction": "long"})
    )

    record = only_record(journal)

    assert code == USAGE_ERROR, output
    assert record.direction is Direction.SHORT
    assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 1


# --- open, then closed, both lines on disk ------------------------------------


def test_omitting_the_exit_records_an_open_trade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    record = only_record(journal)

    assert record.exit_price is None
    assert record.closed_at is None


def test_a_second_add_closes_the_trade_and_keeps_both_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Append-only, and the reader keeps the last line per id.

    Rewriting the first line in place would be the one thing the storage
    format exists to prevent: a record that can be quietly edited after a bad
    week is not evidence about anyone's discipline.
    """
    add(monkeypatch, tmp_path, *taken())
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"--exit": "1.0791"}))

    records = load(path=journal)

    assert code == 0, output
    assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert len(records) == 1
    assert records[0].exit_price == pytest.approx(1.0791)
    assert records[0].closed_at is not None


# --- the R-multiple, measured against what the account actually risked --------


def test_the_r_multiple_is_measured_against_the_realised_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The intended risk is what the rule asked for, not what was taken.

    The lot step rounds the size down, so the money actually exposed is less
    than the rule asked for, and a review dividing by the intended figure
    reports an R-multiple the account never earned. Here the ladder asked for
    30.00 at MEDIUM and the account carried 26.60, so the two R-multiples are
    1.55 and 1.38 and only one of them happened.

    The figures are literals rather than the implementation's own formula
    rearranged. Restating the formula asserts that the record is
    self-consistent, which it is under a wrong sign, a wrong pip size and a
    wrong contract size alike.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        account_currency="USD",
    )

    record = only_record(journal)

    assert record.risk_amount == pytest.approx(RISK)
    assert record.risk_amount != pytest.approx(INTENDED)
    assert record.outcome_zar == pytest.approx(WIN), output
    assert record.r_multiple == pytest.approx(WIN / RISK, rel=1e-6)
    assert record.r_multiple != pytest.approx(WIN / INTENDED, rel=1e-6)
    assert record.risk_fraction == pytest.approx(RISK / 2000.0)


def test_a_winning_short_is_signed_by_the_direction_it_was_taken_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A short that fell made money, and the record has to say so.

    Subtracting the wrong way round writes a losing trade for a winning one,
    with a plausible magnitude and an inverted sign, and every statistic Phase
    6 computes from the file then reports the model backwards.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        account_currency="USD",
    )

    record = only_record(journal)

    assert record.outcome_zar == pytest.approx(+41.30), output
    assert record.r_multiple == pytest.approx(+1.5526, abs=1e-4)


def test_a_losing_short_is_signed_the_other_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same short, exited above the entry. 50 pips against, 35.00 lost."""
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0900"}),
        account_currency="USD",
    )

    record = only_record(journal)

    assert record.outcome_zar == pytest.approx(-35.00), output
    assert record.r_multiple == pytest.approx(-1.3158, abs=1e-4)


def test_a_winning_long_records_a_positive_outcome_and_a_positive_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the same arithmetic, and the one that pins the risk.

    A stop distance taken signed rather than absolute is equivalent on a short
    with the stop above the entry, which is every other money case here. On a
    long the stop sits below the entry, the risk comes out negative, and every
    R-multiple in the file flips sign while each one still looks like a number.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--direction": "long", "--stop": "1.0812", "--exit": "1.0900"}),
        account_currency="USD",
    )

    record = only_record(journal)

    assert record.risk_amount == pytest.approx(RISK), output
    assert record.outcome_zar == pytest.approx(+35.00)
    assert record.r_multiple == pytest.approx(+1.3158, abs=1e-4)


def test_the_units_come_from_the_broker_profile_in_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not from the 100,000 that happens to be the default.

    A literal contract size is invisible while the profile matches it and
    silently wrong the day the owner opens an account quoting in units of
    10,000, which is what this config describes.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        account_currency="USD",
        contract_size=10000.0,
    )

    record = only_record(journal)

    assert record.units == pytest.approx(700.0), output
    assert record.risk_amount == pytest.approx(2.66)
    assert record.outcome_zar == pytest.approx(4.13)


def test_a_jpy_pair_is_valued_in_jpy_pips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pip is 0.01 on a JPY cross and 0.0001 everywhere else.

    A pip size fixed at 0.0001 turns this 38 pip stop into 3,800 pips and the
    risk with it, which is a hundredfold error that still prints as a number
    and still round-trips through the file.
    """
    usdjpy = replace(
        ENGINE,
        pair="USDJPY",
        base="USD",
        quote="JPY",
        base_score=1.36,
        quote_score=-0.95,
    )

    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"pair": "USDJPY", "--entry": "150.50", "--stop": "150.88"}),
        engine=usdjpy,
        account_currency="JPY",
        account_balance=200000.0,
    )

    record = only_record(journal)

    assert "38.0 pip stop" in output
    assert record.risk_amount == pytest.approx(2660.0), output
    assert record.risk_fraction == pytest.approx(0.0133)
    # The ladder's figure moves with the balance and the currency, so a render
    # carrying a constant is visible here rather than only on the fixture it
    # was written against.
    assert "intended JPY 3000.00" in output


def test_an_open_trade_records_the_risk_but_no_outcome_and_no_r(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent rather than zero. A zero R is a real result and this is not one.

    Run on an account the position can be valued in, so the absence of the
    outcome is the trade being open and not the rate being missing. The risk
    is known the moment the stop is placed, which is the interesting half: it
    is what `check_limits` would count against the exposure caps while the
    trade is still running.
    """
    _, output, journal = add(monkeypatch, tmp_path, *taken(), account_currency="USD")

    record = only_record(journal)

    assert record.risk_amount == pytest.approx(RISK), output
    assert record.outcome_zar is None
    assert record.r_multiple is None


def test_money_that_cannot_be_valued_is_absent_on_every_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The owner's own account, and today the only case it ever takes.

    Nothing in the tree supplies a rate into ZAR, so `pip_value` refuses and
    the command has no honest figure for the risk, the outcome or the R. All
    three are recorded absent. A zero would be worse than a gap: the Phase 6
    revenge rule compares one trade's ``risk_amount`` against another's, and a
    book of zeros ranks every unpriced trade as the smallest one taken.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"--exit": "1.0791"}))

    record = only_record(journal)

    assert code == 0, output
    assert record.account_currency == "ZAR"
    assert record.risk_amount is None
    assert record.risk_fraction is None
    assert record.outcome_zar is None
    assert record.r_multiple is None
    # The three fragments the worked example in ``docs/interfaces.md`` prints,
    # asserted here so the contract document stays true to the command. A
    # published example that does not reproduce is worse than no example.
    assert "Recorded EURUSD short, 0.07 lots, 38.0 pip stop." in output
    assert "No money figures: no rate from USD to ZAR" in output
    assert "Risk, outcome and R are recorded as absent" in output


# --- the trader's own labels round-trip ---------------------------------------


def test_the_labels_round_trip_through_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Setup, plan, note and tags, read back as they were written.

    Tags in particular: JSON has one sequence type, so a field written from a
    tuple and read back as a list compares unequal to itself.
    """
    _, _, journal = add(
        monkeypatch,
        tmp_path,
        *taken(),
        "--setup",
        "trendline-break-retest",
        "--broke-plan",
        "--note",
        "Chased the break instead of waiting for the retest.",
        "--tag",
        "london",
        "--tag",
        "news-day",
    )

    record = only_record(journal)

    assert record.setup == "trendline-break-retest"
    assert record.followed_plan is False
    assert record.notes == "Chased the break instead of waiting for the retest."
    assert record.tags == ("london", "news-day")


def test_following_the_plan_is_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """And it is a separate fact from agreeing with the engine.

    A trade can follow the plan and disagree with the bias, or the reverse,
    and the review reports the two as separate splits because one is a model
    problem and the other is a discipline problem.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(**{"--direction": "long"}))

    record = only_record(journal)

    assert record.followed_plan is True
    assert record.agreed_with_bias is False


# --- refusals, each writing nothing -------------------------------------------


@pytest.mark.parametrize("pair", ["EUR", "EURUSDX", "EURXYZ"])
def test_a_pair_the_universe_does_not_hold_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pair: str
) -> None:
    """And nothing is written. A half-written journal is worse than none."""
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"pair": pair}))

    # The message is asserted as well as the code, because the two refusals
    # this command can make differ only by exit code otherwise, and a pair
    # that reached the chain and produced no row would look the same.
    assert code == USAGE_ERROR, output
    assert pair in output
    assert not journal.exists()


@pytest.mark.parametrize("size", ["0", "-0.07"])
def test_a_size_that_is_not_positive_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, size: str
) -> None:
    """A position of zero or less is not a trade, and it divides by zero."""
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"--lots": size}))

    assert code == USAGE_ERROR, output
    assert "lots" in output.lower()
    assert not journal.exists()


def test_omitting_the_size_is_refused_rather_than_sized_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The decision, pinned so it reads as one rather than as an oversight.

    ``--lots`` has no default. The command could size the trade itself from
    the conviction ladder, and it deliberately does not: the journal records
    what the account held, and the ladder's figure is what it should have held.
    Recording the second as the first puts a position in the book that was
    never on it, and every money figure derived from it is then fiction.
    """
    arguments = taken()
    index = arguments.index("--lots")

    code, output, journal = add(
        monkeypatch, tmp_path, *arguments[:index], *arguments[index + 2 :]
    )

    assert code == USAGE_ERROR, output
    assert "lots" in output.lower()
    assert not journal.exists()


def test_a_stop_equal_to_the_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """There is no risk to divide by, so every R-multiple would be infinite."""
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{"--stop": "1.0850"}))

    assert code == USAGE_ERROR
    assert "stop" in output.lower()
    assert not journal.exists()


def test_a_neutral_direction_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neutral is the engine saying it has no side, not a side to hold.

    It is a member of `Direction` and the parser accepts it, so without this
    refusal it falls through to the short branch of the outcome arithmetic. A
    long position mistyped as neutral is then recorded with its profit and loss
    inverted at full magnitude, which is the worst shape a wrong number here
    can take: right size, wrong sign, and nothing on screen.
    """
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(**{"--direction": "neutral"})
    )

    assert code == USAGE_ERROR, output
    assert not journal.exists()


@pytest.mark.parametrize("option", ["--entry", "--stop", "--exit"])
def test_a_price_that_is_not_finite_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, option: str
) -> None:
    """A NaN compares unequal to itself, so it walks past the stop check.

    What it produces is a NaN outcome and a NaN R, and the journal refuses to
    serialise those, so the command dies with a traceback rather than the
    refusal its contract documents.
    """
    code, output, journal = add(monkeypatch, tmp_path, *taken(**{option: "nan"}))

    assert code == USAGE_ERROR, output
    assert not journal.exists()


def test_an_account_balance_of_zero_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`risk_fraction` divides by it, and `Config.validate` does not check it.

    Every other sizing path goes through `fbe.risk.position_size`, which
    refuses the input. This one derives the fraction itself, so it has to make
    the same refusal rather than dividing by zero at the last line.
    """
    code, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        account_currency="USD",
        account_balance=0.0,
    )

    assert code == UNUSABLE, output
    assert "balance" in output.lower()
    assert not journal.exists()


def test_the_close_time_is_the_one_the_caller_gave(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not the moment the trade was written up.

    The Phase 6 revenge rule measures from one trade's close to the next one's
    open, so a Friday close stamped on Monday hides the sequences it looks for
    and can manufacture new ones out of a batch of write-ups.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        "--closed",
        "2026-09-24 15:30",
    )

    record = only_record(journal)

    assert record.closed_at == datetime(2026, 9, 24, 15, 30, tzinfo=UTC), output


def test_a_direction_the_engine_does_not_recognise_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refused at the parser, because the option is typed as `Direction`.

    Widening it to a plain string moves the failure into `journal.append`,
    which raises rather than exiting, so the contract this pins is the
    annotation and not a check in the body.
    """
    code, _, journal = add(monkeypatch, tmp_path, *taken(**{"--direction": "sideways"}))

    assert code == USAGE_ERROR
    assert not journal.exists()


def test_a_failed_write_reaches_the_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trade that was taken and not recorded is the worst outcome here.

    Swallowing the error leaves the trader believing the journal has the trade
    in it, and the absence is discovered at the review, by which time the
    entry price and the reasoning are gone.
    """

    def refuse(record: TradeRecord, path: Path = tmp_path) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fbe.journal.append", refuse)

    code, output, _ = add(monkeypatch, tmp_path, *taken())

    assert code != 0
    assert "read-only file system" in output or "could not" in output.lower()


# --- what the trader is told at the desk --------------------------------------


def test_the_output_names_the_size_the_stop_and_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The alignment line is the command in one sentence.

    It is printed now because the alternative is finding out at the weekly
    review that the trade was taken against the engine, which is too late to
    be a decision and only in time to be a regret.
    """
    _, output, _ = add(monkeypatch, tmp_path, *taken(), account_currency="USD")

    assert "Recorded EURUSD short, 0.07 lots, 38.0 pip stop." in output
    assert "short, medium conviction, spread -2.31" in output
    assert "Aligned." in output


def test_the_output_names_the_intended_risk_beside_the_realised_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gap the R-multiple turns on, at the desk rather than in the spec.

    26.60 was taken where the ladder asked for 30.00, and a trader who only
    ever sees the second figure has no way to notice that the lot step is
    quietly holding the account under its own risk budget.
    """
    _, output, _ = add(
        monkeypatch,
        tmp_path,
        *taken(**{"--exit": "1.0791"}),
        account_currency="USD",
    )

    assert "USD +41.30, +1.55R" in output
    assert "realised risk of USD 26.60" in output
    assert "intended USD 30.00" in output


def test_a_trade_taken_against_the_engine_says_so_in_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Counted separately in review, so it is named when it is recorded."""
    _, output, _ = add(monkeypatch, tmp_path, *taken(**{"--direction": "long"}))

    assert "Against the bias." in output


# --- nothing touches the owner's own journal ----------------------------------


def test_the_command_writes_where_the_module_points_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read at call time rather than captured at import.

    The tests rely on it, and so does anyone relocating the data tree. A
    default bound at import cannot be pointed anywhere afterwards, which is
    how a test suite ends up appending to the owner's real book.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken())

    assert journal.exists()
    assert json.loads(journal.read_text(encoding="utf-8").strip())["pair"] == "EURUSD"


def test_the_record_copies_the_pillar_scores_for_both_legs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The snapshot is the part that cannot be reconstructed later.

    Macro series get revised and the weights may change, so the pillar values
    behind a recorded composite are only available on the day. Asserted
    against the values the chain produced for each leg, and against the legs
    being the right way round: the fixture gives EUR and USD different values
    for every pillar, so a record copying one leg twice fails rather than
    looking complete.
    """
    _, output, journal = add(monkeypatch, tmp_path, *taken())

    record = only_record(journal)
    expected_base = {
        name: pillar.score for name, pillar in pillars("EUR", ENGINE.base_score).items()
    }
    expected_quote = {
        name: pillar.score
        for name, pillar in pillars("USD", ENGINE.quote_score).items()
    }

    assert set(record.base_pillars) == set(PillarName), output
    assert dict(record.base_pillars) == pytest.approx(expected_base)
    assert dict(record.quote_pillars) == pytest.approx(expected_quote)
    assert dict(record.base_pillars) != pytest.approx(expected_quote)


def test_the_pillar_snapshot_is_empty_rather_than_seven_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A unit test, because the command cannot reach this today.

    `build_pair_biases` raises for a leg it has no score for, so the command
    exits before the snapshot is taken, which the test above pins. The branch
    is still the one that matters the day that changes: seven zeros read back
    as a currency the model scored at the middle of its band, and an empty map
    reads as the chain having had nothing. They are different facts.
    """
    assert cli_module._pillar_snapshot({}, "EUR") == {}
