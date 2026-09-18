"""The module status table in ``CLAUDE.md``, checked against the modules.

``CLAUDE.md`` offers this table as the answer to "what is implemented and what is
not", and it is the first thing a contributor or an unattended run reads about
this repository. It said `src/fbe/config.py` was "Implemented apart from
`load_config()`" for some weeks after `load_config` landed.

The cost is small and real. A run looking for work reads the row, concludes there
is an entry point to write, and spends a claim discovering there is not. The
larger cost is to the table: `CLAUDE.md` presents it as authoritative and then
offers a way around it, "How to tell without checking this table: a stub raises
`NotImplementedError`". A row that is wrong where the bypass is right teaches the
reader to stop consulting the table, and the table is the only place recording
that `types.py` and `universe.py` are finished and load-bearing.

So this checks the table the way the bypass does, by reading the stub markers.
A row claiming a module is implemented must name modules with no scaffolded
callable in them; a row claiming work is in progress must name at least one that
has. That makes the table falsifiable rather than a note someone has to remember
to update, which is what let it drift in the first place.

The abstract methods in `datasources/base.py` carry a bare ``raise
NotImplementedError`` and are deliberately excluded, per ``CLAUDE.md``: reaching
one means a subclass is incomplete, not that a phase is unfinished. Only the
scaffold marker counts here, which is the same rule `tests/test_stubs.py`
enforces from the other side.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO / "CLAUDE.md"

SCAFFOLD = "is scaffolded;"
"""The stub marker, per ``CLAUDE.md``. A bare ``raise NotImplementedError`` on an
abstract method is not one and is not counted."""

ROW = re.compile(r"^\|\s*(`[^|]+`)\s*\|\s*([^|]+?)\s*\|$", re.MULTILINE)
PATH = re.compile(r"`([^`]+)`")


def _table() -> list[tuple[str, str]]:
    """Every data row of the module status table, as (modules cell, status).

    Bounded to the section so a table elsewhere in the document cannot be read
    as this one. The header and separator rows are dropped by requiring the
    first cell to be backticked, which the header's "Module" is not.
    """
    body = CLAUDE_MD.read_text()
    start = body.index("## What is implemented and what is not")
    section = body[start : body.index("\n## ", start + 1)]
    return [(modules, status) for modules, status in ROW.findall(section)]


def _references(cell: str) -> list[tuple[str, list[Path]]]:
    """One row's backticked references, each with the files it resolves to.

    Grouped per reference rather than flattened, because the two kinds carry
    different claims. A glob claims something about a directory taken as a
    whole; a named file claims something about that file. Flattening them loses
    the distinction, and losing it is what let a finished module sit in an
    unfinished row, which is the defect one row along from this issue's own.

    Every reference is written from the repository root rather than as a bare
    filename, which is what lets this resolve them without guessing a parent
    directory from a neighbouring token.
    """
    resolved: list[tuple[str, list[Path]]] = []
    for reference in PATH.findall(cell):
        if reference.endswith("*"):
            resolved.append((reference, sorted(REPO.glob(f"{reference[:-1]}*.py"))))
        else:
            resolved.append((reference, [REPO / reference]))
    return resolved


def _paths(cell: str) -> list[Path]:
    """Every file a row names, flattened, for the checks that do not care how."""
    return [path for _, paths in _references(cell) for path in paths]


def _scaffolded(path: Path) -> bool:
    return SCAFFOLD in path.read_text()


def test_the_table_has_rows() -> None:
    """Guards every other test here from passing vacuously.

    A parse that silently matched nothing would make each sweep below an empty
    loop, and the table would be unchecked while the suite stayed green.
    """
    assert len(_table()) >= 4


def test_every_module_the_table_names_exists() -> None:
    """A row pointing at a path that is gone is as wrong as a stale status.

    `src/fbe/reasoning/` is the live example of why this is worth asserting:
    `CLAUDE.md` refers to it elsewhere and it does not exist yet, so a row
    naming it would be describing the status of nothing.
    """
    for modules, _ in _table():
        for path in _paths(modules):
            assert path.exists(), f"{modules} names {path.relative_to(REPO)}"


@pytest.mark.parametrize("modules,status", _table())
def test_each_row_matches_the_modules_it_names(modules: str, status: str) -> None:
    """The table, checked the way its own bypass tells a reader to check.

    "Implemented" means no scaffolded callable in any module the row names.
    "In progress" is checked per reference rather than across the row: every
    file named on its own, and every glob taken as a whole, must still hold at
    least one stub. Checking the row as a whole was too weak, and a mutation
    proved it: moving the finished `scoring.py` back under an "In progress" row
    beside the unfinished pillars passed, which is precisely the wrong claim
    this issue is about.
    """
    references = _references(modules)
    assert references, modules

    if status.startswith("Implemented"):
        scaffolded = sorted(
            path.relative_to(REPO) for path in _paths(modules) if _scaffolded(path)
        )
        assert not scaffolded, (
            f"{modules} is listed as implemented but these still hold a "
            f"scaffolded callable: {scaffolded}"
        )
        return

    # Per reference, not across the row. A row naming four modules where three
    # are stubs and one is finished reads as "in progress" for all four, and
    # the finished one is then the stale claim this table exists to avoid.
    for reference, paths in references:
        assert any(_scaffolded(path) for path in paths), (
            f"{modules} is listed as '{status}' but {reference} is complete. "
            f"Move it to an implemented row."
        )


def test_the_lead_in_does_not_count_the_implemented_modules() -> None:
    """The sentence above the table made a claim that went stale with it.

    It read "Three modules are real", which was true when three were. A count
    in prose has to be corrected on the merge that changes it and nothing fails
    when it is not, so it is written without one.
    """
    body = CLAUDE_MD.read_text()
    start = body.index("## What is implemented and what is not")
    lead_in = body[start : body.index("| Module | Status |", start)]

    assert "Three modules are real" not in lead_in


def test_the_bypass_paragraph_is_unchanged() -> None:
    """Criterion 4. It is already correct and #20 owns the stub convention."""
    body = CLAUDE_MD.read_text()

    assert "**How to tell without checking this table:**" in body
    assert "fbe.scoring.composite is scaffolded; see docs/roadmap.md Phase 2" in body


def test_nothing_in_claude_md_calls_load_config_unimplemented() -> None:
    """Criterion 2, over the whole document rather than the one row.

    The row is the known instance. A second mention elsewhere would leave the
    document contradicting itself after the row is fixed, which is the state
    this issue reports.
    """
    body = " ".join(CLAUDE_MD.read_text().split())

    for phrase in (
        "apart from `load_config()`",
        "apart from `load_config`",
        "`load_config` is scaffolded",
    ):
        assert phrase not in body, phrase
