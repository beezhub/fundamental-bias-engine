"""Trade journal, and the only honest way to find out whether the model works.

The trading plan asks for a journal and a post-market review. This module
serves that requirement and one other that matters just as much: it is the
engine's evaluation harness. A fundamental bias model produces a number for a
pair, the owner trades it or does not, and weeks later something happened. With
no record of what the model actually said at the moment of entry, there is no
way to ever answer whether the model said anything useful. The weights would
stay wherever they were first guessed, forever.

That is why every record carries a snapshot of the bias at entry, not a
reference to a report that may since have been regenerated. Composite scores
for both legs, the spread between them, the conviction, the seven pillar scores
per leg, and the config digest that produced them. Reconstructing that later is
impossible: the underlying macro series get revised, the cross-sectional
normalisation depends on the rest of the universe on that day, and the weights
may have changed. A snapshot at entry is the only version that is true.

Storage is JSONL under ``data/journal/``, one JSON object per line, append-only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path

from fbe.config import DATA_DIR, RiskConfig
from fbe.types import Conviction, Direction, PillarName

__all__ = [
    "JOURNAL_DIR",
    "JOURNAL_PATH",
    "REVENGE_WINDOW_MINUTES",
    "OVERTRADING_TRADES_PER_WEEK",
    "DATETIME_FIELDS",
    "ENUM_FIELDS",
    "PILLAR_FIELDS",
    "TUPLE_FIELDS",
    "BlackoutCheck",
    "TradeRecord",
    "ConvictionStats",
    "DisciplineFlag",
    "append",
    "load",
    "evaluate",
    "discipline_flags",
]


JOURNAL_DIR: Path = DATA_DIR / "journal"
"""Directory holding the journal files."""

JOURNAL_PATH: Path = JOURNAL_DIR / "trades.jsonl"
"""Canonical journal file: one JSON object per line, newest appended last.

JSONL rather than a spreadsheet, for four reasons that all bite on a real
trading record. Append-only writes mean a crash or a half-finished entry
damages one line, not the file. There is no cell to fat-finger and no formula
to break, so a record cannot silently change after the fact, which matters when
the file is evidence about the owner's own discipline. It diffs and versions
cleanly, so the history of the record is itself auditable. And it holds nested
structure natively, which the bias snapshot needs: fourteen pillar scores per
trade flatten into a spreadsheet badly and read back worse.

A spreadsheet is still the right place to LOOK at this data. Export to CSV for
that. It is the wrong place to store it.
"""


REVENGE_WINDOW_MINUTES: int = 60
"""Minutes after a losing trade closes within which a new entry is flagged.

Not from the plan, which says only to avoid entering immediately after a loss.
One hour is a working definition on 1h and 4h charts: a genuinely new setup on
those timeframes takes at least one candle to form, so an entry inside the hour
is very unlikely to be a fresh signal and quite likely to be a reaction. Tune it
once there are enough trades to see the owner's own pattern.
"""

OVERTRADING_TRADES_PER_WEEK: int = 5
"""Entries per week above which the run is flagged as overtrading.

Also a convention rather than a plan rule. The plan says be selective and wait
for high-probability setups. Across 28 G10 crosses on 1h and 4h charts, with a
fundamental bias filter and a news blackout on top, five qualifying setups in a
week is already generous. Consistently exceeding it means the criteria have
loosened, not that the market got better.
"""


class BlackoutCheck(StrEnum):
    """What the calendar guard managed to say before a trade was entered.

    Three states rather than the boolean this replaced, because that boolean
    collapsed the middle one into the other two and the middle one is the only
    one worth counting. `docs/decisions/0002-representing-not-known.md` rule 1
    is the general form: absence is a value in the contract, never a value
    inside the normal range that a reader could mistake for a reading.

    The states, and what each one asks of a later reader:

        `CLEAR`: the guard ran, reached the moment, and found nothing in the
        blackout window. The entry was clean on the calendar.

        `UNKNOWN`: the guard ran and could not see, most often a failed fetch
        or a cached week that does not reach the run date, and the trade was
        taken anyway. This is the override proposal #2 asks to be counted: if
        it is most of the runs on which the state fired, the guard has been
        converted into a prompt and the fail-open policy on #24 needs revisiting.

        `NOT_RUN`: no guard was consulted. An offline run, or an entry recorded
        outside the engine. Not a judgement about the calendar at all, and the
        default, because a record that says nothing about the calendar must not
        read as one that says the calendar was clear.

    A trade forced through a blackout the guard could *see* is a fourth fact
    and is not here. `fbe.cli.size` is still scaffolded, so nothing can record
    it yet, and inventing the value before the command that writes it exists
    would put a member in this vocabulary that nothing ever sets. Issue #45
    scopes the unknown case; the visible-blackout override belongs with the
    command.
    """

    CLEAR = "clear"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One completed or open trade, with the model's view at entry attached.

    The record is written once at entry with the exit fields empty, then
    rewritten at exit. Because the file is append-only, the exit is a second
    line carrying the same ``trade_id``; readers keep the last line per id. That
    keeps the write path a pure append and leaves the intermediate state visible
    in the history, which is useful when reviewing a trade that was managed
    badly rather than sized badly.

    Attributes:
        trade_id: Stable identifier, unique per trade. Suggested form is
            ``{pair}-{opened_at:%Y%m%dT%H%M}``.
        pair: Six-character pair, market convention.
        direction: Long or short, expressed on the base currency, matching
            `PairBias`.
        opened_at: Entry fill time, timezone-aware UTC.
        closed_at: Exit fill time, or ``None`` while the trade is open.
        entry: Fill price on entry. The actual fill, not the intended level; the
            difference between the two is slippage and is worth knowing.
        exit_price: Fill price on exit, or ``None`` while open.
        stop: Stop price as originally placed, per the plan, just beyond the
            opposite side of the channel or the key trendline.
        target: Take-profit level as originally planned.
        units: Position size in base-currency units.
        lots: Same size in the broker's lots.
        risk_amount: Money actually at risk in the account currency if the
            stop fills, or ``None`` when the position could not be valued in
            it. Populate this from ``PositionSize.realised_risk_amount``, never
            from ``PositionSize.risk_amount``, which is the intended figure
            before the lot size was rounded down. The two differ on almost
            every trade at this account size, routinely by 10% or more, and the
            realised one is what was on the book. Should land within 1-2% of
            ``account_balance_at_entry``.

            ``None`` rather than ``0.0`` because the two are read the same way
            by anything that compares sizes and mean opposite things: a trade
            that risked nothing, and a trade whose risk nobody could price. It
            is absent whenever `fbe.risk.pip_value` cannot reach the account
            currency from the pair's quote currency, which today is every trade
            on the owner's ZAR account, since nothing supplies a rate into ZAR.
            Read by `discipline_flags`, which skips the comparison rather than
            treating an unpriced trade as a small one.
        risk_fraction: ``risk_amount / account_balance_at_entry``, using the
            realised figure above. Stored explicitly so a breach of the band is
            visible without arithmetic, and ``None`` exactly when
            ``risk_amount`` is.
        account_balance_at_entry: Balance the size was derived from.
        account_currency: Denomination of every money figure here, ``"ZAR"``.
        outcome_zar: Realised profit or loss in the account currency. Negative
            for a loss. ``None`` while open, and ``None`` when ``risk_amount``
            is, for the reason given there.

            Gross of costs as `fbe.cli.journal_add` writes it today, not net.
            ``BrokerConfig.commission_per_lot`` and ``typical_spread_pips``
            both exist and neither is read, so the figure is the price move on
            the size traded. A Phase 6 reader must subtract costs itself rather
            than assume they are already out, and the field is named here
            rather than left to be discovered from a number that looks right.
        r_multiple: ``outcome_zar / risk_amount``, and therefore divided by the
            REALISED risk. The only comparable measure of a result across
            different position sizes and account balances: a +2R on a R2,000
            account and a +2R on a R20,000 account are the same trade well
            executed. Dividing by the intended risk instead inflates every
            R-multiple by the rounding ratio, and since these numbers are the
            sole input to `evaluate`, that error would flatter the model rather
            than the trader and would be invisible in the output. ``None`` while
            open.
        setup: Name of the technical setup taken, from the plan's own
            vocabulary, e.g. ``"channel_bounce"``, ``"trendline_break_retest"``,
            ``"double_bottom_neckline"``.
        timeframe: Chart the entry was executed on, ``"1h"`` or ``"4h"``.
        exit_reason: Which of the plan's five exit strategies ended the trade:
            ``"target"``, ``"stop"``, ``"trailing_stop"``, ``"partial"``,
            ``"structure_break"`` or ``"time_exit"``. Grouping outcomes by this
            field is how the owner finds out whether the time-based exit is
            saving money or cutting winners short.
        base_score: Composite fundamental score of the base currency at entry.
        quote_score: Composite fundamental score of the quote currency at entry.
        spread_score: ``base_score - quote_score``, the pair's bias magnitude.
        conviction: The engine's conviction at entry. The single most important
            field for evaluation, because the whole conviction ladder rests on
            the claim that this field predicts something.
        base_pillars: The seven pillar scores for the base currency at entry.
        quote_pillars: The seven pillar scores for the quote currency at entry.
        config_digest: ``Config.digest()`` at entry. Ties the snapshot to the
            exact weights that produced it, so a re-weighting does not
            contaminate the history of trades taken under the old weights.
        agreed_with_bias: True when the trade direction matched
            ``PairBias.direction``. False marks a discretionary override, which
            is legitimate but must be counted separately, otherwise the model's
            record includes trades the model did not ask for.
        followed_plan: Whether the trade obeyed the owner's plan, which is a
            different question from whether it agreed with the engine and is
            kept in a separate field for that reason. A trade can follow the
            plan and disagree with the bias, or the reverse, and
            ``docs/interfaces.md`` reports the two as separate splits because
            they call for different fixes: one is a model problem and the other
            is a discipline problem.
        tags: Labels for grouping in review, as the trader wrote them. Free
            text on purpose: a controlled vocabulary here would be one more
            thing to maintain and the review groups on whatever is present.
        blackout_check: What the calendar guard was able to say before entry,
            as a `BlackoutCheck`. Three states rather than a boolean, because
            "consulted and the window was clear" and "consulted and blind, and
            the trade was taken anyway" are different facts and only the second
            is an override worth counting.
        broker: Broker name, so a change of execution venue is visible in the
            record when spreads and fills change with it.
        notes: Free text from the post-market review. The plan asks for the
            reasons behind winners and losers, and this is where they go.

    """

    trade_id: str
    pair: str
    direction: Direction
    opened_at: datetime
    entry: float
    stop: float
    units: float
    lots: float
    risk_amount: float | None
    risk_fraction: float | None
    account_balance_at_entry: float
    conviction: Conviction
    base_score: float
    quote_score: float
    spread_score: float
    config_digest: str
    account_currency: str = "ZAR"
    target: float | None = None
    closed_at: datetime | None = None
    exit_price: float | None = None
    outcome_zar: float | None = None
    r_multiple: float | None = None
    setup: str = ""
    timeframe: str = "4h"
    exit_reason: str = ""
    base_pillars: Mapping[PillarName, float] = field(default_factory=dict)
    quote_pillars: Mapping[PillarName, float] = field(default_factory=dict)
    agreed_with_bias: bool = True
    followed_plan: bool = True
    blackout_check: BlackoutCheck = BlackoutCheck.NOT_RUN
    broker: str = ""
    notes: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConvictionStats:
    """Performance of every trade taken at one conviction level.

    Attributes:
        conviction: The bucket these figures describe.
        trades: Number of closed trades in the bucket.
        wins: Trades with ``r_multiple`` above zero.
        hit_rate: ``wins / trades``, in ``0..1``.
        expectancy_r: Mean ``r_multiple`` across the bucket. This, not hit rate,
            is the number that decides whether the bucket makes money: a 35% hit
            rate at 3R average win is a better business than 70% at 0.4R.
        avg_win_r: Mean ``r_multiple`` of winners.
        avg_loss_r: Mean ``r_multiple`` of losers, negative.
        total_r: Sum of ``r_multiple``, the bucket's contribution to the account.
        max_drawdown_r: Deepest peak-to-trough run of the bucket's cumulative R.

    """

    conviction: Conviction
    trades: int
    wins: int
    hit_rate: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    total_r: float
    max_drawdown_r: float


@dataclass(frozen=True, slots=True)
class DisciplineFlag:
    """One instance of a behaviour the trading plan warns against.

    Attributes:
        kind: ``"revenge"``, ``"overtrading"`` or ``"against_bias"``.
        trade_id: The trade that triggered the flag, or the first trade of the
            period for a period-level flag such as overtrading.
        occurred_at: When the flagged behaviour happened.
        detail: Human-readable explanation naming the specific numbers, e.g.
            "entered GBPUSD 22 minutes after a -1.0R loss on EURUSD".

    """

    kind: str
    trade_id: str
    occurred_at: datetime
    detail: str


def _ends_mid_line(path: Path) -> bool:
    """Whether the file exists, holds bytes, and does not end in a newline.

    Args:
        path: Journal file, which may not exist.

    Returns:
        True when the last write was torn. An absent or empty file is False:
        there is nothing to separate the next record from.

    Note:
        Read as the last byte rather than by loading the file, so the cost does
        not grow with the history. A torn tail is the only state this cannot
        tell apart from a deliberate one, and there is no deliberate one:
        `append` is the only writer and it always terminates its line.

    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) != b"\n"


def _encode(value: object) -> object:
    """Turn one field value into something `json.dumps` accepts.

    Args:
        value: A `TradeRecord` field value.

    Returns:
        The JSON-ready form: a datetime as ISO 8601 in UTC with its offset
        written out, an enum as its value, a mapping keyed by its keys' string
        values, anything else unchanged.

    Raises:
        ValueError: If a datetime is naive. `TradeRecord` documents its
            timestamps as timezone-aware and `load` refuses a naive one on
            read, so writing one would produce a line this module cannot read
            back: the trade would be recorded and then lost.

    Note:
        Converted to UTC before being written, which is what `TradeRecord`
        means by "timezone-aware UTC" and what keeps every line in the file
        directly comparable. The instant is preserved exactly: an entry at
        09:00 in the owner's SAST is stored as 07:00+00:00 and reads back as
        the same moment. The offset is written out rather than dropped, because
        a naive timestamp cannot be compared against `load`'s ``since`` without
        guessing a zone.

    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(
                f"{value!r} is naive. Journal timestamps are instants, and a "
                f"record written without an offset cannot be ordered against "
                f"one written from another zone."
            )
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return value


def _checked(record: TradeRecord) -> None:
    """Refuse a record carrying a value `load` would later reject.

    Args:
        record: The trade about to be written.

    Raises:
        ValueError: If an enum field holds something outside its enum, or a
            pillar map is keyed by something that is not a `PillarName`.

    Note:
        `TradeRecord` is a frozen dataclass and validates nothing at runtime,
        so a plain string reaches here from any caller that is not statically
        typed: a CLI flag, a hand-built dict, a YAML entry form. Written, it
        succeeds, and the failure surfaces on the next read, by which point the
        line is in the file and `load` refuses the whole journal rather than
        one row, so every record written before it is unreadable too.

        This is the rule `_encode` already applies to a naive timestamp,
        applied to the other two things `_from_line` rejects. Checking on write
        keeps the failure next to the trade that caused it.

        The pillar keys are tested for being coercible to `PillarName` rather
        than for being members already, because `load` coerces them and a bare
        ``"monetary"`` round trips correctly. What must be refused is a key
        that is not a pillar at all, such as ``"momentum"``, which writes
        cleanly and then makes the file unreadable.

        One related loss is not preventable here and is worth knowing about.
        `PillarName` is a `StrEnum`, so a member and its equal string hash
        equal and cannot coexist as separate keys: ``{PillarName.MONETARY:
        1.0, "monetary": 2.0}`` is already a one-entry dict before this
        function is reached. Nothing in this module can see the second value,
        let alone keep it.

    """
    for name, enum_type in ENUM_FIELDS.items():
        value = getattr(record, name)
        if not isinstance(value, enum_type):
            raise ValueError(
                f"{name} is {value!r}, not a {enum_type.__name__}. Writing it "
                f"would produce a line load refuses, which makes every record "
                f"already in the file unreadable too."
            )
    for name in PILLAR_FIELDS:
        for key in getattr(record, name):
            try:
                PillarName(key)
            except ValueError as error:
                raise ValueError(
                    f"{name} is keyed by {key!r}, which is not a pillar name. "
                    f"Written, it produces a line load refuses, which makes "
                    f"every record already in the file unreadable too."
                ) from error


def _as_payload(record: TradeRecord) -> dict[str, object]:
    """Flatten a record into the object written as one JSON line.

    Args:
        record: The trade to serialise.

    Returns:
        Every field of `TradeRecord`, keyed by field name.

    Note:
        Driven from ``dataclasses.fields`` rather than from a list of names
        written out here. A hand-written list is a second copy of the record's
        shape, and when the two disagree the new field is simply not persisted:
        no error, no missing key on read because the default fills it, and the
        loss is invisible until Phase 6 asks a question the data cannot answer.

    """
    return {item.name: _encode(getattr(record, item.name)) for item in fields(record)}


def append(record: TradeRecord, path: Path = JOURNAL_PATH) -> None:
    """Append one record to the journal as a single JSON line.

    Creates the parent directory if it does not exist. Serialises enums by
    value, datetimes as ISO 8601 with an explicit UTC offset, and pillar maps as
    plain objects keyed by pillar name. Writes with a trailing newline, and
    writes a separating newline first when the file does not already end in
    one. The second half is what makes the first claim true after a torn write:
    without it the next record is concatenated onto the fragment and both are
    lost to a single unparseable line, rather than only the torn one.

    Never rewrites or truncates the file. Correcting a record means appending a
    corrected one with the same ``trade_id``; `load` keeps the last line per id.
    The superseded line stays in the file, which is the point: a journal the
    owner can quietly edit after a bad week is not a record of anything.

    Args:
        record: The trade to write.
        path: Journal file. Defaults to `JOURNAL_PATH`.

    Raises:
        OSError: If the file cannot be created or written. This propagates
            rather than being logged and swallowed, because a trade that was
            taken but not recorded is worse than a failed write the owner
            notices immediately.
        ValueError: If a timestamp on the record is naive. Writing one would
            produce a line `load` refuses, so the trade would be recorded and
            then unreadable.

    """
    # Serialised before anything touches the filesystem, so a record this
    # module refuses to write leaves no directory and no file behind.
    _checked(record)
    line = json.dumps(_as_payload(record), sort_keys=True, allow_nan=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened in append mode and never in "w", so no code path here can shorten
    # the file. A correction is a new line carrying the same trade_id, and the
    # superseded one stays where it is.
    with path.open("a", encoding="utf-8") as handle:
        if _ends_mid_line(path):
            # A previous process died between the record and its newline.
            # Without this, the next append is concatenated onto the fragment
            # and BOTH become one unparseable line: the torn record is gone,
            # which is expected, and this record is gone too, which is not.
            # `load` refuses the whole file rather than skipping a line, so
            # from that point every read raises and the only repair is editing
            # the file by hand, which is what this design exists to avoid.
            handle.write("\n")
        handle.write(line + "\n")
        # Flushed and synced because nothing else in the repository backs this
        # file up, per CLAUDE.md, and because this function's own OSError
        # rationale claims a trade taken but not recorded is the worse
        # outcome. Without the sync, append can return on a record that a
        # power loss then drops. One syscall on a file written a handful of
        # times a week.
        handle.flush()
        os.fsync(handle.fileno())


DATETIME_FIELDS: tuple[str, ...] = ("opened_at", "closed_at")
"""`TradeRecord` fields written as ISO 8601 and read back as datetimes."""

ENUM_FIELDS: Mapping[str, type[Enum]] = {
    "direction": Direction,
    "conviction": Conviction,
    "blackout_check": BlackoutCheck,
}
"""`TradeRecord` fields written as an enum value and read back as the member.

Read back as members rather than as their strings because the failure of
getting this wrong is silent: a ``direction`` left as ``"long"`` compares False
against `fbe.types.Direction.LONG`, so a caller filtering the book finds
nothing and reads an empty result as a period with no long trades in it.
"""

PILLAR_FIELDS: tuple[str, ...] = ("base_pillars", "quote_pillars")
"""`TradeRecord` fields holding a pillar map, keyed by `PillarName` value."""

TUPLE_FIELDS: tuple[str, ...] = ("tags",)
"""`TradeRecord` fields written as a JSON array and read back as a tuple.

JSON has one sequence type and `TradeRecord` is frozen, so a field left as the
list `json.loads` produces compares unequal to the tuple it was written from.
A round-trip test comparing field by field catches that; a caller comparing two
records does not, and neither does anything that only reads the values.
"""


def _cutoff(since: date | datetime | None) -> datetime | None:
    """Resolve ``since`` into the instant records are compared against.

    Args:
        since: A date, a datetime, or ``None``.

    Returns:
        The cutoff instant, or ``None`` to keep everything.

    Raises:
        ValueError: If a datetime is naive. There is no correct zone to assume:
            the owner trades in SAST, the records are stored in UTC, and
            guessing either way moves the cutoff by two hours without saying so.

    Note:
        ``datetime`` is a subclass of ``date``, so it is tested first. The other
        order reads every datetime as a date and silently widens an intraday
        cutoff to midnight on the same day.

    """
    if since is None:
        return None
    if isinstance(since, datetime):
        if since.tzinfo is None:
            raise ValueError(
                f"since must be timezone-aware, got the naive {since!r}. "
                f"Journal timestamps are instants, and assuming a zone here "
                f"would move the cutoff silently."
            )
        return since
    return datetime(since.year, since.month, since.day, tzinfo=UTC)


def _from_line(line: str, number: int, path: Path) -> TradeRecord:
    """Parse one JSON line into a record, refusing anything it cannot build.

    Args:
        line: The stripped line, known to be non-empty.
        number: 1-based line number, named in any error so the offending row
            can be found by eye in a file with hundreds of them.
        path: Journal file, named for the same reason.

    Returns:
        The record that line describes.

    Raises:
        ValueError: If the line is not a JSON object, omits a required field,
            carries an unknown one, holds an unknown enum value, or carries a
            naive ``opened_at``. Every one of these is reported rather than
            skipped: dropping a line understates the trade count and flatters
            every statistic Phase 6 computes from it, and a journal missing its
            losses reports a hit rate the model never earned.

    """
    try:
        payload = json.loads(line)
    except ValueError as error:
        raise ValueError(f"{path} line {number} is not valid JSON: {line!r}") from error
    if not isinstance(payload, dict):
        raise ValueError(
            f"{path} line {number} is a {type(payload).__name__}, not a JSON "
            f"object: {line!r}"
        )

    decoded = dict(payload)
    for name in DATETIME_FIELDS:
        raw = decoded.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            # Anything else reaches TradeRecord untouched and then fails on an
            # attribute lookup, raising AttributeError rather than the
            # ValueError this function documents. A caller wrapping load in
            # `except ValueError` to report an unknown book crashes instead.
            raise ValueError(
                f"{path} line {number} has a {name} of {raw!r}, which is a "
                f"{type(raw).__name__} rather than an ISO 8601 string"
            )
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as error:
            raise ValueError(
                f"{path} line {number} has a {name} that is not ISO 8601: {raw!r}"
            ) from error
        if moment.tzinfo is None:
            # Checked for every datetime field, not just opened_at. A naive
            # closed_at reads back fine and then raises TypeError inside the
            # Phase 6 revenge rule, which compares it against the next trade's
            # opened_at, far from the row that caused it.
            raise ValueError(
                f"{path} line {number} has a naive {name} {raw!r}. Journal "
                f"timestamps are instants, and a naive one cannot be ordered "
                f"against a record written from another zone."
            )
        decoded[name] = moment
    for name, enum_type in ENUM_FIELDS.items():
        raw = decoded.get(name)
        if raw is not None:
            try:
                decoded[name] = enum_type(raw)
            except ValueError as error:
                raise ValueError(
                    f"{path} line {number} has a {name} of {raw!r}, which is "
                    f"not one of {[member.value for member in enum_type]}"
                ) from error
    for name in TUPLE_FIELDS:
        raw = decoded.get(name)
        if isinstance(raw, list):
            decoded[name] = tuple(raw)
    for name in PILLAR_FIELDS:
        raw = decoded.get(name)
        if isinstance(raw, dict):
            try:
                decoded[name] = {PillarName(key): value for key, value in raw.items()}
            except ValueError as error:
                raise ValueError(
                    f"{path} line {number} has a {name} keyed by something "
                    f"that is not a pillar name: {sorted(raw)}"
                ) from error

    try:
        entry = TradeRecord(**decoded)
    except TypeError as error:
        raise ValueError(
            f"{path} line {number} does not describe a TradeRecord: {error}"
        ) from error

    return entry


def load(
    since: date | datetime | None = None,
    path: Path = JOURNAL_PATH,
) -> Sequence[TradeRecord]:
    """Read journal records, keeping only the latest line per ``trade_id``.

    Args:
        since: Only return trades with ``opened_at`` at or after this point. A
            ``date`` is interpreted as midnight UTC on that day. ``None``
            returns everything.
        path: Journal file. Defaults to `JOURNAL_PATH`.

    Returns:
        Records sorted by ``opened_at`` ascending. An absent file returns an
        empty sequence rather than raising, since a fresh install legitimately
        has no history.

    Raises:
        ValueError: If a line is present but unparseable, if a line carries a
            naive ``opened_at``, or if ``since`` is a naive datetime. A corrupt
            journal is reported, not skipped: silently dropping records would
            understate the trade count and flatter every statistic computed
            from it. ``since`` is checked before the file is opened, so the
            same bad argument behaves the same way on a machine with no
            journal as on one with a full history.

    """
    # Resolved before the file is looked at, so that a malformed ``since`` is
    # refused whether or not a journal exists. Validating it after the early
    # return would mean the same call raises on a machine that has traded and
    # is silently accepted on one that has not.
    cutoff = _cutoff(since)
    if not path.exists():
        return ()

    latest: dict[str, TradeRecord] = {}
    with path.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            entry = _from_line(stripped, number, path)
            # Last line per id wins. Insertion order is file order, so a
            # correction appended later replaces the value and keeps the
            # original's position, which the sort below then discards anyway.
            latest[entry.trade_id] = entry

    kept = [
        entry
        for entry in latest.values()
        if cutoff is None or entry.opened_at >= cutoff
    ]
    return tuple(sorted(kept, key=lambda entry: entry.opened_at))


def evaluate(records: Sequence[TradeRecord]) -> Mapping[Conviction, ConvictionStats]:
    """Group closed trades by conviction and report hit rate and expectancy.

    This is the analysis the whole journal exists for. Open trades and trades
    with no ``r_multiple`` are excluded. Every remaining trade lands in the
    bucket of its ``conviction`` at entry, and each bucket yields a
    `ConvictionStats`.

    What to look for, stated plainly: expectancy should rise monotonically from
    LOW through MEDIUM to HIGH. If it does not, the conviction model is wrong.
    Not unlucky, wrong. Conviction is derived from the score spread, the
    agreement between pillars and the freshness of the data, and the claim
    embedded in the risk ladder is that those three things predict outcomes well
    enough to justify risking twice as much on HIGH as on LOW. If HIGH does not
    outperform LOW over a reasonable sample, that claim is false, the ladder is
    actively harmful because it puts more money on the worse trades, and the
    correct response is to re-weight the pillars or flatten the ladder until the
    model earns the difference back. Flatten it by lowering
    ``RiskConfig.risk_per_trade_max`` toward ``risk_per_trade_min``, not by
    editing `risk.CONVICTION_BAND_POSITION`: the ladder interpolates across the
    configured band, so narrowing the band narrows every rung together and
    nothing ends up clamped.

    ``hit_rate`` by bucket carries a second load. It is the evidence for or
    against `risk.MIN_REWARD_TO_RISK`, whose shape assumes hit rate rises with
    conviction. A flat or inverted hit rate refutes that assumption, and the
    reward ladder should then collapse to a single minimum for every trade.

    Sample size caveat, which must be stated wherever these numbers are shown:
    at five trades a week, a bucket needs months to say anything. Thirty closed
    trades per bucket is the point at which a difference in expectancy starts
    being worth acting on. Below that the report is a record, not evidence, and
    reading it as evidence is how a working model gets tuned into a broken one.

    Args:
        records: Journal records, typically from `load`. Open trades are ignored.

    Returns:
        One `ConvictionStats` per conviction level present in the data. Levels
        with no closed trades are omitted rather than reported as zeros, so an
        empty bucket cannot be mistaken for a losing one.

    """
    raise NotImplementedError(
        "fbe.journal.evaluate is scaffolded; see docs/roadmap.md Phase 6"
    )


def discipline_flags(
    records: Sequence[TradeRecord], config: RiskConfig | None = None
) -> Sequence[DisciplineFlag]:
    """Detect the behaviours the trading plan explicitly warns against.

    The plan names these three and this function looks for exactly them. It
    reports, it does not block. Blocking would be a control the owner has to
    argue with mid-session; a flag is something they read in the weekly review,
    when the emotion that caused the behaviour is no longer present.

    Revenge trading:
        A new entry opened within `REVENGE_WINDOW_MINUTES` of a losing trade
        closing. Flagged more strongly when the new trade is in the same pair as
        the loss, or carries a larger ``risk_amount`` than the trade that lost,
        both of which are the classic shape of trying to win it straight back.
        Compare realised risk against realised risk, since that is what both
        records hold, and skip the size half of the comparison when either
        ``risk_amount`` is ``None``. An unpriced trade is not a small one, and
        reading it as zero would rank every priced trade above it and report
        the escalation backwards.

    Overtrading:
        More than `OVERTRADING_TRADES_PER_WEEK` entries in any rolling seven-day
        window, or more than ``RiskConfig.max_concurrent_positions`` positions
        open at once. The second is the harder rule: it is a limit `check_limits`
        should already have refused, so seeing it in the journal means the limit
        was bypassed.

    Trading against the engine's bias:
        Records with ``agreed_with_bias`` false. These are not automatically
        wrong. The engine supplies a fundamental lean over days to weeks and the
        owner may have a legitimate technical reason to fade it. They are
        flagged so they can be counted and evaluated as their own group. If the
        overrides consistently outperform the model, the model is the problem.
        If they consistently lose, the discipline is.

    Args:
        records: Journal records, typically from `load`, sorted or not. They are
            read, never altered.
        config: Risk limits to read ``max_concurrent_positions`` from. Defaults
            to `RiskConfig()`, which is the desk's own limit. It is a parameter
            because a limit that cannot be moved cannot be tested, and because a
            journal written under one limit should be readable against it rather
            than against whatever the defaults say today.

    Returns:
        Flags in chronological order. An empty sequence means a clean run, which
        on a real trading record is worth noting in the review rather than
        assuming. Nothing here reports absence of data as a behaviour: too few
        trades to judge reads in the review as a finding about the trader.

    """
    limits = RiskConfig() if config is None else config
    ordered = sorted(records, key=lambda record: record.opened_at)
    found = [
        *_revenge_flags(ordered),
        *_weekly_flags(ordered),
        *_concurrent_flags(ordered, limits.max_concurrent_positions),
        *_against_bias_flags(ordered),
    ]
    return tuple(sorted(found, key=lambda flag: flag.occurred_at))


def _loss_closed_at(record: TradeRecord) -> datetime | None:
    """When this record closed, if it closed as a loss.

    Args:
        record: One journal record.

    Returns:
        The close time of a closed trade with a negative result, otherwise
        ``None``. An open position is not a loss whatever it is showing on the
        screen, a closed one with no result recorded is an unknown rather than a
        win, and a breakeven trade is neither: none of the three starts a
        revenge window, because the window is about a loss the owner has taken.
        The time rather than a flag, so the caller cannot ask when a trade that
        did not lose closed.

    Note:
        `r_multiple` is preferred over `outcome_zar` because it is the same
        number at any account size, and both carry the sign of the result:
        negative is a loss, in the account currency for `outcome_zar`.

    """
    if record.closed_at is None:
        return None
    if record.r_multiple is not None:
        return record.closed_at if record.r_multiple < 0.0 else None
    if record.outcome_zar is not None and record.outcome_zar < 0.0:
        return record.closed_at
    return None


def _revenge_flags(ordered: Sequence[TradeRecord]) -> list[DisciplineFlag]:
    """Find entries opened inside `REVENGE_WINDOW_MINUTES` of a loss closing.

    Args:
        ordered: Records sorted by open time.

    Returns:
        One flag per qualifying entry, against the most recent loss that closed
        inside the window, since that is the one being reacted to. The window is
        inclusive at both ends: an entry in the same second as the close is the
        behaviour rather than a coincidence, and one at exactly the boundary is
        what the constant says.

    Note:
        The size half of the comparison is skipped when either ``risk_amount``
        is ``None``, so the flag is still raised and simply says nothing about
        size. Money figures in the detail carry the record's account currency.

        A record is not excluded from its own search because it cannot match:
        a trade opens before it closes, so the gap from its own close to its
        own open is negative and falls outside the window at the lower end.

    """
    window = timedelta(minutes=REVENGE_WINDOW_MINUTES)
    flags: list[DisciplineFlag] = []
    for record in ordered:
        loss: TradeRecord | None = None
        lost_at: datetime | None = None
        for candidate in ordered:
            closed = _loss_closed_at(candidate)
            if closed is None:
                continue
            if not timedelta(0) <= record.opened_at - closed <= window:
                continue
            if lost_at is None or closed > lost_at:
                loss, lost_at = candidate, closed
        if loss is None or lost_at is None:
            continue
        minutes = int((record.opened_at - lost_at).total_seconds() // 60)
        result = "loss" if loss.r_multiple is None else f"{loss.r_multiple:.1f}R loss"
        detail = (
            f"entered {record.pair} {minutes} minutes after a {result} on {loss.pair}"
        )
        aggravations: list[str] = []
        if record.pair == loss.pair:
            aggravations.append("same pair")
        if (
            record.risk_amount is not None
            and loss.risk_amount is not None
            and record.risk_amount > loss.risk_amount
        ):
            aggravations.append(
                f"larger risk than the trade that lost, "
                f"{record.account_currency} {record.risk_amount:.2f} "
                f"against {record.account_currency} {loss.risk_amount:.2f}"
            )
        if aggravations:
            detail = f"{detail}, {' and '.join(aggravations)}"
        flags.append(
            DisciplineFlag(
                kind="revenge",
                trade_id=record.trade_id,
                occurred_at=record.opened_at,
                detail=detail,
            )
        )
    return flags


def _weekly_flags(ordered: Sequence[TradeRecord]) -> list[DisciplineFlag]:
    """Find rolling seven-day windows holding more entries than the week allows.

    Args:
        ordered: Records sorted by open time.

    Returns:
        One flag per breaching window, keyed on the entry that opens it, after
        which the entries inside it are not counted again. Reporting every
        overlapping window would turn one busy fortnight into a page of flags
        saying the same thing.

    Note:
        The window is rolling rather than a calendar week on purpose. Three
        trades late on a Sunday and three early the following Tuesday are six
        inside six days, and a calendar reading calls both weeks clean.

    """
    allowed = OVERTRADING_TRADES_PER_WEEK
    window = timedelta(days=7)
    flags: list[DisciplineFlag] = []
    index = 0
    while index < len(ordered):
        first = ordered[index]
        inside = [
            record
            for record in ordered[index:]
            if record.opened_at - first.opened_at <= window
        ]
        if len(inside) > allowed:
            flags.append(
                DisciplineFlag(
                    kind="overtrading",
                    trade_id=first.trade_id,
                    occurred_at=first.opened_at,
                    detail=(
                        f"{len(inside)} entries in the seven days from "
                        f"{first.opened_at:%Y-%m-%d %H:%M}, above the {allowed} "
                        f"a selective week is expected to hold"
                    ),
                )
            )
            index += len(inside)
        else:
            index += 1
    return flags


def _concurrent_flags(
    ordered: Sequence[TradeRecord], allowed: int
) -> list[DisciplineFlag]:
    """Find the moments when more positions were open at once than allowed.

    Args:
        ordered: Records sorted by open time.
        allowed: ``RiskConfig.max_concurrent_positions``.

    Returns:
        One flag per entry that pushed the count above the limit. This is the
        harder of the two overtrading readings and stays a separate flag from
        the weekly count: a busy week is a habit, while this is a limit that
        `check_limits` refuses before the trade is taken, so finding it in the
        journal means the refusal was bypassed or never asked for.

    Note:
        A position counts as open from its ``opened_at`` until its ``closed_at``,
        and a record with no ``closed_at`` is open from then on.

    """
    flags: list[DisciplineFlag] = []
    for record in ordered:
        moment = record.opened_at
        open_now = [
            other
            for other in ordered
            if other.opened_at <= moment
            and (other.closed_at is None or other.closed_at > moment)
        ]
        if len(open_now) <= allowed:
            continue
        flags.append(
            DisciplineFlag(
                kind="overtrading",
                trade_id=record.trade_id,
                occurred_at=moment,
                detail=(
                    f"{len(open_now)} positions open at once on entering "
                    f"{record.pair}, above the {allowed} in "
                    f"RiskConfig.max_concurrent_positions, which check_limits "
                    f"refuses before a trade is taken"
                ),
            )
        )
    return flags


def _against_bias_flags(ordered: Sequence[TradeRecord]) -> list[DisciplineFlag]:
    """Find the entries taken against the engine's lean.

    Args:
        ordered: Records sorted by open time.

    Returns:
        One flag per record with ``agreed_with_bias`` false.

    Note:
        The wording says what happened and stops there. Whether fading the lean
        helps or hurts is a question about a group of trades that nobody here
        has measured, and a flag that called each one an error would be
        answering it.

    """
    return [
        DisciplineFlag(
            kind="against_bias",
            trade_id=record.trade_id,
            occurred_at=record.opened_at,
            detail=(
                f"entered {record.pair} {record.direction.name.lower()} against "
                f"the engine's lean, flagged so the overrides can be counted "
                f"and read as their own group"
            ),
        )
        for record in ordered
        if not record.agreed_with_bias
    ]
