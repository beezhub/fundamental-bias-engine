"""The journal's write and read path, which is the model's only evidence.

Phase 6 can evaluate nothing the journal did not record, and a bias
reconstructed after the fact is not the bias that was live at entry: the macro
series get revised, the cross-sectional normalisation depends on the rest of
the universe on the day, and the weights may have changed since. So the
failures worth forcing here are the quiet ones. A dropped line understates the
trade count and flatters every statistic computed from it. A rewritten file
turns a record of the owner's own discipline into something they can edit after
a bad week. An enum that comes back as a bare string filters to nothing and
reads as a journal with no trades of that kind in it.

Every test writes to ``tmp_path``. None touches `fbe.journal.JOURNAL_PATH`,
which on a real machine holds the owner's entry prices, sizes and realised
profit and loss, and which `CLAUDE.md` keeps out of git for that reason.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from enum import Enum
from inspect import signature
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

import pytest

from fbe.journal import (
    DATETIME_FIELDS,
    ENUM_FIELDS,
    JOURNAL_PATH,
    PILLAR_FIELDS,
    TradeRecord,
    append,
    load,
)
from fbe.types import Conviction, Direction, PillarName

SEVEN_PILLARS = {
    PillarName.MONETARY: 1.25,
    PillarName.INFLATION: -0.5,
    PillarName.GROWTH: 0.75,
    PillarName.EMPLOYMENT: 0.0,
    PillarName.EXTERNAL: -1.5,
    PillarName.POSITIONING: 0.25,
    PillarName.RISK: -0.125,
}
"""A full pillar map, every value distinct, so a transposition shows up."""


def record(
    trade_id: str = "EURUSD-20260921T0900",
    *,
    pair: str = "EURUSD",
    opened_at: datetime | None = None,
    direction: Direction = Direction.LONG,
    conviction: Conviction = Conviction.HIGH,
    **overrides: object,
) -> TradeRecord:
    """Build a record with every required field populated.

    Defaults are the plan's own account: R2,000, R40 at risk, which is the 2%
    a HIGH conviction call earns from `fbe.risk.risk_fraction_for`.
    """
    fields: dict[str, object] = {
        "trade_id": trade_id,
        "pair": pair,
        "direction": direction,
        "opened_at": opened_at or datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        "entry": 1.0850,
        "stop": 1.0825,
        "units": 1000.0,
        "lots": 0.01,
        "risk_amount": 40.00,
        "risk_fraction": 0.02,
        "account_balance_at_entry": 2000.0,
        "conviction": conviction,
        "base_score": 1.2,
        "quote_score": -0.4,
        "spread_score": 1.6,
        "config_digest": "a1b2c3d4e5f6",
    }
    fields.update(overrides)
    return TradeRecord(**fields)  # type: ignore[arg-type]


def lines(path: Path) -> list[str]:
    """Every line in the file, blank ones included, so a truncation shows."""
    return path.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------


def test_one_record_is_one_line_with_a_trailing_newline(tmp_path: Path) -> None:
    """The trailing newline is what lets the next append start cleanly.

    Without it, a process that died mid-line last time leaves the next record
    concatenated onto the previous one, and both are lost to a single
    unparseable line rather than one.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert len(lines(path)) == 1


def test_the_parent_directory_is_created_when_absent(tmp_path: Path) -> None:
    """A fresh clone has no `data/journal/` at all.

    `CLAUDE.md` keeps the directory out of git except for its `.gitkeep`, and
    the routine hosts clone `main` on every run, so the first write on a new
    machine is into a directory that does not exist. Raising there would lose
    the first trade recorded on that machine.
    """
    path = tmp_path / "deeply" / "nested" / "trades.jsonl"
    assert not path.parent.exists()
    append(record(), path)
    assert path.exists()
    assert len(lines(path)) == 1


def test_appending_never_rewrites_or_truncates(tmp_path: Path) -> None:
    """Three records and a correction leave four lines, not three.

    The superseded line staying on disk is the whole point. A journal that can
    be edited after a bad week is not a record of anything, and this is the
    assertion that stops a later implementation from "tidying" the file.
    """
    path = tmp_path / "trades.jsonl"
    for index in range(3):
        append(record(trade_id=f"EURUSD-{index}"), path)
    assert len(lines(path)) == 3

    append(record(trade_id="EURUSD-1", notes="corrected"), path)
    assert len(lines(path)) == 4


def test_an_existing_file_keeps_its_earlier_lines_byte_for_byte(
    tmp_path: Path,
) -> None:
    """Appending adds; it does not reformat what is already there.

    Asserted on the raw prefix rather than on a line count, because a rewrite
    that happened to produce the same number of lines would pass that.
    """
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="first"), path)
    before = path.read_text(encoding="utf-8")

    append(record(trade_id="second"), path)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before)


def test_a_write_failure_propagates_rather_than_being_swallowed(
    tmp_path: Path,
) -> None:
    """A trade taken but not recorded is worse than a write the owner sees fail.

    The parent path here is a file, so creating a directory at it fails. The
    error has to reach the caller: swallowing it loses the record of a real
    position while the caller believes it is journalled.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(OSError):
        append(record(), blocker / "trades.jsonl")


@pytest.mark.parametrize("function", [append, load])
def test_the_default_path_is_the_journal_constant(function: object) -> None:
    """Both signatures default to the canonical file, and no test may use it.

    Asserted rather than assumed, because every other test in this file passes
    an explicit `tmp_path`, so nothing else would notice if either default
    drifted. `load`'s is the half that fails quietly: reading the wrong file
    returns an empty book, which is indistinguishable from a period in which
    the owner took no trades.

    Read through `inspect.signature` rather than ``__defaults__`` so that
    making ``path`` keyword-only, which is a refactor and not a defect, does
    not fail this.
    """
    assert signature(function).parameters["path"].default is JOURNAL_PATH


# --------------------------------------------------------------------------
# Serialisation, asserted field by field
# --------------------------------------------------------------------------


def test_a_record_round_trips_field_by_field(tmp_path: Path) -> None:
    """Every field comes back equal, and equality is checked per field.

    Comparing two serialised blobs would pass on a pair of implementations that
    are consistently wrong in the same way, which is precisely what a
    round-trip test is supposed to rule out.
    """
    path = tmp_path / "trades.jsonl"
    original = record(
        closed_at=datetime(2026, 9, 22, 14, 30, tzinfo=UTC),
        exit_price=1.0925,
        target=1.0925,
        outcome_zar=120.0,
        r_multiple=3.0,
        setup="trendline_break_retest",
        timeframe="1h",
        exit_reason="target",
        base_pillars=dict(SEVEN_PILLARS),
        quote_pillars={name: -value for name, value in SEVEN_PILLARS.items()},
        agreed_with_bias=False,
        blackout_checked=True,
        broker="generic-retail-micro",
        notes="Retest held on the fourth touch.",
        # Deliberately not the "ZAR" default. Left at it, this field round
        # trips to an identical value even when it is never written at all, so
        # this test alone would not notice it being dropped from the payload.
        # It is also the denomination of every money figure on the record.
        account_currency="USD",
    )
    append(original, path)
    (loaded,) = load(path=path)

    for name in TradeRecord.__dataclass_fields__:
        assert getattr(loaded, name) == getattr(original, name), name


def test_enums_serialise_by_value_and_return_as_enums(tmp_path: Path) -> None:
    """On disk they are their values; in memory they are enum members again.

    The second half is what matters downstream. A `direction` that comes back
    as the string ``"long"`` compares False against `Direction.LONG`, so a
    caller filtering the book finds nothing and reads an empty result as a
    period with no long trades in it.
    """
    path = tmp_path / "trades.jsonl"
    append(record(direction=Direction.SHORT, conviction=Conviction.MEDIUM), path)

    payload = json.loads(lines(path)[0])
    assert payload["direction"] == "short"
    assert payload["conviction"] == "medium"

    (loaded,) = load(path=path)
    assert loaded.direction is Direction.SHORT
    assert loaded.conviction is Conviction.MEDIUM


def test_datetimes_carry_an_explicit_utc_offset(tmp_path: Path) -> None:
    """A naive timestamp cannot be compared against `since` without guessing.

    The offset is written out, so a reader on another machine in another zone
    reconstructs the same instant rather than the same wall clock.
    """
    path = tmp_path / "trades.jsonl"
    append(record(closed_at=datetime(2026, 9, 22, 14, 30, tzinfo=UTC)), path)

    payload = json.loads(lines(path)[0])
    assert payload["opened_at"] == "2026-09-21T09:00:00+00:00"
    assert payload["closed_at"] == "2026-09-22T14:30:00+00:00"

    (loaded,) = load(path=path)
    assert loaded.opened_at.tzinfo is not None
    assert loaded.opened_at.utcoffset() == timedelta(0)


def test_a_non_utc_timestamp_is_normalised_to_utc_and_stays_the_same_instant(
    tmp_path: Path,
) -> None:
    """The owner is in SAST, and the file is written in UTC.

    09:00 at +02:00 is 07:00 UTC. Both halves are asserted: the stored line is
    in UTC, which is what `TradeRecord` means by "timezone-aware UTC" and what
    keeps every line in the file directly comparable, and the instant is
    unchanged, which is what `since` and every ordering downstream compare.

    Equality alone would not show the normalisation, because two aware
    datetimes in different zones compare equal when they name the same moment.
    """
    sast = timezone(timedelta(hours=2))
    opened = datetime(2026, 9, 21, 9, 0, tzinfo=sast)
    path = tmp_path / "trades.jsonl"
    append(record(opened_at=opened), path)

    assert json.loads(lines(path)[0])["opened_at"] == "2026-09-21T07:00:00+00:00"

    (loaded,) = load(path=path)
    assert loaded.opened_at == opened
    assert loaded.opened_at.utcoffset() == timedelta(0)
    assert loaded.opened_at.hour == 7


def test_writing_a_naive_timestamp_raises_rather_than_producing_an_unreadable_line(
    tmp_path: Path,
) -> None:
    """`append` must not write a line `load` will refuse.

    Without this, a naive `opened_at` is written happily and the trade is only
    discovered to be unreadable the next time the journal is loaded, by which
    point the fill details are gone. Refusing at the write keeps the failure
    next to the trade it concerns.
    """
    path = tmp_path / "trades.jsonl"
    with pytest.raises(ValueError, match="naive"):
        append(record(opened_at=datetime(2026, 9, 21, 9, 0)), path)


def test_pillar_maps_are_plain_objects_keyed_by_pillar_name(tmp_path: Path) -> None:
    """Fourteen scores per trade, and the keys have to survive as pillar names.

    A map keyed by ordinal or by index reads back as a different pillar's score
    with no error anywhere, which would misattribute the model's view at entry
    on every trade in the file.
    """
    path = tmp_path / "trades.jsonl"
    append(record(base_pillars=dict(SEVEN_PILLARS)), path)

    payload = json.loads(lines(path)[0])
    assert payload["base_pillars"] == {
        "monetary": 1.25,
        "inflation": -0.5,
        "growth": 0.75,
        "employment": 0.0,
        "external": -1.5,
        "positioning": 0.25,
        "risk": -0.125,
    }

    (loaded,) = load(path=path)
    assert loaded.base_pillars[PillarName.EXTERNAL] == -1.5
    assert set(loaded.base_pillars) == set(SEVEN_PILLARS)


def test_the_two_pillar_maps_are_not_interchanged(tmp_path: Path) -> None:
    """Base and quote carry opposite signs here, so a swap is visible.

    A swap would invert the model's recorded view of both legs while leaving
    every other field correct, and nothing downstream could detect it.
    """
    path = tmp_path / "trades.jsonl"
    append(
        record(
            base_pillars=dict(SEVEN_PILLARS),
            quote_pillars={name: -value for name, value in SEVEN_PILLARS.items()},
        ),
        path,
    )
    (loaded,) = load(path=path)
    assert loaded.base_pillars[PillarName.MONETARY] == 1.25
    assert loaded.quote_pillars[PillarName.MONETARY] == -1.25


def test_the_config_digest_survives_the_round_trip(tmp_path: Path) -> None:
    """Without it an outcome cannot be tied to the weights that produced it.

    A re-weighting would otherwise contaminate the history of trades taken
    under the old weights, and Phase 6 would compare results across models it
    cannot tell apart.
    """
    path = tmp_path / "trades.jsonl"
    append(record(config_digest="deadbeefcafe"), path)
    (loaded,) = load(path=path)
    assert loaded.config_digest == "deadbeefcafe"


def test_an_optional_field_left_empty_returns_as_none(tmp_path: Path) -> None:
    """An open trade has no exit, and None is not 0.0.

    A zero `outcome_zar` on an open trade reads as a scratch, which Phase 6
    would count as a closed trade that made nothing.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    (loaded,) = load(path=path)
    assert loaded.closed_at is None
    assert loaded.exit_price is None
    assert loaded.outcome_zar is None
    assert loaded.r_multiple is None
    assert loaded.target is None


def test_every_field_needing_conversion_is_listed(tmp_path: Path) -> None:
    """The three decode tables have to cover every field of their kind.

    `_as_payload` is driven from ``dataclasses.fields``, so a new field is
    written out automatically. Reading it back is driven from these three
    tables, so a new datetime, enum or pillar map added to `TradeRecord` and
    not listed here comes back as the raw JSON type: a string where a datetime
    belongs, which then fails to compare against `since`, or a string where an
    enum belongs, which filters to nothing.

    The annotations are strings under ``from __future__ import annotations``,
    so they are resolved before being inspected, and each rule looks inside a
    union as well as at the bare type. An earlier version of this test checked
    the enum rule against the bare type only, which meant an optional field
    such as ``override_direction: Direction | None`` was invisible to it and to
    every other test in the file.
    """

    def unwrapped(hint: object) -> tuple[object, ...]:
        """The hint itself plus a union's members, never a mapping's parameters.

        ``Mapping[PillarName, float]`` carries an Enum in its args, so a rule
        that looked at every arg would classify the pillar maps as enum fields
        and demand that they appear in two tables at once.
        """
        if get_origin(hint) in (Union, UnionType):
            return (hint, *get_args(hint))
        return (hint,)

    hints = get_type_hints(TradeRecord)
    expected_datetimes = {
        name for name, hint in hints.items() if datetime in unwrapped(hint)
    }
    expected_enums = {
        name
        for name, hint in hints.items()
        if any(
            isinstance(candidate, type) and issubclass(candidate, Enum)
            for candidate in unwrapped(hint)
        )
    }
    expected_pillars = {
        name
        for name, hint in hints.items()
        if get_origin(hint) is Mapping and PillarName in get_args(hint)
    }

    assert set(DATETIME_FIELDS) == expected_datetimes
    assert set(ENUM_FIELDS) == expected_enums
    assert set(PILLAR_FIELDS) == expected_pillars


def test_every_field_survives_a_round_trip_without_being_listed_twice(
    tmp_path: Path,
) -> None:
    """The payload carries exactly the record's fields, no more and no fewer.

    An extra key makes `TradeRecord(**decoded)` raise on read, and a missing
    one is filled by its default, which is the silent half: the field simply
    stops being recorded and nothing says so until Phase 6 asks for it.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    payload = json.loads(lines(path)[0])
    assert set(payload) == set(TradeRecord.__dataclass_fields__)


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def test_an_absent_file_returns_an_empty_sequence(tmp_path: Path) -> None:
    """A fresh install legitimately has no history, and every routine host has none.

    They clone `main` on every run and `data/journal/` is git-ignored, so this
    is the normal case on those machines rather than an edge one. Raising would
    break every run that touches the journal.
    """
    assert list(load(path=tmp_path / "nothing-here.jsonl")) == []


def test_an_empty_file_returns_an_empty_sequence(tmp_path: Path) -> None:
    """Distinct from absent, and the same answer. A `.gitkeep` install has this."""
    path = tmp_path / "trades.jsonl"
    path.write_text("", encoding="utf-8")
    assert list(load(path=path)) == []


def test_the_last_line_per_trade_id_wins(tmp_path: Path) -> None:
    """The correction is what `load` returns, and the original stays on disk.

    Both halves are asserted. Returning the first line would make corrections
    invisible; deleting the superseded line would make the file editable.
    """
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="EURUSD-1", notes="first"), path)
    append(record(trade_id="EURUSD-1", notes="corrected", outcome_zar=120.0), path)

    (loaded,) = load(path=path)
    assert loaded.notes == "corrected"
    assert loaded.outcome_zar == 120.0
    assert len(lines(path)) == 2


def test_an_exit_line_supersedes_the_entry_line(tmp_path: Path) -> None:
    """The write path the module docstring describes, end to end.

    A record is written at entry with the exit fields empty and again at exit
    with the same id. What `load` returns is the closed trade.
    """
    path = tmp_path / "trades.jsonl"
    opened = record(trade_id="EURUSD-1")
    append(opened, path)
    append(
        record(
            trade_id="EURUSD-1",
            closed_at=datetime(2026, 9, 22, 14, 30, tzinfo=UTC),
            exit_price=1.0925,
            outcome_zar=120.0,
            r_multiple=3.0,
            exit_reason="target",
        ),
        path,
    )
    (loaded,) = load(path=path)
    assert loaded.closed_at is not None
    assert loaded.r_multiple == 3.0
    assert loaded.exit_reason == "target"


def test_records_come_back_sorted_by_opened_at(tmp_path: Path) -> None:
    """Written out of order, returned in order.

    The file is append-only and a correction is appended late, so write order
    is not chronological order and nothing downstream should have to know that.
    """
    path = tmp_path / "trades.jsonl"
    third = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    first = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    second = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    for index, moment in enumerate((third, first, second)):
        append(record(trade_id=f"T{index}", opened_at=moment), path)

    loaded = load(path=path)
    assert [entry.opened_at for entry in loaded] == [first, second, third]


def test_the_sort_survives_a_correction_appended_last(tmp_path: Path) -> None:
    """A late correction to an early trade keeps that trade's position.

    Sorting by file order rather than by `opened_at` would move the corrected
    trade to the end and misreport when it was taken.
    """
    path = tmp_path / "trades.jsonl"
    early = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    late = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    append(record(trade_id="early", opened_at=early), path)
    append(record(trade_id="late", opened_at=late), path)
    append(record(trade_id="early", opened_at=early, notes="fixed"), path)

    loaded = load(path=path)
    assert [entry.trade_id for entry in loaded] == ["early", "late"]
    assert loaded[0].notes == "fixed"


# --------------------------------------------------------------------------
# since
# --------------------------------------------------------------------------


def test_since_none_returns_everything(tmp_path: Path) -> None:
    path = tmp_path / "trades.jsonl"
    for day in (21, 22, 23):
        append(
            record(
                trade_id=f"T{day}",
                opened_at=datetime(2026, 9, day, 9, 0, tzinfo=UTC),
            ),
            path,
        )
    assert len(load(since=None, path=path)) == 3


def test_a_date_is_read_as_midnight_utc_on_that_day(tmp_path: Path) -> None:
    """Midnight UTC, not local midnight and not the end of the day.

    A trade opened at 00:30 UTC on the 22nd is inside `date(2026, 9, 22)`.
    Reading the date as the end of the day would silently drop it, and reading
    it in the owner's SAST would drop everything before 02:00 UTC.
    """
    path = tmp_path / "trades.jsonl"
    append(
        record(
            trade_id="just-before",
            opened_at=datetime(2026, 9, 21, 23, 59, tzinfo=UTC),
        ),
        path,
    )
    append(
        record(
            trade_id="just-after",
            opened_at=datetime(2026, 9, 22, 0, 30, tzinfo=UTC),
        ),
        path,
    )

    loaded = load(since=date(2026, 9, 22), path=path)
    assert [entry.trade_id for entry in loaded] == ["just-after"]


def test_a_datetime_is_read_as_itself(tmp_path: Path) -> None:
    """To the minute, so an intraday cutoff is honoured rather than widened."""
    path = tmp_path / "trades.jsonl"
    append(
        record(trade_id="before", opened_at=datetime(2026, 9, 22, 8, 59, tzinfo=UTC)),
        path,
    )
    append(
        record(trade_id="after", opened_at=datetime(2026, 9, 22, 9, 1, tzinfo=UTC)),
        path,
    )

    loaded = load(since=datetime(2026, 9, 22, 9, 0, tzinfo=UTC), path=path)
    assert [entry.trade_id for entry in loaded] == ["after"]


def test_since_is_inclusive_of_its_own_instant(tmp_path: Path) -> None:
    """ "At or after", per the docstring, so a trade exactly on the boundary counts.

    An exclusive comparison drops exactly one trade per query and the caller
    has no way to notice.
    """
    boundary = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="on-the-boundary", opened_at=boundary), path)
    assert len(load(since=boundary, path=path)) == 1


def test_since_compares_instants_across_zones(tmp_path: Path) -> None:
    """A cutoff given in SAST filters the same trades as its UTC equivalent.

    11:00 at +02:00 is 09:00 UTC. Comparing wall clocks rather than instants
    would move the cutoff by two hours on the owner's own machine.
    """
    sast = timezone(timedelta(hours=2))
    path = tmp_path / "trades.jsonl"
    append(
        record(trade_id="before", opened_at=datetime(2026, 9, 22, 8, 59, tzinfo=UTC)),
        path,
    )
    append(
        record(trade_id="after", opened_at=datetime(2026, 9, 22, 9, 1, tzinfo=UTC)),
        path,
    )

    loaded = load(since=datetime(2026, 9, 22, 11, 0, tzinfo=sast), path=path)
    assert [entry.trade_id for entry in loaded] == ["after"]


def test_since_is_applied_after_the_correction_is_resolved(tmp_path: Path) -> None:
    """The surviving line decides, not a superseded one.

    Filtering before resolving corrections would let a superseded line's
    `opened_at` admit or exclude a trade whose current record says otherwise.
    """
    path = tmp_path / "trades.jsonl"
    append(
        record(trade_id="T1", opened_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC)),
        path,
    )
    append(
        record(
            trade_id="T1",
            opened_at=datetime(2026, 9, 23, 9, 0, tzinfo=UTC),
            notes="entry time corrected",
        ),
        path,
    )

    assert len(load(since=date(2026, 9, 22), path=path)) == 1
    assert len(load(since=date(2026, 9, 24), path=path)) == 0


def test_a_correction_that_moves_the_entry_time_earlier_uses_the_new_time(
    tmp_path: Path,
) -> None:
    """The case that distinguishes filtering before and after the resolution.

    The test above corrects `opened_at` forward, and filtering first happens to
    give the right answer there for the wrong reason: the superseded line is
    dropped by the filter and the correction survives it. Correcting backwards
    separates them. The surviving record opens on the 20th, so a cutoff of the
    22nd returns nothing; filtering first drops the correction and returns the
    superseded line, which is a record the journal no longer says exists.
    """
    path = tmp_path / "trades.jsonl"
    append(
        record(trade_id="T1", opened_at=datetime(2026, 9, 23, 9, 0, tzinfo=UTC)),
        path,
    )
    append(
        record(
            trade_id="T1",
            opened_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
            notes="entry time corrected earlier",
        ),
        path,
    )
    assert load(since=date(2026, 9, 22), path=path) == ()
    assert len(load(since=date(2026, 9, 19), path=path)) == 1


# --------------------------------------------------------------------------
# A corrupt journal is reported, never skipped
# --------------------------------------------------------------------------


def test_an_unparseable_line_raises_and_names_it(tmp_path: Path) -> None:
    """Silently dropping a record flatters every statistic computed from the file.

    A journal missing its losses reports a hit rate the model never earned, and
    Phase 6 is the only thing that ever tests whether the weights work. The
    message names the offending line so it can be found by eye.
    """
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="good"), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")

    with pytest.raises(ValueError, match="not json at all"):
        load(path=path)


def test_a_line_missing_a_required_field_raises(tmp_path: Path) -> None:
    """Valid JSON is not a valid record, and the difference matters.

    A line that parses but lacks `risk_amount` cannot produce an R-multiple, so
    admitting it with a default would put a fabricated denominator under every
    result computed from that trade.
    """
    path = tmp_path / "trades.jsonl"
    payload = json.loads(json.dumps({"trade_id": "T1", "pair": "EURUSD"}))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load(path=path)


def test_a_line_with_an_unknown_enum_value_raises(tmp_path: Path) -> None:
    """A direction of "sideways" is not a trade this system took.

    Coercing it to a default would attribute the result to a direction the
    owner never traded, and `agreed_with_bias` would then be measured against
    the wrong side.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    payload = json.loads(lines(path)[0])
    payload["direction"] = "sideways"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load(path=path)


def test_a_naive_since_raises_rather_than_assuming_a_zone() -> None:
    """There is no correct zone to assume, and guessing moves the cutoff silently.

    The owner trades in SAST and the records are stored in UTC, so reading a
    naive cutoff either way shifts it by two hours. A weekly review asking for
    "since Monday" would then quietly include or exclude Monday's first trades.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        load(since=datetime(2026, 9, 22, 9, 0), path=Path("unused.jsonl"))


def test_a_naive_opened_at_on_disk_raises(tmp_path: Path) -> None:
    """A record without an offset cannot be ordered against one written elsewhere.

    `TradeRecord` documents `opened_at` as timezone-aware, but the file is
    hand-editable and nothing upstream of `load` enforces it. Admitting the
    line would raise `TypeError` later, on the comparison against `since`, far
    from the row that caused it.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    payload = json.loads(lines(path)[0])
    payload["opened_at"] = "2026-09-21T09:00:00"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="naive"):
        load(path=path)


def test_a_line_carrying_an_unknown_field_raises(tmp_path: Path) -> None:
    """An unrecognised key is a record this version cannot honestly read.

    It usually means the file was written by a later version carrying a field
    this one would silently drop. Dropping it is the same harm as dropping a
    line: the record loads, looks complete, and is missing something the writer
    thought mattered enough to store.
    """
    path = tmp_path / "trades.jsonl"
    append(record(), path)
    payload = json.loads(lines(path)[0])
    payload["slippage_pips"] = 0.4
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load(path=path)


def test_two_trades_filled_in_the_same_minute_keep_file_order(
    tmp_path: Path,
) -> None:
    """Ties are broken by the order they were written, which is entry order.

    Unspecified until now, and two fills in the same minute is a realistic
    book. Pinned so the sort cannot quietly become unstable.
    """
    same = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="first", opened_at=same), path)
    append(record(trade_id="second", opened_at=same), path)
    assert [entry.trade_id for entry in load(path=path)] == ["first", "second"]


def test_a_blank_line_inside_the_file_is_skipped_rather_than_reported(
    tmp_path: Path,
) -> None:
    """An empty or whitespace-only line is absence, not corruption.

    The realistic producer is a half-flushed write or a hand-edited file, and
    the line carries no record to lose. Reporting it would refuse to read a
    journal whose trades are all intact, which on a file the owner cannot
    regenerate is the more expensive failure.

    Whitespace-only is covered as well as empty, because the reader strips
    before testing and the two must not diverge.
    """
    path = tmp_path / "trades.jsonl"
    append(record(trade_id="T1"), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n   \n")
    append(record(trade_id="T2"), path)

    assert len(load(path=path)) == 2
    assert [entry.trade_id for entry in load(path=path)] == ["T1", "T2"]


# --------------------------------------------------------------------------
# Filtering, which is what the round trip has to make possible
# --------------------------------------------------------------------------


def mixed_book(path: Path) -> None:
    """Four trades spanning two pairs, both directions and three convictions."""
    append(
        record(
            trade_id="A",
            pair="EURUSD",
            direction=Direction.LONG,
            conviction=Conviction.HIGH,
            opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        ),
        path,
    )
    append(
        record(
            trade_id="B",
            pair="EURUSD",
            direction=Direction.SHORT,
            conviction=Conviction.LOW,
            opened_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        ),
        path,
    )
    append(
        record(
            trade_id="C",
            pair="USDJPY",
            direction=Direction.LONG,
            conviction=Conviction.MEDIUM,
            opened_at=datetime(2026, 9, 23, 9, 0, tzinfo=UTC),
        ),
        path,
    )
    append(
        record(
            trade_id="D",
            pair="USDJPY",
            direction=Direction.LONG,
            conviction=Conviction.HIGH,
            opened_at=datetime(2026, 9, 24, 9, 0, tzinfo=UTC),
        ),
        path,
    )


def test_records_can_be_filtered_by_pair(tmp_path: Path) -> None:
    """Phase 4's definition of done, and a real test of the round trip.

    `pair` is a plain string, so this one would survive almost any
    serialisation. It is here because the three filters together are the
    criterion, and because a pair returned with its case or order changed would
    fail it.
    """
    path = tmp_path / "trades.jsonl"
    mixed_book(path)
    loaded = load(path=path)
    assert [e.trade_id for e in loaded if e.pair == "EURUSD"] == ["A", "B"]
    assert [e.trade_id for e in loaded if e.pair == "USDJPY"] == ["C", "D"]


def test_records_can_be_filtered_by_direction(tmp_path: Path) -> None:
    """Filtering on the enum member, not on its string value.

    This is the test that fails if `direction` comes back as ``"long"`` rather
    than `Direction.LONG`, and the failure mode it guards is not an exception:
    the comparison simply matches nothing and the caller reads an empty result
    as a book with no long trades in it.
    """
    path = tmp_path / "trades.jsonl"
    mixed_book(path)
    loaded = load(path=path)
    longs = [e.trade_id for e in loaded if e.direction is Direction.LONG]
    assert longs == ["A", "C", "D"]
    assert [e.trade_id for e in loaded if e.direction is Direction.SHORT] == ["B"]


def test_records_can_be_filtered_by_conviction(tmp_path: Path) -> None:
    """The field the whole conviction ladder rests on being able to predict.

    Same identity comparison as direction, and the same silent failure if the
    enum does not survive: Phase 6 buckets every trade by this field, and a
    bucket that matches nothing reads as a conviction level never traded.
    """
    path = tmp_path / "trades.jsonl"
    mixed_book(path)
    loaded = load(path=path)
    assert [e.trade_id for e in loaded if e.conviction is Conviction.HIGH] == ["A", "D"]
    assert [e.trade_id for e in loaded if e.conviction is Conviction.LOW] == ["B"]


def test_the_three_filters_combine(tmp_path: Path) -> None:
    """One high-conviction long on USDJPY, which is trade D and nothing else."""
    path = tmp_path / "trades.jsonl"
    mixed_book(path)
    loaded = load(path=path)
    picked = [
        entry.trade_id
        for entry in loaded
        if entry.pair == "USDJPY"
        and entry.direction is Direction.LONG
        and entry.conviction is Conviction.HIGH
    ]
    assert picked == ["D"]


def test_filtering_composes_with_since(tmp_path: Path) -> None:
    """The two mechanisms are independent and both apply."""
    path = tmp_path / "trades.jsonl"
    mixed_book(path)
    loaded = load(since=date(2026, 9, 23), path=path)
    assert [e.trade_id for e in loaded if e.direction is Direction.LONG] == ["C", "D"]
