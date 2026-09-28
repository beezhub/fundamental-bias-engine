"""`required_indicators` is a seam with no caller, and must stay honest about it.

`fbe.pillars.required_indicators` used to say "the runner uses this to decide
what to fetch". It does not. `fbe.datasources.collect.collect` defaults its
``indicators`` argument to every key in `fbe.datasources.registry.INDICATORS`,
and ``fbe refresh`` passes nothing, so a refresh fetches the whole registry.
The helper had no caller at all.

The claim was wrong in the dangerous direction. It warned that an indicator a
pillar forgets to declare "will not be there at compute time", when the
registry-wide fetch supplies it regardless, so the failure it described cannot
happen by that route. A reader taking it at face value stops looking for what
does catch a pillar requiring a key the registry does not route, which is
`tests/test_registry_pillar_agreement.py`.

Two things are pinned here, and the second is why #301 was worth a change
rather than a note.

The helper must keep returning the union of ``pillar.requires``, because
nothing in production calls it and a helper with no caller drifts unnoticed.

`collect`'s default must keep being the whole registry. Narrowing it to this
helper's list is the improvement a later reader will reach for on finding a
function that computes the fetch list beside a caller that ignores it. Taking
it would stop fetching the sixteen series behind ``yield_10y`` and
``equity_index``, which are registered but unread precisely so each stays cheap
to re-adopt. The third unconsumed key, ``pmi_composite``, is manual on all
eight legs and no fetch reaches it either way.

Nothing here reaches the network. `collect` is read, never run.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import fbe
from fbe.datasources.collect import collect
from fbe.datasources.registry import INDICATORS, UNCONSUMED_INDICATORS
from fbe.pillars import default_pillars, required_indicators

REPO = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(inspect.getfile(fbe)).resolve().parent
DEFINITION_SITE = PACKAGE_ROOT / "pillars" / "__init__.py"


def test_it_returns_the_union_of_what_the_default_pillars_require() -> None:
    """The fifth criterion. Nothing in production calls this to keep it true.

    The union is recomputed from `default_pillars` here rather than compared
    against a literal list of twenty keys, so adding a pillar or moving an
    indicator between two of them does not fail this for the wrong reason. The
    count is asserted separately, below.
    """
    union: set[str] = set()
    for pillar in default_pillars():
        union.update(pillar.requires)

    assert required_indicators() == tuple(sorted(union))


def test_the_union_stands_at_twenty_keys() -> None:
    """The figure #301 quotes, pinned on its own so its failure reads plainly.

    Twenty is not a rule, it is the count on the day. This failing means the
    pillars now require a different number of indicators, which is a legitimate
    thing to do and a thing worth noticing: the gap against the registry below
    is what actually has to hold.
    """
    assert len(required_indicators()) == 20


def test_it_is_sorted_and_free_of_duplicates() -> None:
    """The return contract, which several pillars sharing one key would break."""
    keys = required_indicators()

    assert list(keys) == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_it_takes_the_pillars_it_is_given() -> None:
    """The argument is the point of the seam, so it is exercised.

    A helper whose only tested path is its default is one refactor away from
    ignoring its argument, and the argument is what makes it usable as
    ``collect(indicators=required_indicators(some_pillars))`` the day someone
    wants a narrowed fetch for a single pillar.
    """
    one = default_pillars()[:1]

    assert required_indicators(one) == tuple(sorted(set(one[0].requires)))
    assert set(required_indicators(one)) < set(required_indicators())


def test_the_pillars_require_fewer_keys_than_the_registry_holds() -> None:
    """The gap the corrected docstring describes, and exactly what sits in it.

    Asserted as a relationship rather than as 23 against 20, so registering a
    new indicator does not fail this by arithmetic alone. What must hold is
    that the difference is exactly the held-ready set: a key in neither is an
    orphan, and `tests/test_registry_pillar_agreement.py` is what catches that.
    """
    required = set(required_indicators())

    assert set(INDICATORS) - required == set(UNCONSUMED_INDICATORS)
    assert required < set(INDICATORS)


def test_collect_still_defaults_to_the_whole_registry() -> None:
    """The sixth criterion, and the one that earns this change its label.

    Read out of the source rather than by calling `collect`, which fetches.

    A later reader who finds a helper computing the fetch list beside a
    ``collect`` that ignores it has an obvious-looking improvement in front of
    them. Taking it narrows the fetch to the pillars' current ``requires`` and
    stops exercising the fetch path behind ``yield_10y`` and ``equity_index``,
    sixteen fetchable series that are registered but unread so each stays cheap
    to re-adopt. It also splits `fbe.datasources.collect.CollectionResult.gaps`,
    filled from `registry.stale_refs` and reporting on the whole registry
    regardless of what the run asked for, away from the set the run actually
    fetched. ``pmi_composite``, the third unconsumed key, is manual on all
    eight legs, so no fetch reaches it either way.
    """
    source = inspect.getsource(collect)

    assert "tuple(INDICATORS) if indicators is None" in source, (
        "collect no longer defaults its fetch list to the whole registry. "
        "If that was deliberate, read fbe.pillars.required_indicators first. "
        "Narrowing the fetch stops requesting UNCONSUMED_INDICATORS, which are "
        "registered but unread so each stays cheap to re-adopt, and it leaves "
        "CollectionResult.gaps reporting on indicators the run never asked "
        "for. Ruled on #301."
    )


def test_the_guard_the_docstring_sends_readers_to_still_exists() -> None:
    """The docstring names a file, and a named file can be renamed.

    Criterion 3 of #301 is that the docstring points at what actually guards a
    pillar requiring a key the registry does not route. A pointer to a file
    that no longer exists is the same defect as the wrong sentence it replaced,
    so the reference is checked rather than trusted.
    """
    named = "tests/test_registry_pillar_agreement.py"
    doc = required_indicators.__doc__ or ""

    assert named in doc, f"the docstring no longer names {named}"
    assert (REPO / named).is_file(), (
        f"fbe.pillars.required_indicators sends readers to {named}, which is "
        "not there. Point it at whatever replaced that test."
    )


def test_nothing_in_the_package_calls_the_helper_yet() -> None:
    """Pins the premise the docstring rests on, so the docstring cannot go stale.

    The docstring says this has no production caller and explains why that is
    deliberate. The day something calls it, that paragraph needs rewriting, and
    this says so rather than leaving a confident and wrong explanation behind.

    Only the definition site is exempt. The name in ``__all__`` is a string
    with no call parenthesis, so it does not match.
    """
    callers = sorted(
        str(path.relative_to(PACKAGE_ROOT))
        for path in PACKAGE_ROOT.rglob("*.py")
        if path.resolve() != DEFINITION_SITE
        and "required_indicators(" in path.read_text()
    )

    assert not callers, (
        f"required_indicators now has callers under src/fbe/ ({callers}). "
        "Its docstring explains why it has none and needs updating, and so "
        "does this test."
    )
