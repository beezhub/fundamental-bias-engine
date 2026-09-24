"""`CalendarEvent.impact` documents the vocabulary the feed actually emits.

The Forex Factory feed publishes ``High``, ``Medium``, ``Low`` and ``Holiday``,
capitalised exactly like that. `fbe.types.CalendarEvent.impact` described the
field as ``"high"``, ``"medium"`` or ``"low"``: wrong in the case of all three
it listed, and silent about the fourth.

Nothing computed the wrong answer, because `fbe.calendar_guard.is_high_impact`
folds the case before comparing. The cost is in what the shared vocabulary
teaches the next consumer. A developer reading `types.py`, which `CLAUDE.md`
names as the file every module speaks in terms of, writes
``event.impact == "high"``, matches none of the 105 rows in the captured week,
and reports every week as clear, including the ones carrying a Federal Open
Market Committee decision. That failure is silent in the direction that lets a
trade through.

The ``Holiday`` omission is the more expensive half, because a reader of
`types.py` alone has no reason to believe the value exists, and a closure thins
the book rather than spiking it.

Two halves, as with any contract test here. The wording half pins what the
field says. `test_the_documented_values_are_exactly_what_the_feed_emits` is the
one that matters most: it parses the values out of the docstring and compares
them against `fbe.datasources.calendar.IMPACT_LEVELS`, so the contract and the
authority cannot drift apart again without a failure. The behaviour the wording
now relies on is pinned next door in `tests/test_blackout_windows.py`, beside
the other `is_high_impact` tests rather than duplicated here.

Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import re

from fbe.datasources.calendar import IMPACT_LEVELS
from fbe.types import CalendarEvent

SUPERSEDED = '``"high"``, ``"medium"`` or ``"low"``'
"""The phrase the field carried, kept so the test and the issue look at the same
characters rather than at two approximations of them."""

QUOTED = re.compile(r'``"([A-Za-z]+)"``')
"""A value written the way this repository writes one, in a literal code span.

Matching the inner quotes as well as the backticks is what keeps
``Holiday`` in prose, where the docstring calls it a closure, from being read
as a fifth member of the vocabulary.
"""


def _impact_doc() -> str:
    """The docstring written under ``impact`` in `CalendarEvent`'s source.

    Read out of the source rather than from ``__doc__``, because an attribute
    docstring on a dataclass field is not kept at runtime.
    """
    source = inspect.getsource(CalendarEvent)
    match = re.search(
        r'^\s+impact:[^\n]*\n\s+"""(.*?)"""',
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "no attribute docstring under CalendarEvent.impact"
    return match.group(1)


def test_the_documented_values_are_exactly_what_the_feed_emits() -> None:
    """Criterion 1, tied to the authority rather than to a copy of it.

    Asserting the four spellings as literals would pass just as well against a
    docstring that had drifted from `IMPACT_LEVELS`, which is the defect. This
    compares the two, so adding a value to the feed's list without telling the
    contract fails here, and so does the reverse.
    """
    documented = set(QUOTED.findall(_impact_doc()))

    assert documented == set(IMPACT_LEVELS), (documented, IMPACT_LEVELS)


def test_the_documented_values_carry_the_feeds_capitalisation() -> None:
    """Criterion 1, the half the test above cannot fail on its own.

    If `IMPACT_LEVELS` itself were ever lower-cased, the comparison above would
    still hold while both drifted together. This pins the case against the
    literal spelling the feed sends, which is what a consumer types.
    """
    documented = set(QUOTED.findall(_impact_doc()))

    assert "High" in documented, documented
    assert "Holiday" in documented, documented
    assert "high" not in documented, documented


def test_the_docstring_says_a_holiday_is_a_closure_and_not_a_release() -> None:
    """Criterion 2, and the reason the omission was the expensive half.

    A reader who meets ``Holiday`` as a fourth severity will sort it below
    ``Low`` and trade into a thin book. `IMPACT_SEVERITY` deliberately leaves
    it out for that reason, and the contract has to say why it is absent.
    """
    doc = _impact_doc().lower()

    assert "closure" in doc, doc
    assert "release" in doc, doc


def test_the_docstring_names_the_authority_on_the_vocabulary() -> None:
    """Criterion 3. One home for the vocabulary, not two.

    The field lists the values because a reader of the shared vocabulary needs
    to know ``Holiday`` exists without leaving the file. It names
    `IMPACT_LEVELS` because that is where a new value lands when the feed adds
    one, and a reader checking whether the list is current needs somewhere to
    look.
    """
    assert "fbe.datasources.calendar.IMPACT_LEVELS" in _impact_doc()


def test_the_docstring_does_not_restate_the_blackout_policy() -> None:
    """Criterion 4. Which levels block a trade is not this field's business.

    `BLACKOUT_IMPACTS` is the policy and it is one value wide today. Restating
    it here would create a second copy, and the drift would be silent in the
    dangerous direction: a policy widened there and not here leaves the
    contract telling a reader that fewer events block than actually do.
    """
    doc = _impact_doc().lower()

    assert "blackout" not in doc, doc
    assert "blackout_impacts" not in doc, doc


def test_the_superseded_spelling_does_not_survive() -> None:
    """The three lower-case values are gone, not merely joined by four others.

    Leaving the old sentence above the new one would pass every test here and
    leave the contract offering both vocabularies, and a reader picks whichever
    they already believed.
    """
    assert SUPERSEDED not in _impact_doc()
