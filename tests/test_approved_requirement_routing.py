"""Where an approved proposal's requirement goes, checked against the files.

The two pools in `docs/routines.md` did not cover the state the approval gate
produces. Build lane 1 claims `routine-safe` at `p2` or `p3`. The implementation
lanes claim `roadmap`. The proposals desk converts an approved proposal into a
`type:requirement` and did not apply `roadmap`, so a converted requirement at
`p1` was barred from one pool by priority and from the other by a missing label.

Nothing errors when that happens, which is what makes it expensive. Each lane
correctly reports no qualifying issue, the board shows a healthy `status:ready`
count, and the approved work is never built. Five issues sat in it for a week
while eleven lane slots a weekday passed over them.

Issue #97 ruled that such a requirement carries `roadmap` and no `phase:N`. This
checks that the ruling is written where the desks read it, which is
`docs/routines.md` for the rule and `.github/labels.yml` for what the label
means. Both are prose, and prose is what drifted in the first place.

The `roadmap` description is asserted for length as well as content. Label
descriptions are capped at 100 characters and the apply loop in
`scripts/sync_labels.py` is not atomic, so an over-long entry creates every
label ahead of it and loses the rest. That has happened once already, on
`run:build-b`. `tests/test_sync_labels.py` enforces the cap for the whole file;
this pins the one entry this change touches, so a later rewording of it fails
here with the reason rather than in a partial sync.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ROUTINES = REPO / "docs" / "routines.md"
LABELS = REPO / ".github" / "labels.yml"

DESCRIPTION_LIMIT = 100
"""GitHub's cap on a label description, as `scripts/sync_labels.py` enforces it."""


def _pools() -> str:
    """The two-pools section, collapsed to one line.

    Bounded to the section so a sentence elsewhere in the file cannot satisfy
    an assertion about this one. Collapsed because the prose is hard-wrapped at
    80, so a sentence a reader sees as one line is several in the file.
    """
    body = ROUTINES.read_text()
    start = body.index("## Two pools, and why they do not overlap")
    return " ".join(body[start : body.index("\n## ", start + 1)].split())


def _roadmap_label() -> dict[str, str]:
    entries: list[dict[str, str]] = yaml.safe_load(LABELS.read_text())
    return next(entry for entry in entries if entry["name"] == "roadmap")


# --- the rule, which is criterion 1 ----------------------------------------


def test_the_two_pools_section_routes_a_converted_requirement() -> None:
    """Criterion 1. Both halves in one sentence, because either alone misleads.

    "Carries `roadmap`" without "no `phase:N`" invites a guessed phase, which
    puts a false provenance on the issue. "No `phase:N`" without the rest says
    nothing about who claims it, which is the whole defect.
    """
    assert (
        "A `type:requirement` converted from an approved proposal carries "
        "`roadmap`, and carries no `phase:N`.**" in _pools()
    )


def test_the_section_says_which_lanes_claim_it() -> None:
    """Criterion 1's routing half, stated as the claim rather than implied.

    A reader who knows the label but not the pool still cannot tell whether the
    issue is queued, which is the state the five stranded issues were in.
    """
    assert (
        "The implementation lanes then claim it like any other work in their "
        "pool, at any priority." in _pools()
    )


def test_the_section_says_why_the_phase_label_is_omitted() -> None:
    """The reason, so the next run does not add a phase to tidy the schema.

    An empty column is the kind of thing that gets filled in. Without the
    reason beside it, `phase:4` on proposal-derived work looks like housekeeping
    rather than a false claim about where the work came from.
    """
    body = _pools()

    assert "The phase label is omitted rather than guessed" in body
    assert "false provenance" in body


# --- the residual case, which is criterion 5 -------------------------------


def test_the_section_says_what_happens_to_a_requirement_in_neither_pool() -> None:
    """Criterion 5, which is what stops the gap reopening silently.

    The rule above routes the case that produced this issue. This one names the
    state itself, so a requirement that lands in it for some other reason is
    read as a labelling mistake rather than as work that is waiting its turn.
    """
    body = _pools()

    assert (
        "A `type:requirement` carrying neither `roadmap` nor `routine-safe` "
        "belongs to no pool and nothing will claim it.**" in body
    )
    assert "That is not a resting state, it is a labelling mistake" in body


def test_the_section_records_what_the_gap_cost() -> None:
    """The cost, because a rule with no cost attached reads as bookkeeping.

    The reason this is worth a paragraph rather than a line is that the failure
    is silent: every lane behaves correctly and the board looks healthy.
    """
    body = _pools()

    assert "eleven lane slots a weekday passed over them" in body
    assert "Nothing errors when this happens" in body


# --- the label, which is criterion 2 ---------------------------------------


def test_the_roadmap_label_describes_a_pool_rather_than_a_document() -> None:
    """Criterion 2. The old wording is what made the ruling look wrong.

    "Delivers a roadmap phase" reads as a claim about provenance, so applying
    it to proposal-derived work looks like a category error. It is a claim key,
    and saying so is what makes the rule above follow from the label.
    """
    description = _roadmap_label()["description"]

    assert description == (
        "The implementation lanes' pool. Roadmap phases and approved "
        "requirements, never routine-safe"
    )


def test_the_roadmap_description_is_inside_the_limit() -> None:
    """The cap, pinned on the one entry this change touches.

    `tests/test_sync_labels.py` enforces it across the file. This fails with the
    reason attached if someone rewords this entry past the limit, rather than
    letting a partial sync create the labels above it and drop the rest.
    """
    assert len(_roadmap_label()["description"]) <= DESCRIPTION_LIMIT


def test_the_old_provenance_wording_is_gone() -> None:
    """The phrase itself, paired with the positive assertion above.

    A negative that passes because the entry was deleted proves nothing, which
    is why `test_the_roadmap_label_describes_a_pool_rather_than_a_document`
    asserts the whole replacement rather than a fragment of it.
    """
    assert "Delivers a roadmap phase" not in LABELS.read_text()
