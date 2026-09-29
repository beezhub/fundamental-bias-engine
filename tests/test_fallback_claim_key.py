"""The fallback the implementation lanes claim from when their own pool is shut.

#270. Both lanes stopped at step 2 on two consecutive days with ten ready,
unblocked issues in front of them, nine of them ruled, because the claim key
requires `roadmap` and none of those carried it. The bottleneck is not a
shortage of filed work: a dependency only counts as landed when a human merges,
so two green unreviewed pull requests held four issues shut.

The ruling of 2026-09-25 gave the lanes a fallback. It then lived only in a
GitHub comment for three days while `docs/routines.md` went on describing the
claim key it replaced, which is the failure this module exists to catch: a rule
that reaches the desk's memory and not the file the desk is told is the
authority.

The rule is read out of the document rather than written here twice, except
where the exact wording is the thing being pinned. Every assertion is bounded
to the row or the section that carries the rule, never to the whole file. That
is not tidiness: the same change added "carries an architect ruling with
acceptance criteria" to the two-pools prose as well, so a whole-file search for
the fence passes with the fence deleted from both places that define the pool.

Nothing here reaches the network.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ROUTINES = REPO / "docs" / "routines.md"
DECISIONS = REPO / "docs" / "decisions"
ADR = DECISIONS / "0017-the-implementation-lanes-claim-key-has-a-fallback.md"
INDEX = DECISIONS / "README.md"

FALLBACK = "### The fallback, for the day the roadmap pool is shut"

CONDITIONS = (
    "carries an architect ruling with acceptance criteria",
    "has no open dependency",
    "carries neither `routine-hold` nor `desk-only`",
    "is not already `status:in-progress`",
    "is not `p0`",
    "is not a `type:proposal`",
)
"""The fence, in the words the document uses.

Written here rather than read out of the file, because these are the assertion:
a fallback stated with four of them is a wider pool than the one written, and
the difference is the fence.

The first four are the ruling's own. The last two are narrower than the ruling,
which said "at any priority" and named no type, and they are here because the
document states them and a document that states a bound has to keep it. Whether
they survive is the architect's, and the section says they are raised on #270.
"""


def _section(path: Path, heading: str, end: str) -> str:
    """One section of a document, collapsed to one line.

    Args:
        path: The document to read.
        heading: The section's heading line, asserted present so a rename fails
            here rather than returning an empty string and passing.
        end: Text that starts the next section.

    Returns:
        The text between the two, whitespace collapsed, because the prose is
        hard-wrapped at 80 and a sentence a reader sees as one line is several
        in the file.
    """
    body = path.read_text()
    assert heading in body, f"{path.name} no longer has {heading!r}"

    start = body.index(heading)
    return " ".join(body[start : body.index(end, start + len(heading))].split())


def _fallback() -> str:
    return _section(ROUTINES, FALLBACK, "\n## ")


def _alternatives() -> str:
    return _section(ADR, "## Alternatives considered", "\n## Consequences")


def _row(run: str) -> str:
    """One run's row from the table that states what each run may do.

    A table row is one line in the file, so the row is read as a line rather
    than carved out of the collapsed text. Anchored on the schedule cell that
    follows the name because the file carries three tables keyed on the same
    run names, and this is the only one that carries the rules: the others key
    the run to its desk label and to its hour. Those two put the name in the
    second cell rather than the first, so the anchor is belt and braces rather
    than the only thing separating them, and it also fails loudly if the runs
    table ever drops the schedule cell.

    Raises:
        AssertionError: if no such row exists, rather than `StopIteration`,
            which pytest reports as an error inside the helper instead of as
            the document having changed shape.
    """
    prefix = f"| {run} | weekdays"
    rows = [
        line for line in ROUTINES.read_text().splitlines() if line.startswith(prefix)
    ]
    assert len(rows) == 1, f"{len(rows)} rows in routines.md start {prefix!r}"
    return rows[0]


def test_both_lane_rows_carry_the_fallback() -> None:
    """A rule on one row only is the shape the old drift took.

    Lane A works up and lane B works down, and they were given the fallback
    together. A reader checking their own lane's row must find it there rather
    than inferring it from the other, which is why the trigger is asserted and
    not the word "fallback". Lane B's row opens "The same", and that convention
    carries a rule the reader has just read one row above. It does not carry
    the conditions, which criterion 5 asks for on both rows and which the
    conditions test below checks on both.
    """
    for lane in ("A", "B"):
        row = _row(f"implement lane {lane}")
        assert "claims from the fallback instead" in row, lane


def test_the_lane_rows_name_the_ends_they_take_from_the_fallback() -> None:
    """Lane A takes the lowest and lane B the highest, as in the roadmap pool.

    Without this the two lanes race for the same issue on the day the fallback
    fires, which is the collision the numbered ends exist to prevent.
    """
    assert "lowest-numbered" in _row("implement lane A")
    assert "highest-numbered" in _row("implement lane B")


def test_the_four_conditions_are_all_stated() -> None:
    """The fence, in full, in both places that define the pool.

    Three of four is a wider pool than the one ruled. Asserted against lane A's
    row and against the section separately rather than against the file,
    because the file also names the ruling condition in the two-pools prose,
    and a search of the whole file passes with the fence gone from both places
    a lane actually reads.
    """
    section = _fallback()

    for condition in CONDITIONS:
        assert condition in section, f"fallback section: {condition}"
        for lane in ("A", "B"):
            assert condition in _row(f"implement lane {lane}"), f"{lane}: {condition}"


def test_the_lane_declares_in_its_pull_request_that_it_used_the_fallback() -> None:
    """Otherwise a reviewer cannot tell a fallback claim from a roadmap one.

    The two have different reasons for existing and the pull request is where
    that reason has to be visible, because nothing else records it. Pinned on
    both rows and on the section: the row is what a run reads to find out what
    it owes, the section is what a reader checking the rule reads, and
    criterion 5 names the rows.
    """
    owed = "says in its pull request body that it claimed from the fallback"

    assert owed in _fallback()
    for lane in ("A", "B"):
        assert owed in _row(f"implement lane {lane}"), lane


def test_a_ready_issue_with_no_architect_ruling_is_excluded_with_its_reason() -> None:
    """The fence, and why, so it cannot be read away as an oversight.

    Build lane 1 is barred from `p1` because a high-priority defect deserves a
    person deciding who fixes it. The architect is that person and the ruling
    is the record, which is what makes `p1` safe for these lanes and what an
    unruled issue lacks.
    """
    body = _fallback()

    assert "A ready issue with no architect ruling is not in the fallback pool" in body
    assert "the gate has already run" in body


def test_the_floor_survives_the_fallback() -> None:
    """Stopping is still the answer when the fallback is empty too.

    The fallback widens the pool; it does not oblige a lane to find something.
    A lane that claims an unruled issue because nothing else was left has
    removed the fence.
    """
    assert "the lane says which issues are blocked and on what, and stops" in (
        _fallback()
    )


def test_the_section_says_whether_the_rule_is_live_is_not_settled() -> None:
    """A reader who finishes the section otherwise assumes it starts tomorrow.

    `desk-only` is the precedent and it runs the other way: this file already
    rules that a rule reaching it and not the routine prompts stops nothing.
    That ruling is about a label a lane matches on, and this is a claim rule a
    lane reaches for when its own key returns nothing, so the two may not be
    the same case. Nothing distinguishes them yet, and saying which it is would
    be deciding the `desk-only` ruling's scope in passing. The section says the
    question is open and names the step that closes it, which is the owner's.
    """
    body = _fallback()

    assert "Whether the lanes do this before their prompts are recreated is not" in body
    assert "Recreating the two implementation routines with the fallback" in body
    assert "only the owner can do that" in body


def test_deliver_counts_dependencies_rather_than_labels() -> None:
    """Criterion 1. The sentence was already right, which is why it is pinned.

    A count of `status:ready` issues by label reads four where the rule holds
    zero, and that reading is what produced the two idle days: the delivery run
    files nothing because the board looks healthy, and the lanes then find
    nothing claimable. Nothing tested this sentence, so an edit tidying the row
    could have reverted it silently.
    """
    assert (
        "Counts open issues carrying `roadmap` and `status:ready` whose "
        "dependencies have all landed." in _row("deliver")
    )


def test_deliver_files_nothing_when_the_phase_is_fully_decomposed() -> None:
    """Criterion 3. Today's answer is "files nothing and stops", unwritten.

    Decomposing a phase whose deliverables are sequential does not produce
    parallel work, so the aim of roughly six unblocked issues can be unmet with
    nothing left to decompose. What deliver does then was the one thing the
    routine's own description did not say, and the answer is asserted rather
    than the phrase, because the opposite rule states the phrase too.
    """
    assert "it files nothing and stops rather than decomposing the next phase" in _row(
        "deliver"
    )


def test_the_ruling_is_recorded_as_a_decision() -> None:
    """It is cross-cutting, so it belongs in `docs/decisions/`, not a comment.

    Three runs claimed from a rule that existed only in a GitHub comment. A
    decision that lives in one comment is one nobody finds from `main`.
    """
    assert ADR.exists()

    record = ADR.read_text()

    assert "Status: Accepted" in record
    assert "#270" in record


def test_the_decision_is_in_the_index() -> None:
    """An ADR the index does not list is one the next reader does not find."""
    assert ADR.name in INDEX.read_text()


def test_the_decision_names_why_it_rejected_branching_from_an_open_head() -> None:
    """#270's option B, branching from an unmerged pull request's head.

    It is the obvious unblock and it trades away the reason the delivery order
    exists, so a record that does not say why it was rejected invites it back.
    The ADR does not letter its alternatives, so it is named here by what it
    proposes rather than by the letter the issue gave it.
    Bounded to the section and asserted on the reason rather than on the word
    "unmerged", which also appears in the Status section and let the whole
    argument be replaced by "None." with this test still green.
    """
    body = _alternatives()

    assert "branch from an unmerged pull request's head" in body
    assert "the dependency can still change under review" in body
    assert "rebase debt" in body


def test_the_two_narrowed_conditions_are_marked_as_narrower_than_the_ruling() -> None:
    """The document states a bound the ruling did not, and says so in the file.

    Silently narrowing a ruling is the same defect as silently widening one: a
    reader comparing the file against the comment on #270 finds a difference
    and cannot tell whether it was reasoned or lost in transcription. Both
    exclusions are conservative, `p0` because a wrong number is reaching the
    trader now and `type:proposal` because the approval gate has no agent
    override, and both are the architect's to confirm or lift.
    """
    body = _fallback()

    assert "narrower than the ruling and are raised on #270" in body
    assert "It did not reach `p0`" in body
    assert "the approval gate on `type:proposal` has no agent override" in body


def test_the_gap_between_the_trigger_and_claimability_is_named() -> None:
    """The trigger fires on dependency state, and #270 asked for claimability.

    One dependency-clear roadmap issue, claimed by the first lane at 09:00,
    keeps the pool open for the 12:00 lane by this rule while holding nothing
    it can take. That is the #270 outcome reproduced by the rule written for
    it. Widening the trigger widens what an unattended lane may claim, so the
    gap is recorded rather than closed, and what is asserted here is that it is
    recorded rather than left for a reader to find.
    """
    body = _fallback()

    assert "The trigger is dependency state, not claimability" in body
    assert "already claimed it, or it carries `routine-hold`" in body


def test_the_section_says_what_a_lane_does_when_it_cannot_read_the_fence() -> None:
    """The fence is prose on an issue, and a lane matches on labels.

    #71 is the case this file already records: a lane does not read an issue's
    closing section, which is why `desk-only` is a label. The ruling with
    criteria cannot be a label and the ADR declines to keep a second list, so
    what is left is what a lane does when it cannot tell. Excluding the issue
    is the conservative answer, and an unstated one would have each run guess.
    """
    body = _fallback()

    assert "it treats the issue as outside the pool and says so" in body


def test_the_deliver_row_no_longer_promises_the_fallback_covers_the_day() -> None:
    """The fallback fires at zero claimable, and deliver stops under four.

    A row reading "the lanes have the fallback for that day" is false for a
    count of one, two or three: the pool is not shut, so the fallback does not
    fire, and the slots that find nothing to claim idle. Deliver filing nothing
    is correct on its own terms and does not need the guarantee.
    """
    row = _row("deliver")

    assert "the lanes have the fallback" not in row
    assert "widens a queue that is already longer than the merge rate" in row
