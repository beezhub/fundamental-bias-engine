"""Read the broker's position exports, for `fbe journal import`.

The owner places every trade by hand on an Exness Standard account, and the
platform exports open positions as CSV. This module turns one export into
positions the journal can record. It reads a file and returns values: it knows
the export's columns, the pair universe and the shared types, and nothing about
the journal, the bias or the network.

The export carries no stop loss and no take profit, which the platform shows on
screen but does not export, so a position here has neither. `fbe journal
import` asks the owner for them (#347).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fbe.types import Direction
from fbe.universe import ALL_PAIRS

__all__ = [
    "OPEN_POSITIONS_HEADER",
    "ExportFormatError",
    "ExportedPosition",
    "read_open_positions",
]

OPEN_POSITIONS_HEADER: tuple[str, ...] = (
    "ticket",
    "opening_time_utc",
    "type",
    "original_position_size",
    "symbol",
    "opening_price",
    "commission",
)
"""The open-positions export's header, as downloaded on 2026-10-06. The header
is the contract: a file whose header differs is refused rather than read, so a
renamed or reordered column can never be read as a price."""

SIDES: dict[str, Direction] = {"buy": Direction.LONG, "sell": Direction.SHORT}
"""The export's ``type`` values. Anything else, a pending order type for
instance, is refused: it is not a position."""


class ExportFormatError(ValueError):
    """The file is not an open-positions export this module can read.

    Its own type so a caller can refuse the whole file with the reason, before
    asking the owner anything, rather than catching a stray ``ValueError``.
    """


@dataclass(frozen=True, slots=True)
class ExportedPosition:
    """One open position as the broker exported it.

    Attributes:
        ticket: The broker's position ticket, unique per position. The journal
            uses it as the trade's ID.
        opened_at: Open time, aware UTC. The export's column is UTC with no
            offset written.
        direction: Long for ``buy``, short for ``sell``, on the base currency.
        lots: Position size in lots.
        pair: Six-letter pair in market convention, the account-type suffix
            removed (``EURAUDm`` is ``EURAUD``).
        entry: Opening price.

    """

    ticket: str
    opened_at: datetime
    direction: Direction
    lots: float
    pair: str
    entry: float


def read_open_positions(path: Path) -> tuple[ExportedPosition, ...]:
    """Read every position in an open-positions export.

    Args:
        path: The downloaded CSV.

    Returns:
        The positions, in file order.

    Raises:
        ExportFormatError: When the header is not `OPEN_POSITIONS_HEADER`,
            naming what differs, or when a row cannot be read, naming the row
            by its line number and the value that failed. The whole file is
            refused, so nothing is half imported.
        OSError: When the file cannot be opened.

    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ExportFormatError(f"{path.name} is empty")
    header = tuple(cell.strip() for cell in rows[0])
    if header != OPEN_POSITIONS_HEADER:
        missing = [name for name in OPEN_POSITIONS_HEADER if name not in header]
        extra = [name for name in header if name not in OPEN_POSITIONS_HEADER]
        raise ExportFormatError(
            f"{path.name} does not have the open-positions header. Missing: "
            f"{', '.join(missing) or 'none'}. Unexpected: "
            f"{', '.join(extra) or 'none'}. Expected "
            f"{','.join(OPEN_POSITIONS_HEADER)}."
        )
    return tuple(
        _position(dict(zip(OPEN_POSITIONS_HEADER, row, strict=False)), line)
        for line, row in enumerate(rows[1:], start=2)
        if any(cell.strip() for cell in row)
    )


def _position(row: dict[str, str], line: int) -> ExportedPosition:
    """Read one row, naming the row on any failure."""
    where = f"row {line}"
    ticket = row.get("ticket", "").strip()
    if not ticket:
        raise ExportFormatError(f"{where} has no ticket")
    side = row.get("type", "").strip().lower()
    if side not in SIDES:
        raise ExportFormatError(
            f"{where} has type {side!r}, which is neither buy nor sell"
        )
    return ExportedPosition(
        ticket=ticket,
        opened_at=_moment(row.get("opening_time_utc", ""), where),
        direction=SIDES[side],
        lots=_positive(row.get("original_position_size", ""), "size", where),
        pair=_pair(row.get("symbol", ""), where),
        entry=_positive(row.get("opening_price", ""), "price", where),
    )


def _pair(symbol: str, where: str) -> str:
    """Strip the account-type suffix and check the pair is in the universe."""
    text = symbol.strip()
    pair = text[:6].upper()
    suffix = text[6:]
    if pair not in ALL_PAIRS or (suffix and not suffix.isalpha()):
        raise ExportFormatError(
            f"{where} has symbol {text!r}, which is not one of the engine's "
            f"pairs (read as {pair})"
        )
    return pair


def _positive(raw: str, label: str, where: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise ExportFormatError(f"{where} has {label} {raw!r}, not a number") from error
    if not math.isfinite(value) or value <= 0.0:
        raise ExportFormatError(f"{where} has {label} {raw!r}, not a positive number")
    return value


def _moment(raw: str, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError as error:
        raise ExportFormatError(f"{where} has open time {raw!r}") from error
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
