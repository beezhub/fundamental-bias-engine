"""The three labels that keep an issue out of the maintenance pool.

A lane matches on labels and does not read an issue's closing section. #71 ended
with a section headed "Why this is not `routine-safe`", arguing that its two
halves belonged to different owners and would drift if split, and it carried
`routine-safe` anyway from 14 September. Build lane 1 could have claimed it at
any of nine weekday slots and split the fix, which is the outcome that paragraph
predicted. It did not, by luck rather than by design.

So the exclusion has to live in the thing the lane matches on. `desk-only` is
that label, ruled on #154, and it joins `roadmap` and `routine-hold` as one
list rather than a third rule.

**The failure this module exists to catch is the list reaching one document and
not the other.** That is not hypothetical: `routine-hold`'s exclusion reached
`docs/routines.md` and has still not reached the build lane 1 prompt, so a run
following its prompt rather than the file would claim a held issue. The prompt
is outside this repository and no test can read it. The two documents inside it
can be held to each other, and that is what the sweep below does.

The set is read out of each document rather than written here twice. Writing it
here would make this a test of its own copy, which is the defect one level up:
a rule in two places disagreeing silently.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

ROUTINES = REPO / "docs" / "routines.md"
SKILL = REPO / ".claude" / "skills" / "issue-workflow" / "SKILL.md"
LABELS = REPO / ".github" / "labels.yml"

BACKTICKED = re.compile(r"`([a-z][a-z0-9:-]*)`")

CLAIM_KEY = "routine-safe"
"""The label the exclusions are about, excluded from the sets being compared.

Every enumeration names it as the thing not being applied, so leaving it in
would put it in both sets and weaken nothing, but naming it here says plainly
that it is the subject rather than one of the exclusions.
"""


def _label_names() -> frozenset[str]:
    """Every label the repository actually defines.

    The extraction below keeps only backticked tokens that name a real label.
    Without that, a document mentioning `status:ready` in the same sentence
    would put it in the set and the comparison would drift for a reason that
    has nothing to do with the exclusions.
    """
    entries: list[dict[str, str]] = yaml.safe_load(LABELS.read_text())
    return frozenset(entry["name"] for entry in entries)


def _excluded_in(path: Path, marker: str, span: int) -> frozenset[str]:
    """The labels named in one document's exclusion statement.

    Args:
        path: The document to read.
        marker: Text that starts the statement, asserted present so a rewording
            fails here rather than returning an empty set and passing.
        span: How many characters after the marker the statement runs for.

    Returns:
        Every real label named in that span, less `routine-safe` itself.
    """
    body = " ".join(path.read_text().split())
    assert marker in body, f"{path.relative_to(REPO)} no longer says {marker!r}"

    start = body.index(marker)
    named = set(BACKTICKED.findall(body[start : start + span]))
    return frozenset(named & _label_names()) - {CLAIM_KEY}


def _routines_exclusions() -> frozenset[str]:
    return _excluded_in(ROUTINES, "Add `routine-safe` to anything carrying", 120)


def _skill_exclusions() -> frozenset[str]:
    return _excluded_in(
        SKILL, "triage never adds `routine-safe` to an issue carrying any of them", 300
    )


# --- the set itself, which is the criterion --------------------------------


def test_both_documents_name_the_same_exclusions() -> None:
    """The criterion. A third label cannot land in one file and not the other.

    Compared as sets read out of the two documents rather than against a list
    written here, so this fails on the disagreement itself rather than on
    either document disagreeing with a third copy.
    """
    assert _routines_exclusions() == _skill_exclusions()


def test_the_exclusions_are_the_three_that_were_ruled() -> None:
    """Guards the comparison above from passing on two empty sets.

    Two extractions that both stopped matching would agree perfectly and check
    nothing. This is the one place the three names are written down in this
    module, and it is deliberately separate from the comparison so that a
    genuine fourth exclusion fails here, visibly, rather than silently widening
    what lanes skip.
    """
    assert _routines_exclusions() == {"roadmap", "routine-hold", "desk-only"}


def test_the_label_exists_with_a_description_of_what_it_keys() -> None:
    """The label is real, not only described.

    A rule naming a label nobody created is worse than no rule: triage would
    look for it, never find it, and the exclusion would read as honoured.
    """
    entries: list[dict[str, str]] = yaml.safe_load(LABELS.read_text())
    desk_only = next(entry for entry in entries if entry["name"] == "desk-only")

    assert "maintenance pool" in desk_only["description"]
    assert "filing desk" in desk_only["description"]
    assert len(desk_only["description"]) <= 100


# --- the rules that stop it landing dead -----------------------------------


def test_both_documents_say_the_label_is_inert_until_the_prompts_carry_it() -> None:
    """The half that decides whether this is a control or the look of one.

    The label lives here and the behaviour lives in four routine definitions
    outside the repository. A reader who takes the merge as the control going
    live is the person who will later be surprised that a `desk-only` issue was
    claimed, so both documents say it.
    """
    routines = " ".join(ROUTINES.read_text().split())
    skill = " ".join(SKILL.read_text().split())

    assert "**`desk-only` has no effect until the routine prompts carry it.**" in (
        routines
    )
    assert (
        "**Neither label has any effect until the routine prompts carry it.**" in skill
    )


def test_the_routines_say_triage_never_overrides_the_prose_silently() -> None:
    """The rule for an issue that argues against the label and carries none.

    Asserted with the distinction that carries it. "Override with a comment" is
    satisfiable by a comment that notes the argument and answers nothing, which
    is silence with extra words, so the sentence names what the comment has to
    do.
    """
    body = " ".join(ROUTINES.read_text().split())

    assert "adds `routine-safe` with a comment that answers the argument" in body
    assert "Never silently, and never a comment that merely notes it." in body


def test_the_routines_carry_the_worked_example() -> None:
    """#71, which is the case that occurred rather than an invented one.

    A label nobody remembers to apply is dead weight, and the example is what
    makes it remembered: a desk writing that paragraph today recognises itself.
    """
    body = " ".join(ROUTINES.read_text().split())

    assert '#71 is the case: it ends with a section headed "Why this is not' in body
    assert "a lane matches on labels and does not read the closing section" in body


def test_the_routines_say_why_routine_hold_cannot_take_this_job() -> None:
    """The alternative that was rejected, with the reason, so it is not reopened.

    Redefining `routine-hold` is the obvious cheaper option and it is ruled out
    by its own defining sentence. Without the reason recorded, the next run
    proposing it has to be talked out of it again.
    """
    body = " ".join(ROUTINES.read_text().split())

    assert "`routine-hold` cannot take this job" in body
    assert "Only a person adds or removes it" in body


# --- the bounds, which are criteria 4 and 6 --------------------------------


def test_both_documents_state_the_bounds_on_the_change_not_the_file() -> None:
    """Criteria 4 and 6 together, because they are one ruling.

    The bar protects against a moved number and a broken contract. A docstring
    is neither, and reading the bounds as file bounds left #11, #40 and #182
    unclaimable by the only pool that runs three times a weekday.
    """
    routines = " ".join(ROUTINES.read_text().split())
    skill = " ".join(SKILL.read_text().split())

    for body in (routines, skill):
        assert "bounds are on the change, not on the file" in body
        assert "It may not change a field" in body or (
            "changing a field, a type, an argument order or a default" in body
        )

    assert "#11, #40 and #182" in routines
    assert "#11, #40 and #182" in skill


def test_the_lane_row_states_both_bounds_by_what_changes() -> None:
    """The row, because that is what a run reads to find out what it may do.

    A correct paragraph beside a row still saying "Touch `types.py`" leaves the
    run following the row, which is the same shape as the defect this issue is
    about.
    """
    body = " ".join(ROUTINES.read_text().split())
    # The runs table, not the weekday schedule, which also has a row opening
    # "| build lane 1 |" and carries only a time. Matching the short prefix
    # found that one and checked the wrong table, which is how this locator
    # was written the first time.
    start = body.index("| build lane 1 | weekdays 07:00, 11:00 and 15:00 |")
    row = body[start : body.index("| implement lane A |", start)]

    assert "Change a field, a type, an argument order or a default in" in row
    assert "Change a value in `ScoringConfig` or `RiskConfig`." in row
    assert "Touch `types.py`." not in row
