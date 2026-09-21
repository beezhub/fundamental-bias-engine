"""The closing trailer is prescribed as plain text, in every place it is taught.

`Closes: #NN` in a pull request body closes the issue on merge. Written inside
backticks it is a code span: GitHub parses no keyword, creates no link, and the
merge closes nothing, while the body still reads as though it does.

It cannot be repaired afterwards. The links are resolved when the pull request
merges, so editing a merged body changes nothing and a person has to close the
issue by hand. Three instances are on the record: #131 left #115 open, #166 left
#156 open, and #184 left #158 open.

What makes it worth a change rather than more care is who made those mistakes.
The desk that wrote up the second instance, including the explanation that
backticks read as correct because every other identifier in these bodies is in
backticks, then made the same mistake the next day in a body discussing this
issue. So the failure survives being understood by the person about to commit
it, and the reason is priming: the documents that teach the trailer showed it in
backticks.

That is why this checks two things rather than one. Each document says the rule,
**and** none of them demonstrates the wrong form while stating it. The second
half is the one that addresses the actual mechanism.

A backticked trailer inside a Markdown file closes nothing and breaks nothing:
only a pull request body and a commit message are parsed for keywords. It is
still worth failing on, because these files are what an author reads immediately
before writing a body, and what they show is what gets copied.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

CONTRIBUTING = REPO / "CONTRIBUTING.md"
TEMPLATE = REPO / ".github" / "pull_request_template.md"
DEVELOPER = REPO / ".claude" / "agents" / "developer.md"
ISSUE_WORKFLOW = REPO / ".claude" / "skills" / "issue-workflow" / "SKILL.md"
NEXT = REPO / ".claude" / "skills" / "next" / "SKILL.md"

TEACHES_THE_TRAILER: tuple[Path, ...] = (
    CONTRIBUTING,
    TEMPLATE,
    DEVELOPER,
    ISSUE_WORKFLOW,
    NEXT,
)
"""Every document that tells someone to write the trailer.

The first three are the files issue #132 names. The last two are the skills a
lane loads immediately before writing a pull request body, and both stated the
rule while showing the trailer in backticks, which is the priming the issue is
about.
"""

BACKTICKED_TRAILER = re.compile(r"`(?:Closes|Refs|Fixes|Resolves):\s*#")
"""A closing trailer written inside a code span.

Matches the opening backtick and the keyword together, so ``Closes:`` in running
prose and a backticked ``#NN`` on its own both pass. Only the pairing is the
defect, because only the pairing is what an author copies into a body.
"""


def _flat(path: Path) -> str:
    """The file as one line, since these are hard-wrapped at 80.

    A sentence a reader sees as one line is several in the file, so a search for
    it has to collapse the wrapping or it misses.
    """
    return " ".join(path.read_text().split())


# --- the rule is stated, which is criteria 1, 2 and 3 ----------------------


def test_contributing_says_the_trailer_is_plain_text() -> None:
    """Criterion 1. The instruction and the warning, asserted together.

    "Write it as plain text" alone reads as a style note. The consequence is
    what makes it a rule, and the consequence is invisible: the body looks
    correct and the issue stays open.
    """
    body = _flat(CONTRIBUTING)

    assert "**Write the trailer as plain text. Never put backticks around it.**" in body
    assert "GitHub reads a backticked trailer as a code span" in body


def test_contributing_says_it_cannot_be_fixed_after_the_merge() -> None:
    """Criterion 1's other half, and the reason care alone does not cover it.

    Most mistakes in a pull request body are editable. This one is not, and a
    reader who assumes it is will not treat the rule as load-bearing.
    """
    assert "editing a merged body changes nothing" in _flat(CONTRIBUTING)


def test_the_template_carries_the_note_beside_the_trailer_line() -> None:
    """Criterion 2, asserted by position rather than by presence.

    The criterion is that the note sits beside the line a desk edits. A correct
    note in the wrong part of the template is not the same thing: this is the
    one place the warning is in front of the author at the moment they write.
    """
    lines = TEMPLATE.read_text().splitlines()
    # The bare line a desk fills in, not the first text that happens to contain
    # "Refs: #". The note itself names both trailers, so matching on substring
    # finds the note rather than the line the note is about.
    editable = next(i for i, line in enumerate(lines) if line.strip() == "Refs: #")
    preceding = "\n".join(lines[:editable])

    assert "NEVER inside backticks" in preceding
    assert preceding.rindex("<!--") > preceding.rindex("## What this changes")


def test_the_developer_brief_says_it_where_it_names_the_trailer() -> None:
    """Criterion 3. The brief an implementation lane reads before writing.

    Asserted on the sentence that prescribes the trailer rather than anywhere in
    the file, so a note added elsewhere does not satisfy a criterion about the
    place a lane is actually reading.
    """
    body = _flat(DEVELOPER)
    start = body.index("One issue per pull request")
    paragraph = body[start : start + 600]

    assert "Write it as plain text and never inside backticks" in paragraph
    assert "the merge closes nothing" in paragraph


# --- the rule is modelled, which is the mechanism --------------------------


@pytest.mark.parametrize(
    "path", TEACHES_THE_TRAILER, ids=lambda p: str(p.relative_to(REPO))
)
def test_no_document_that_teaches_the_trailer_shows_it_backticked(path: Path) -> None:
    """The half that addresses why the rule was not enough on its own.

    Parametrised per file so a failure names the file rather than handing back
    one assertion about five of them.

    This is not about the Markdown rendering. A backticked trailer in a document
    closes nothing and breaks nothing, because only pull request bodies and
    commit messages are parsed. It is about what the next author copies, which
    is the documented cause of all three instances.
    """
    found = BACKTICKED_TRAILER.findall(path.read_text())

    assert not found, (
        f"{path.relative_to(REPO)} shows a closing trailer inside backticks, "
        f"which is the form it tells the reader not to use: {found}"
    )


def test_the_guard_would_catch_the_form_it_is_looking_for() -> None:
    """Guards the sweep above from passing because the pattern matches nothing.

    Every assertion in that sweep is a negative, so a pattern that had stopped
    matching would leave five files unchecked and the suite green. This pins the
    pattern against the exact string the three instances used.
    """
    assert BACKTICKED_TRAILER.search("see `Closes: #115` in the body")
    assert BACKTICKED_TRAILER.search("`Refs: #NN`")
    assert not BACKTICKED_TRAILER.search("Closes: #115")
    assert not BACKTICKED_TRAILER.search("the `#115` issue")
