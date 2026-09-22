"""The triage release test and the `Closes:` rule, checked against the files.

Two rules combined into a loop that sent lanes back over finished work. Triage
released a claim when the issue had no **open** pull request, and the lanes
wrote `Refs:` far more often than `Closes:`, which closes nothing. So an issue
whose work had merged looked identical to one whose run was killed: still open,
still `status:in-progress`, and back in the claimable pool the next morning.

It ran twice before anyone noticed, on #16 and #20, and each time cost most of a
lane slot proving that finished work was finished. By 2026-09-18 twelve issues
were sitting in that state. Issue #96 carries the count as it grew.

Both halves are prose in two files, which is how they drifted apart from what
the desks actually do. This checks them the way the rest of this suite checks a
document: by asserting the sentence that carries the meaning, not a word that
several other sentences also contain.

The phrases here are asserted whole for that reason. An earlier habit of
searching for "merged" or "Closes" on its own passed against unrelated lines in
the same section, which is a test that cannot fail for the right reason.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTINES = REPO / "docs" / "routines.md"
SKILL = REPO / ".claude" / "skills" / "issue-workflow" / "SKILL.md"


GREP_ARGUMENT = re.compile(r'git log main[^\n]*--grep="([^"]+)"')
"""The pattern out of the command the routines document.

Read back rather than retyped. A test that retypes it checks a copy, and the
copy is what went wrong: the first version of this module asserted the command
as a fixed string, so it passed against a pattern that matched the wrong issue.
"""


def _flat(path: Path) -> str:
    """The file as one line, since both are hard-wrapped at 80.

    A phrase that reads as one sentence is split across lines in the file, so a
    naive search for it misses. Collapsing whitespace is what lets the
    assertions below quote the sentence as a reader sees it.
    """
    return " ".join(path.read_text().split())


def _documented_pattern(issue: int) -> re.Pattern[str]:
    """The documented ``--grep`` pattern, with ``NN`` standing for `issue`.

    The document writes the placeholder as ``NN`` so a reader substitutes their
    own number. Doing the same here is what makes the assertions below a test of
    the command rather than of a sentence describing it.
    """
    found = GREP_ARGUMENT.search(ROUTINES.read_text())
    assert found is not None, "the routines no longer document a git log command"
    return re.compile(found.group(1).replace("NN", str(issue)))


# --- the release test, which is criterion 1 --------------------------------


def test_the_triage_row_states_the_release_test_as_open_or_merged() -> None:
    """Criterion 1's first half, asserted on the row rather than the section.

    The row is what a run reads to find out what it does. A paragraph below the
    table saying the right thing while the row says the old thing would leave
    the run following the row.
    """
    body = _flat(ROUTINES)
    start = body.index("| triage | weekdays 06:00 and 12:00 |")
    row = body[start : body.index("| build lane 1 |", start)]

    assert (
        "Releases stranded `status:in-progress` claims with no pull request "
        "referencing the issue, **open or merged**." in row
    )


def test_the_triage_row_says_what_happens_when_the_work_has_merged() -> None:
    """Criterion 1's second half. Correcting the test alone is not enough.

    A run told only that a merged pull request fails the release test still has
    to decide what to do with the issue, and the answer is not "nothing": the
    issue is finished and waiting on a person, which is a thing to report.
    """
    body = _flat(ROUTINES)
    start = body.index("| triage | weekdays 06:00 and 12:00 |")
    row = body[start : body.index("| build lane 1 |", start)]

    assert (
        "Reports a claim whose pull request has merged for closure instead of "
        "releasing it." in row
    )


def test_the_old_release_wording_is_gone_from_the_routines() -> None:
    """The defective phrase itself, so a later edit cannot reintroduce it.

    Paired with the positive assertions above rather than standing alone: a
    negative that passes because the section was deleted proves nothing.
    """
    assert "claims with no open pull request" not in _flat(ROUTINES)


def test_the_routines_give_the_command_that_separates_the_two_cases() -> None:
    """The check itself, which is what makes the rule usable rather than true.

    A run that knows the test but not how to run it will reach for the pull
    request list, which is the wrong index: a merged pull request can predate
    the claim comment by days and still be the reason the issue is open.
    """
    body = _flat(ROUTINES)

    assert "git log main" in body
    assert "Only a person closes an issue" in body


def test_the_documented_pattern_ends_at_the_issue_number() -> None:
    """Issue #207. The behaviour, because the string passed against the defect.

    The first version of this module pinned the command as text, so it went
    green on a pattern with no right-hand boundary. `--grep` takes a regular
    expression, and `#2` as a prefix matches every commit mentioning #20, #24
    or #201: on `main` that was 54 commits of finished work behind an issue
    with none, read by the rule as "this issue is done".

    So this compiles what the document actually says and asks it about the case
    that failed. A pattern that matches #200 when asked about #2 fails here
    whatever the surrounding prose claims.
    """
    pattern = _documented_pattern(2)

    assert pattern.search("Refs: #2")
    assert pattern.search("#2 and more besides")
    assert pattern.search("closes #2.")
    assert not pattern.search("Refs: #20")
    assert not pattern.search("Refs: #200")


def test_the_unbounded_command_is_gone_from_the_routines() -> None:
    """Criterion 3's other half, so the first defect cannot return either.

    Paired with the positive assertion above rather than standing alone. A
    negative that passes because the whole paragraph was deleted proves nothing,
    which is why `test_the_documented_pattern_ends_at_the_issue_number` reads
    the command back out and exercises it.
    """
    assert 'git log main --grep="#NN"' not in _flat(ROUTINES)


def test_the_routines_exempt_a_proposal_from_the_release_test() -> None:
    """Issue #207's second defect, which costs the approval gate.

    A `type:proposal` has no pull request of its own, because the workflow
    forbids implementing one directly. So the corrected command returns nothing
    for every approved proposal, correctly, and the paragraph's conclusion from
    "nothing found" is that the claim is stranded and should be released. That
    puts a proposal in the claimable pool, which is the one thing the
    `issue-workflow` skill says has no agent override.

    Asserted as the sentence that carries the exemption and the reason for it.
    The reason is what stops a later edit trimming this to a bare exception that
    reads as arbitrary.
    """
    body = _flat(ROUTINES)

    assert "**A `type:proposal` is never released by this test.**" in body
    assert "has no pull request of its own" in body
    assert '"Nothing found" therefore carries no information about it' in body


# --- the trailer rule, which is criteria 2 and 3 ---------------------------


def test_the_skill_gives_the_rule_for_choosing_the_trailer() -> None:
    """Criterion 2. Both halves, because either alone is a different rule.

    "Use Closes:" without the partial case would have lanes closing issues their
    pull request only half satisfies, which is the same defect pointing the
    other way and a worse one: an issue closed with work outstanding is
    invisible, where one left open is merely untidy.

    The trailers asserted here are bare rather than backticked, and that is the
    rule rather than a typo. #132 found that a trailer shown in backticks is
    what the next author copies into a pull request body, where it becomes a
    code span and closes nothing.
    """
    body = _flat(SKILL)

    assert (
        "**Closes: #NN when the pull request meets every acceptance criterion "
        "on the issue. Refs: #NN when it is partial, or one of several.**" in body
    )


def test_the_skill_says_the_trailer_must_be_outside_backticks() -> None:
    """The mechanical half of the rule, which is easy to get wrong silently.

    A trailer inside backticks renders correctly and closes nothing, so the
    pull request looks right and the issue stays open. #132 owns the check that
    would catch it; this is the sentence that tells a lane not to write it.
    """
    assert (
        "The trailer is plain text and outside backticks, or GitHub does not "
        "read it." in _flat(SKILL)
    )


def test_the_skill_says_what_the_wrong_trailer_costs() -> None:
    """Criterion 2's reason. A rule with no cost attached gets treated as taste.

    The `Refs:` habit was not carelessness. It is the safer-looking option, and
    it stays the safer-looking option until someone writes down that it is what
    fills the backlog.
    """
    body = _flat(SKILL)

    assert "`Refs:` closes nothing" in body
    assert "returns it to the pool" in body


def test_the_bar_on_a_routine_closing_an_issue_still_stands() -> None:
    """Criterion 3's first half. The rule is unchanged and must stay unchanged.

    Everything else here makes closing happen earlier and more often, which is
    exactly the pressure that would erode this. The bar is what keeps a person
    between merged work and a closed issue.
    """
    assert "closing an issue it did not fully resolve" in _flat(SKILL)


def test_the_skill_says_why_the_closing_bar_stands() -> None:
    """Criterion 3's second half, so the next run does not re-litigate it.

    It was considered and decided against on #96, with a reason. Without the
    reason recorded next to the rule, the next run reads a bar that costs it a
    slot and re-opens the question from scratch.
    """
    body = _flat(SKILL)

    assert (
        "The bar on closing stands, and it is deliberate rather than an "
        "oversight." in body
    )
    assert "the person who merged the pull request is the one who read the diff" in body
    assert "Ruled on #96" in body
