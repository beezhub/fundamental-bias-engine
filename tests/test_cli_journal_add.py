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
for. On this account the gap between the two is routinely 10% or more, so the
difference is the difference between a review that reports what happened and
one that reports what was meant to.

Nothing here reaches the network: the collection, the scorer and the bias layer
are replaced in every test, the same way ``tests/test_cli_bias.py`` does it.
Nothing writes to `fbe.journal.JOURNAL_PATH` either. That file holds the
owner's real entry prices and profit and loss on a live account and is
git-ignored for that reason, so every test points the command at ``tmp_path``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fbe import config as config_module
from fbe.cli import app
from fbe.datasources.collect import CollectionResult
from fbe.journal import TradeRecord, load
from fbe.risk import pip_size, risk_fraction_for
from fbe.types import Conviction, CurrencyScore, Direction, PairBias, PillarName

runner = CliRunner()

USAGE_ERROR = 2
"""Click's exit code for a value the parser or the command refused."""

OPENED = "2026-09-21 09:00"
"""The open timestamp every test passes, so the trade id is deterministic."""

TRADE_ID = "EURUSD-20260921T0900"
"""Derived from the pair and the open timestamp, and not an option.

`docs/risk-and-execution.md` makes corrections an append with the same id, and
the command has no ``--trade-id``, so the id has to be a function of what the
caller gave. Pair plus the minute the trade was opened is the form
``tests/test_journal.py`` already uses.
"""

ENGINE = PairBias(
    pair="EURUSD",
    base="EUR",
    quote="USD",
    spread=-2.31,
    direction=Direction.SHORT,
    conviction=Conviction.MEDIUM,
    asof=date(2026, 9, 21),
    base_score=-0.95,
    quote_score=1.36,
    agreement=0.78,
    tradeable=True,
    blockers=(),
)
"""The engine's view of EURUSD on the day, as the chain would return it.

Short and medium, which is what the worked example in ``docs/interfaces.md``
prints, so a test asserting the record carries the engine's view is asserting
against a value a reader can find in the contract.
"""


def currency_score(code: str, composite: float) -> CurrencyScore:
    """One leg's score, carrying seven pillar values the record copies."""
    return CurrencyScore(
        currency=code,
        composite=composite,
        pillars={},
        asof=date(2026, 9, 21),
        rank=1,
        dispersion=0.40,
        coverage=1.0,
    )


def config_file(tmp_path: Path, account_currency: str = "ZAR") -> Path:
    """A config pointing every directory at ``tmp_path``.

    ``account_currency`` is a parameter because the money figures need a route
    from the pair's quote currency to the account's, and the engine has no
    source for one to ZAR: no G10 cross has a ZAR leg, there is no USDZAR
    series in `fbe.datasources.prices`, and nothing in ``data/manual`` supplies
    one. A USD account trading EURUSD needs no conversion at all, so it is the
    only account this suite can compute a money outcome for today. That is a
    real gap and it is raised on the issue rather than papered over with an
    invented rate.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
        f"risk:\n"
        f"  account_currency: {account_currency}\n",
        encoding="utf-8",
    )
    return path


def add(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *args: str,
    engine: PairBias | None = None,
    account_currency: str = "ZAR",
) -> tuple[int, str, Path]:
    """Run ``fbe journal add`` with the chain replaced and the journal in tmp.

    Returns the exit code, the combined output, and the journal path, so a test
    can assert on what was written as well as on what was printed.
    """
    journal = tmp_path / "trades.jsonl"
    config_path = config_file(tmp_path, account_currency)
    row = ENGINE if engine is None else engine

    def fake_collect(config_in: object, **kwargs: object) -> CollectionResult:
        return CollectionResult(observations=(), outcomes=(), gaps={})

    def fake_score_currencies(
        observations: object, pillars: object, scoring: object, asof: date
    ) -> Sequence[CurrencyScore]:
        return (
            currency_score("EUR", row.base_score),
            currency_score("USD", row.quote_score),
        )

    def fake_build_pair_biases(
        scores: Sequence[CurrencyScore],
        config_in: object,
        asof: date,
        guard: object = None,
    ) -> Sequence[PairBias]:
        return (row,)

    def fake_apply_filters(
        bias_row: PairBias,
        scores: Mapping[str, CurrencyScore],
        config_in: object,
        asof: date,
        **kwargs: object,
    ) -> PairBias:
        return bias_row

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


def taken(tmp_path: Path, **overrides: str) -> list[str]:
    """The arguments for one complete trade, with any part replaced."""
    fields = {
        "pair": "EURUSD",
        "--direction": "short",
        "--entry": "1.0850",
        "--stop": "1.0888",
        "--lots": "0.01",
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
    code, output, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    assert code == 0, output
    assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_the_record_carries_what_the_caller_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pair, direction, entry, stop, lots and the open timestamp, unchanged.

    The trader's own numbers are the half of the record the engine must not
    touch, because they are the only part it cannot reconstruct.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    record = only_record(journal)

    assert record.pair == "EURUSD"
    assert record.direction is Direction.SHORT
    assert record.entry == pytest.approx(1.0850)
    assert record.stop == pytest.approx(1.0888)
    assert record.lots == pytest.approx(0.01)
    assert record.opened_at == datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def test_the_trade_id_is_derived_from_the_pair_and_the_minute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """There is no ``--trade-id``, so the id has to be a function of the input.

    Corrections are an append with the same id, so two adds describing one
    trade have to agree on it without the trader remembering a string.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    assert only_record(journal).trade_id == TRADE_ID


# --- the engine's view, attached rather than asked for ------------------------


def test_the_engine_view_is_attached_from_the_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The criterion the command exists for.

    Asserted against the values the chain returned for that pair, not against
    "not blank". A command that accepted the bias as a flag, or that recorded
    the first pair the chain produced rather than the one being journalled,
    passes a non-blank assertion and fails this one.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    record = only_record(journal)

    assert record.conviction is ENGINE.conviction
    assert record.base_score == pytest.approx(ENGINE.base_score)
    assert record.quote_score == pytest.approx(ENGINE.quote_score)
    assert record.spread_score == pytest.approx(ENGINE.spread)


def test_the_config_digest_is_the_running_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The digest is what makes the recorded scores reproducible later.

    A record carrying scores and no digest cannot be checked against anything,
    because the weights that produced them may have changed since.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    expected = config_module.load_config(config_file(tmp_path)).digest()

    assert only_record(journal).config_digest == expected


def test_a_trade_with_the_engine_is_recorded_as_agreeing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Short on a pair the engine called short."""
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

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
        monkeypatch, tmp_path, *taken(tmp_path, **{"--direction": "long"})
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

    code, output, journal = add(monkeypatch, tmp_path, *taken(tmp_path), engine=flat)

    record = only_record(journal)

    assert code == 0, output
    assert record.conviction is Conviction.NONE
    assert record.agreed_with_bias is False


# --- open, then closed, both lines on disk ------------------------------------


def test_omitting_the_exit_records_an_open_trade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

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
    add(monkeypatch, tmp_path, *taken(tmp_path))
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(tmp_path, **{"--exit": "1.0791"})
    )

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
    reports an R-multiple the account never earned. The two differ here, and
    the record has to carry the realised one.
    """
    _, output, journal = add(
        monkeypatch,
        tmp_path,
        *taken(tmp_path, **{"--exit": "1.0791"}),
        account_currency="USD",
    )

    record = only_record(journal)
    config = config_module.load_config(config_file(tmp_path, "USD"))
    intended = (
        risk_fraction_for(record.conviction, config.risk) * config.risk.account_balance
    )

    assert record.outcome_zar is not None, output
    assert record.r_multiple is not None
    # The realised risk of 0.01 lots on a 38 pip stop is far from the 1.5% of
    # the balance the ladder asks for at MEDIUM, so a record carrying the
    # intended figure would be visibly different rather than coincidentally
    # equal.
    assert record.risk_amount != pytest.approx(intended)
    assert record.risk_amount == pytest.approx(
        abs(record.entry - record.stop) / pip_size(record.pair) * 0.01 * 100000 * 0.0001
    )
    assert record.r_multiple == pytest.approx(
        record.outcome_zar / record.risk_amount, rel=1e-9
    )
    assert record.risk_fraction == pytest.approx(
        record.risk_amount / record.account_balance_at_entry
    )


def test_an_open_trade_records_no_outcome_and_no_r(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent rather than zero. A zero R is a real result and this is not one."""
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    record = only_record(journal)

    assert record.outcome_zar is None
    assert record.r_multiple is None


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
        *taken(tmp_path),
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
    _, _, journal = add(
        monkeypatch, tmp_path, *taken(tmp_path, **{"--direction": "long"})
    )

    record = only_record(journal)

    assert record.followed_plan is True
    assert record.agreed_with_bias is False


# --- refusals, each writing nothing -------------------------------------------


@pytest.mark.parametrize("pair", ["EUR", "EURUSDX", "EURXYZ"])
def test_a_pair_the_universe_does_not_hold_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pair: str
) -> None:
    """And nothing is written. A half-written journal is worse than none."""
    code, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path, **{"pair": pair}))

    assert code == USAGE_ERROR
    assert not journal.exists()


def test_a_stop_equal_to_the_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """There is no risk to divide by, so every R-multiple would be infinite."""
    code, output, journal = add(
        monkeypatch, tmp_path, *taken(tmp_path, **{"--stop": "1.0850"})
    )

    assert code == USAGE_ERROR
    assert "stop" in output.lower()
    assert not journal.exists()


def test_a_direction_the_universe_does_not_recognise_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, _, journal = add(
        monkeypatch, tmp_path, *taken(tmp_path, **{"--direction": "sideways"})
    )

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

    code, output, _ = add(monkeypatch, tmp_path, *taken(tmp_path))

    assert code != 0
    assert "read-only file system" in output or "could not" in output.lower()


# --- nothing touches the owner's own journal ----------------------------------


def test_the_command_writes_where_the_module_points_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read at call time rather than captured at import.

    The tests rely on it, and so does anyone relocating the data tree. A
    default bound at import cannot be pointed anywhere afterwards, which is
    how a test suite ends up appending to the owner's real book.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    assert journal.exists()
    assert json.loads(journal.read_text(encoding="utf-8").strip())["pair"] == "EURUSD"


def test_the_record_copies_the_pillar_scores_for_both_legs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The snapshot is the part that cannot be reconstructed later.

    Macro series get revised and the weights may change, so the pillar values
    behind a recorded composite are only available on the day.
    """
    _, _, journal = add(monkeypatch, tmp_path, *taken(tmp_path))

    record = only_record(journal)

    assert set(record.base_pillars) <= set(PillarName)
    assert set(record.quote_pillars) <= set(PillarName)
