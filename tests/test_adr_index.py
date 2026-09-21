"""The decision record index, checked against the directory it indexes.

`CLAUDE.md` sends a reader to `docs/decisions/` to find out whether a question is
already settled. `docs/decisions/README.md` is the table they read first. For
some weeks it listed 0001 to 0004 and 0008 while 0005, 0006, 0007, 0009 and 0010
sat on branches that never reached `main`, and nothing looked wrong: the
numbering in the table was gapped but sequential, and every file it named was
there. Issue #151.

A file on an unmerged branch is not detectable from `main` by any test. What is
detectable is the README disagreeing with the directory, in either direction.
That is the symptom that would have surfaced the first stranded record rather
than the fifth, so this pins it. Numbering is checked too, because a gap in the
sequence is the other way a missing record hides.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
README = DECISIONS / "README.md"

ROW = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \| (.+?) \| (\w+) \|$", re.MULTILINE)
"""One indexed record: number, file, decision, status."""

FILENAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
"""A record's filename: four digits, a hyphen, a kebab-case slug."""


def _indexed() -> dict[str, str]:
    """Return ``{filename: number}`` for every row of the README table."""
    return {file: number for number, file, _, _ in ROW.findall(README.read_text())}


def _on_disk() -> dict[str, str]:
    """Return ``{filename: number}`` for every record file in the directory."""
    found: dict[str, str] = {}
    for path in DECISIONS.iterdir():
        match = FILENAME.match(path.name)
        if match:
            found[path.name] = match.group(1)
    return found


def test_every_record_on_disk_is_indexed() -> None:
    """A record the README does not list is one nobody will find."""
    missing = set(_on_disk()) - set(_indexed())

    assert not missing, sorted(missing)


def test_every_indexed_record_exists() -> None:
    """A row pointing at a file that is not there is a broken promise, and it
    is what a record merged without its file would look like."""
    dangling = set(_indexed()) - set(_on_disk())

    assert not dangling, sorted(dangling)


def test_the_index_number_matches_the_filename() -> None:
    """The number in the link text and the number in the filename agree."""
    for file, number in _indexed().items():
        assert file.startswith(f"{number}-"), (number, file)


def test_the_records_are_numbered_without_gaps() -> None:
    """Numbers run 0001, 0002, ... with nothing skipped.

    A gap is how the five stranded records hid: 0008 sat beside 0004 and the
    table still read as sequential to a glance. A record is never renumbered
    and never deleted, so the sequence has no legitimate holes.
    """
    numbers = sorted(int(number) for number in _on_disk().values())

    assert numbers == list(range(1, len(numbers) + 1)), numbers


def test_every_record_file_follows_the_naming_rule() -> None:
    """Anything in the directory other than the README is a numbered record."""
    stray = [
        path.name
        for path in DECISIONS.iterdir()
        if path.name != "README.md" and not FILENAME.match(path.name)
    ]

    assert not stray, stray
