"""The `data/` tree in ``CLAUDE.md``, checked against the repository it describes.

``CLAUDE.md`` is the document a contributor reads first, and its `data/` section
is where the tracking status of each directory is stated. It listed three
directories. There are four: `src/fbe/journal.py` writes to `data/journal/`,
`.gitignore` excludes its contents, and `data/journal/.gitkeep` is tracked.

The omission matters because of what the same section's closing sentence does. It
names the journal as one of three durable homes for information that cannot be
reproduced, having just listed the other two in the tree with their tracking
status. A reader following that sentence had no way to learn from this document
that the destination it points at is the only one of the three not in version
control, and the cost lands at Phase 6, which is the first point at which anyone
can honestly say whether the model works.

The ruling on issue #72 is to document it rather than track it. The journal holds
the owner's entry prices, sizes and realised profit and loss on a real account;
`reports/` holds the model's opinion. The first is not publishable and this
repository is on GitHub. The asymmetry is correct and what was missing is that it
was nowhere written down, so it read as an oversight and invited being "fixed".

Two kinds of assertion live here. The documentation checks read ``CLAUDE.md``.
The behaviour checks shell out to ``git check-ignore`` and ``git ls-files``,
because the criterion is that the ignore rules are unchanged, and asserting that
against the text of ``.gitignore`` would be asserting against a restatement of
the thing rather than the thing.

Nothing here reaches the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO / "CLAUDE.md"

JOURNAL_FILE = "data/journal/trades.jsonl"
JOURNAL_KEEP = "data/journal/.gitkeep"


def _data_section() -> str:
    """The `data/` section of ``CLAUDE.md``, collapsed to one line.

    Collapsed because the prose is hard-wrapped, so a phrase that reads as one
    sentence is split across lines in the file and a naive search misses it.
    """
    body = CLAUDE_MD.read_text()
    start = body.index("## The `data/` directory")
    rest = body[start:]
    end = rest.find("\n## ", 1)
    return " ".join((rest if end == -1 else rest[:end]).split())


def _tree_block() -> str:
    """Just the fenced tree, so a directory named only in prose does not count.

    The criterion is that the tree lists the directory. Prose mentioning it
    elsewhere in the section is not the same thing, and was already true before
    this change.
    """
    section = CLAUDE_MD.read_text()
    start = section.index("## The `data/` directory")
    fence = section.index("```", start)
    return section[fence : section.index("```", fence + 3)]


# --- the tree, which is criterion 1 ----------------------------------------


def test_the_tree_lists_all_four_directories() -> None:
    """Criterion 1. Three of four is the defect, so all four is the assertion.

    The other three are asserted alongside the new one. Without them a change
    that replaced the tree wholesale with a single line about the journal would
    satisfy the criterion this test exists for.
    """
    tree = _tree_block()

    for directory in ("cache/", "manual/", "reports/", "journal/"):
        assert directory in tree, directory


def test_the_tree_says_what_the_journal_holds_and_who_writes_it() -> None:
    """Criterion 1. A directory listed without its contents is half an entry."""
    tree = " ".join(_tree_block().split())

    assert "JSONL" in tree or "jsonl" in tree
    assert "journal.py" in tree


def test_the_tree_gives_the_journals_tracking_status() -> None:
    """Criterion 1, and the fact the whole issue turns on.

    The tree states tracking status for every other directory. Stating it for
    this one is what stops the closing sentence reading as a recommendation to
    put irreplaceable data somewhere it will not be kept.
    """
    tree = " ".join(_tree_block().split())
    journal = tree[tree.index("journal/") :]

    assert "Git-ignored" in journal or "git-ignored" in journal
    assert ".gitkeep" in journal


# --- the prose, which is criteria 2, 3, 4 and 5 ----------------------------


def test_the_prose_says_why_the_journal_is_not_committed() -> None:
    """Criterion 2. The reason, so this reads as a decision rather than a gap.

    The reproducibility argument that justifies committing `reports/` applies to
    the journal more strongly, so leaving it out needs a different reason
    written down, not the same one omitted.
    """
    section = _data_section()

    assert "real account" in section or "financial record" in section
    assert "public" in section.lower() or "publishable" in section.lower()


def test_the_prose_says_a_fresh_clone_has_no_journal() -> None:
    """Criterion 3. The consequence a contributor actually trips over."""
    section = _data_section()

    assert "fresh clone" in section
    assert "back up" in section or "backup" in section


def test_the_closing_sentence_marks_the_journal_as_untracked() -> None:
    """Criterion 4. The sentence that sent the reader there in the first place.

    It named three destinations for information that cannot be reproduced and
    distinguished none of them. Either it says which one is untracked or it is
    rewritten so it cannot be read as recommending one silently.
    """
    section = _data_section()
    start = section.index("If deleting `data/cache/` loses information")
    sentence = section[start : start + 400]

    assert "journal" in sentence
    assert "untracked" in sentence or "not tracked" in sentence


def test_the_prose_records_the_missing_journal_dir_as_a_known_gap() -> None:
    """Criterion 5. Recorded as known, with no value proposed.

    `DataConfig` carries `cache_dir`, `manual_dir` and `reports_dir`.
    `JOURNAL_PATH` is a module constant, so an operator who relocates the data
    tree moves three directories and leaves behind the only one holding data no
    rerun can recreate.
    """
    section = _data_section()

    assert "journal_dir" in section
    assert "DataConfig" in section


# --- the behaviour, which is criterion 6 -----------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def test_the_journal_contents_are_still_ignored() -> None:
    """Criterion 6. Asserted against git, not against the text of `.gitignore`.

    The criterion is that behaviour is unchanged. Reading the rule back out of
    the file would assert that the file says what it says, which is not the same
    claim and would pass if a later rule overrode it.
    """
    assert _git("check-ignore", "-q", JOURNAL_FILE).returncode == 0


def test_the_journal_gitkeep_is_still_tracked() -> None:
    """Criterion 6's other half. Ignored contents, kept directory.

    Both halves matter: ignoring the `.gitkeep` too would remove the directory
    from a fresh clone, and `journal.py` builds its path from it.
    """
    assert _git("ls-files", "--error-unmatch", JOURNAL_KEEP).returncode == 0


def test_the_negation_that_keeps_the_gitkeep_is_still_in_force() -> None:
    """The rule itself, which the tracking assertion above cannot see.

    ``git check-ignore`` reports a tracked file as not ignored whatever the
    rules say, because ignore rules only govern untracked files. So the plain
    form is near-tautological here and passes even with the ``!`` negation
    deleted, which is how this was found: replacing the two rules with
    ``data/journal/**`` changed nothing it could observe.

    ``--no-index`` asks the rules directly and does distinguish the two, so this
    fails if the negation is ever dropped. The distinction matters on a fresh
    clone, where nothing is tracked yet and the rules are all there is.
    """
    assert _git("check-ignore", "-q", "--no-index", JOURNAL_KEEP).returncode != 0
    assert _git("check-ignore", "-q", "--no-index", JOURNAL_FILE).returncode == 0


# --- the standing instruction on claims ------------------------------------


def test_the_section_claims_nothing_about_what_the_journal_has_measured() -> None:
    """Criterion 7. The journal is the thing that will one day measure the model.

    That makes this section the easiest place in the repository to write a
    sentence implying it already has. Phase 6 has not run.
    """
    section = _data_section().lower()

    for word in ("proven", "backtested", "win rate", "hit rate", "profitable"):
        assert word not in section, word
