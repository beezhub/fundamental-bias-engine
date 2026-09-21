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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path

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
        risk_amount: Money actually at risk in ZAR if the stop fills. Populate
            this from ``PositionSize.realised_risk_amount``, never from
            ``PositionSize.risk_amount``, which is the intended figure before
            the lot size was rounded down. The two differ on almost every trade
            at this account size, routinely by 10% or more, and the realised one
            is what was on the book. Should land within 1-2% of
            ``account_balance_at_entry``.
        risk_fraction: ``risk_amount / account_balance_at_entry``, using the
            realised figure above. Stored explicitly so a breach of the band is
            visible without arithmetic.
        account_balance_at_entry: Balance the size was derived from.
        account_currency: Denomination of every money figure here, ``"ZAR"``.
        outcome_zar: Realised profit or loss in the account currency, net of
            spread and commission. Negative for a loss. ``None`` while open.
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
        blackout_checked: Whether the calendar guard was consulted before entry.
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
    risk_amount: float
    risk_fraction: float
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
    blackout_checked: bool = False
    broker: str = ""
    notes: str = ""


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


def _encode(value: object) -> object:
    """Turn one field value into something `json.dumps` accepts.

    Args:
        value: A `TradeRecord` field value.

    Returns:
        The JSON-ready form: a datetime as ISO 8601 carrying its offset, an
        enum as its value, a mapping keyed by its keys' string values, anything
        else unchanged.

    Note:
        The offset is written out rather than normalised to UTC and dropped,
        because a naive timestamp cannot be compared against `load`'s ``since``
        without guessing a zone, and the owner trades in SAST. What has to
        survive is the instant, not the wall clock.

    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return value


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
    plain objects keyed by pillar name. Writes with a trailing newline so the
    next append starts cleanly even if the process died mid-line last time.

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

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_as_payload(record), sort_keys=True)
    # Opened in append mode and never in "w", so no code path here can shorten
    # the file. A correction is a new line carrying the same trade_id, and the
    # superseded one stays where it is.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


DATETIME_FIELDS: tuple[str, ...] = ("opened_at", "closed_at")
"""`TradeRecord` fields written as ISO 8601 and read back as datetimes."""

ENUM_FIELDS: Mapping[str, type[Enum]] = {
    "direction": Direction,
    "conviction": Conviction,
}
"""`TradeRecord` fields written as an enum value and read back as the member.

Read back as members rather than as their strings because the failure of
getting this wrong is silent: a ``direction`` left as ``"long"`` compares False
against `fbe.types.Direction.LONG`, so a caller filtering the book finds
nothing and reads an empty result as a period with no long trades in it.
"""

PILLAR_FIELDS: tuple[str, ...] = ("base_pillars", "quote_pillars")
"""`TradeRecord` fields holding a pillar map, keyed by `PillarName` value."""


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
        if isinstance(raw, str):
            try:
                decoded[name] = datetime.fromisoformat(raw)
            except ValueError as error:
                raise ValueError(
                    f"{path} line {number} has a {name} that is not ISO 8601: {raw!r}"
                ) from error
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

    if entry.opened_at.tzinfo is None:
        raise ValueError(
            f"{path} line {number} has a naive opened_at {entry.opened_at!r}. "
            f"The record is an instant, and a naive one cannot be ordered "
            f"against records written from another zone."
        )
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
        ValueError: If a line is present but unparseable. A corrupt journal is
            reported, not skipped: silently dropping records would understate
            the trade count and flatter every statistic computed from it.

    """
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

    cutoff = _cutoff(since)
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
        records hold.

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
