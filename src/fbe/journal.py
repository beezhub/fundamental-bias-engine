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
from datetime import UTC, date, datetime
from enum import Enum, StrEnum
from math import sqrt
from pathlib import Path
from statistics import NormalDist

from fbe.config import DATA_DIR
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


EVIDENCE_THRESHOLD_TRADES: int = 30
"""Closed trades in one conviction bucket before its figures are evidence.

At the plan's five trades a week a bucket needs months to reach this, which is
the point: `evaluate`'s docstring states that below thirty a difference in
expectancy is not worth acting on, and this is that sentence as a number a
`ConvictionStats` can be compared against.

Thirty is conventional rather than derived, and it is not a limit on anything.
It decides when a figure is worth believing, so it lives here and not in
`fbe.config.RiskConfig`, which holds the numbers that constrain trading. Nothing
refuses, blocks or resizes on it: the only effect of crossing it is that
`ConvictionStats.below_evidence_threshold` goes false and a renderer stops
printing the caveat.
"""

HIT_RATE_CONFIDENCE: float = 0.95
"""Confidence level for the interval around every reported hit rate.

The interval is a Wilson score interval, computed from this level rather than
from a stored multiplier, so the level is the only thing to change and the
arithmetic follows. Wilson rather than the normal approximation because the
approximation runs outside ``0..1`` on small samples and reports a zero-width
interval on a bucket that has not lost yet, which would read as certainty from
five trades.

95% is the convention and nothing here depends on it being that rather than 90%.
It is not a threshold: no decision in this repository branches on whether an
interval excludes a value.
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
        wins: Trades with ``r_multiple`` strictly above zero. ``trades`` is
            not necessarily ``wins`` plus the losers: a trade closed exactly at
            breakeven, which is what moving a stop to entry produces, is
            neither, and it is counted in ``trades`` and in neither average.
        hit_rate: ``wins / trades``, in ``0..1``.
        hit_rate_low: Lower end of the interval around ``hit_rate``, in
            ``0..1``, at `HIT_RATE_CONFIDENCE`. Carried beside the point
            estimate because on the sample this journal will have for months the
            two ends are far apart, and a point estimate alone invites a reader
            to act on a difference the record cannot see.
        hit_rate_high: Upper end of the same interval.
        below_evidence_threshold: True when ``trades`` is under
            `EVIDENCE_THRESHOLD_TRADES`, so the bucket is a record of what
            happened rather than evidence about what will. A flag rather than a
            sentence, because a renderer has to branch on a flag and can print
            prose that a reader skims past. Ruled on issue #263.
        expectancy_r: Mean ``r_multiple`` across the bucket. This, not hit rate,
            is the number that decides whether the bucket makes money: a 35% hit
            rate at 3R average win is a better business than 70% at 0.4R.
        avg_win_r: Mean ``r_multiple`` of winners, positive. ``None`` when the
            bucket holds no winner, which is a different fact from winners
            averaging zero and cannot be expressed by a float: a winner is an
            ``r_multiple`` above zero, so no average of winners can be 0.0 and
            putting one there would be a sentinel inside the value space.
        avg_loss_r: Mean ``r_multiple`` of losers, strictly negative. ``None``
            when the bucket has not lost yet, for the same reason.
        total_r: Sum of ``r_multiple``, the bucket's contribution to the account.
        max_drawdown_r: Deepest peak-to-trough fall of the bucket's cumulative
            R, in close order, as a non-negative magnitude. 2.5 means the curve
            fell 2.5R from its high. Zero is a reading and means the curve never
            fell, which is why this is not optional: a bucket with no drawdown
            and a bucket whose drawdown is unknown do not both occur here.

    """

    conviction: Conviction
    trades: int
    wins: int
    hit_rate: float
    hit_rate_low: float
    hit_rate_high: float
    below_evidence_threshold: bool
    expectancy_r: float
    avg_win_r: float | None
    avg_loss_r: float | None
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

    That caveat is carried in the data rather than left to a renderer to
    remember: every bucket reports `ConvictionStats.below_evidence_threshold`
    against `EVIDENCE_THRESHOLD_TRADES`, and every ``hit_rate`` arrives with the
    two ends of its interval. Two buckets whose intervals overlap have not been
    told apart yet, whatever their point estimates say, and saying so is the
    answer rather than the absence of one. Ruled on issue #263.

    Args:
        records: Journal records, typically from `load`. Open trades are ignored,
            and so are closed trades carrying no ``r_multiple``: a trade with no
            realised figure has nothing to contribute and counting it as a loss
            would be the plausible wrong answer.

    Returns:
        One `ConvictionStats` per conviction level present in the data. Levels
        with no closed trades are omitted rather than reported as zeros, so an
        empty bucket cannot be mistaken for a losing one. An empty sequence, or
        one holding only open trades, gives an empty mapping: that is the state
        of every fresh clone, because ``data/journal/`` is git-ignored.

        Every figure comes from the records passed in. This function opens no
        file, reads no config and consults no clock, so the same records always
        give the same answer and no stored number can leak into a thin bucket.

    """
    closed: dict[Conviction, list[tuple[datetime, float]]] = {}
    for record in records:
        if record.closed_at is None or record.r_multiple is None:
            continue
        closed.setdefault(record.conviction, []).append(
            (record.closed_at, record.r_multiple)
        )
    return {
        conviction: _bucket(conviction, outcomes)
        for conviction, outcomes in closed.items()
    }


def _bucket(
    conviction: Conviction,
    outcomes: Sequence[tuple[datetime, float]],
) -> ConvictionStats:
    """Reduce one conviction's closed outcomes to its figures.

    Args:
        conviction: The bucket being described.
        outcomes: ``(closed_at, r_multiple)`` for each closed trade in the
            bucket, in whatever order the caller held them, sorted here for the
            drawdown and nowhere else. Narrowed to concrete values by
            `evaluate` rather than filtered a second time here: the same
            exclusion written twice is one that can be deleted from one place
            and still appear to work, which is how a test stops being able to
            see it.

    Returns:
        The bucket's `ConvictionStats`. Every figure is in R multiples measured
        against realised risk, which is what `TradeRecord.r_multiple` holds, so
        nothing here divides money by anything.

    """
    multiples = [value for _, value in outcomes]
    wins = [value for value in multiples if value > 0.0]
    # Strictly below zero, because `ConvictionStats.wins` is documented as
    # strictly above it. A trade closed exactly at breakeven is neither, so it
    # counts in `trades`, `expectancy_r` and `total_r` and in neither average.
    # Folding it into the losers would make a bucket's average loss shallower
    # every time a stop was moved to entry, which is the opposite of what that
    # figure is read for.
    losses = [value for value in multiples if value < 0.0]
    trades = len(multiples)
    low, high = _hit_rate_interval(len(wins), trades)
    return ConvictionStats(
        conviction=conviction,
        trades=trades,
        wins=len(wins),
        hit_rate=len(wins) / trades,
        hit_rate_low=low,
        hit_rate_high=high,
        below_evidence_threshold=trades < EVIDENCE_THRESHOLD_TRADES,
        expectancy_r=sum(multiples) / trades,
        # None rather than 0.0 where a kind is absent. A winner is above zero,
        # so no average of winners can be 0.0, and writing one would put a
        # marker inside the value space that a consumer reads as a measurement.
        avg_win_r=sum(wins) / len(wins) if wins else None,
        avg_loss_r=sum(losses) / len(losses) if losses else None,
        total_r=sum(multiples),
        max_drawdown_r=_max_drawdown(_in_close_order(outcomes)),
    )


def _in_close_order(outcomes: Sequence[tuple[datetime, float]]) -> list[float]:
    """Return one bucket's R multiples in the order the trades closed.

    Args:
        outcomes: ``(closed_at, r_multiple)`` pairs in any order.

    Returns:
        The multiples, earliest close first. Order matters only to the
        drawdown, and it has to be close order rather than entry order: a
        drawdown is what the account balance did, and the balance moves when a
        trade closes.

        Sorted on the time alone, so two trades closing at the same instant keep
        the order the caller held them in. Sorting on the pair would order those
        by outcome, which would quietly report the gentlest drawdown available
        from the same set of trades.

    """
    return [value for _, value in sorted(outcomes, key=lambda row: row[0])]


def _max_drawdown(multiples: Sequence[float]) -> float:
    """Deepest fall of a cumulative R curve from its own running peak.

    Args:
        multiples: R multiples in close order.

    Returns:
        The largest peak-to-trough fall, as a non-negative magnitude in R. Zero
        when the curve never fell, which is a reading rather than an absence:
        a bucket whose every trade won genuinely has no drawdown.

        The running peak starts at zero rather than at the first point, so a
        bucket that opens with a loss reports that loss as a drawdown. Starting
        at the first point would report 0.0 for a bucket that went straight
        down, which is the flattering answer.

    """
    peak = 0.0
    cumulative = 0.0
    deepest = 0.0
    for value in multiples:
        cumulative += value
        peak = max(peak, cumulative)
        deepest = max(deepest, peak - cumulative)
    return deepest


def _hit_rate_interval(wins: int, trades: int) -> tuple[float, float]:
    """Wilson score interval for a hit rate, at `HIT_RATE_CONFIDENCE`.

    Args:
        wins: Winning trades in the bucket, zero or more.
        trades: Closed trades in the bucket, strictly positive. Buckets with no
            trades are omitted from `evaluate`'s mapping, so there is no
            division to guard here.

    Returns:
        ``(low, high)`` in ``0..1``, both ends inclusive of the achievable
        range and clamped to it, and to either side of ``wins / trades``. The
        interval is computed from the confidence level through the normal
        quantile rather than from a stored multiplier, so changing
        `HIT_RATE_CONFIDENCE` changes the answer and nothing else has to move.

        Wilson rather than the normal approximation, and the reason is the
        sample this journal will have for its first months. The approximation
        is ``p +/- z * sqrt(p(1-p)/n)``, which runs below zero or above one on
        small samples, and collapses to zero width when ``p`` is 0 or 1: five
        winners from five trades would report a hit rate of 100% with no
        uncertainty at all. Wilson stays inside the range and keeps width at
        both extremes, which is the honest answer to "the record has not seen a
        loss yet".

    """
    z = NormalDist().inv_cdf(1 - (1 - HIT_RATE_CONFIDENCE) / 2)
    proportion = wins / trades
    denominator = 1 + z * z / trades
    centre = (proportion + z * z / (2 * trades)) / denominator
    half = (z / denominator) * sqrt(
        proportion * (1 - proportion) / trades + z * z / (4 * trades * trades)
    )
    # Clamped, and only floating point error is being clamped: Wilson is
    # analytically inside 0..1 and astride the point estimate at every
    # (wins, trades). The arithmetic is not. At zero wins of five the lower end
    # evaluates to about -5.6e-17, at zero of thirteen to about +1.4e-17, which
    # is an interval that excludes the hit rate it describes, and at every win
    # the upper end reaches 1.0000000000000002. A hit rate of 0.0% with a lower
    # bound above it is the kind of number a reader has to explain away.
    return (
        min(proportion, max(0.0, centre - half)),
        max(proportion, min(1.0, centre + half)),
    )


def discipline_flags(records: Sequence[TradeRecord]) -> Sequence[DisciplineFlag]:
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
        records: Journal records, typically from `load`, sorted or not.

    Returns:
        Flags in chronological order. An empty sequence means a clean run, which
        on a real trading record is worth noting in the review rather than
        assuming.

    """
    raise NotImplementedError(
        "fbe.journal.discipline_flags is scaffolded; see docs/roadmap.md Phase 6"
    )
