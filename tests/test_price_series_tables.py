"""`FRED_RISK_SERIES` and `FRED_COMMODITY_SERIES`: what is live and what is held.

Both mappings in `fbe.datasources.prices` are records of identifiers confirmed
live on FRED. Neither is imported by anything, and most of what they list is
routed by nothing: the registry holds its own refs and is what fetches.

`FRED_RISK_SERIES` used to say the opposite in the direction that costs work.
It called six of its seven entries "corroboration" and warned that a risk
pillar reading only VIX "will call every equity wobble a crisis", which reads
as a pillar starved of inputs. `docs/scoring-spec.md` section 3.7 specifies the
opposite: ``R`` is a two-term sum over ``world_equity_index`` and ``vol_index``
at 0.50 each, and a third input has no slot. A reader acting on the docstring
would register six indicators feeding a pillar that cannot read them, and
`registry.coverage_report` and `registry.stale_refs` would then report on
indicators nothing scores.

So the tests here pin the split between routed and held, and tie the prose to
the code it describes rather than to a phrase. A docstring naming a key is
checked against `RiskPillar.requires`, and one naming an identifier against the
registry, so renaming either fails here rather than leaving the file confidently
wrong. Ruled on issue #302.

Nothing here reaches the network: the registry and both mappings are read as
data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import fbe
from fbe.datasources.prices import FRED_COMMODITY_SERIES, FRED_RISK_SERIES
from fbe.datasources.registry import INDICATORS, coverage_report
from fbe.pillars.risk import RiskPillar

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "docs" / "scoring-spec.md"
PRICES = Path(fbe.__file__).resolve().parent / "datasources" / "prices.py"

ROUTED_RISK_IDENTIFIER = "VIXCLS"
"""The one entry of `FRED_RISK_SERIES` the registry routes, under ``vol_index``."""

ROUTED_COMMODITY_IDENTIFIERS = frozenset({"PALLFNFINDEXM", "DCOILWTICO", "PIORECRUSDM"})
"""The three entries of `FRED_COMMODITY_SERIES` the registry routes.

All three sit under the single key ``commodity_price``, which is asserted
rather than assumed: three identifiers under three separate keys would be a
different arrangement with the same count.
"""


HELD_RISK_IDENTIFIERS = frozenset(
    {"VXEEMCLS", "BAMLH0A0HYM2", "T10Y2Y", "T10YIE", "STLFSI4", "DTWEXBGS"}
)
"""The six entries of `FRED_RISK_SERIES` the registry does not route."""

HELD_COMMODITY_IDENTIFIERS = frozenset(
    {
        "PNRGINDEXM",
        "PMETAINDEXM",
        "PFOODINDEXM",
        "DCOILBRENTEU",
        "PCOPPUSDM",
        "PCOALAUUSDM",
    }
)
"""The six entries of `FRED_COMMODITY_SERIES` the registry does not route.

Written out rather than counted. A count fails the same way whichever entry
was registered, and the point of the fifth criterion is to name the one that
moved.
"""


def _registry_identifiers() -> dict[str, set[str]]:
    """Map each FRED identifier the registry routes to the keys routing it."""
    routed: dict[str, set[str]] = {}
    for key, spec in INDICATORS.items():
        for ref in spec.series.values():
            routed.setdefault(ref.series_id, set()).add(key)
    return routed


def _offsets(haystack: str, needle: str) -> list[int]:
    """Every start offset of ``needle``, not just the first.

    `str.index` finds one, and the binding below has to be judged against the
    closest pair rather than an arbitrary one: ``VIXCLS`` appears twice in the
    risk docstring for two different reasons.
    """
    return [match.start() for match in re.finditer(re.escape(needle), haystack)]


def _docstring(mapping_name: str) -> str:
    """The attribute docstring for one of the two mappings.

    Read out of the source because a docstring on a module-level assignment is
    not kept at runtime, unlike a function's.
    """
    source = PRICES.read_text()
    start = source.index(f"{mapping_name}: Mapping[str, str] = {{")
    opening = source.index('"""', source.index("}", start))
    closing = source.index('"""', opening + 3)
    return source[opening + 3 : closing]


# --- the mappings themselves -------------------------------------------------


@pytest.mark.parametrize(
    "mapping",
    [FRED_RISK_SERIES, FRED_COMMODITY_SERIES],
    ids=["risk", "commodity"],
)
def test_every_identifier_is_a_non_empty_string(mapping: dict[str, str]) -> None:
    """The sixth criterion's first half.

    An empty identifier would reach a FRED URL and come back as an error the
    caller would read as a dead series rather than as a malformed request.
    """
    for key, identifier in mapping.items():
        assert isinstance(identifier, str), key
        assert identifier.strip() == identifier, key
        assert identifier, key


def test_the_vix_entry_is_the_identifier_the_registry_routes() -> None:
    """The sixth criterion's second half: the one live overlap cannot drift.

    ``vix`` and the registry's ``vol_index`` both name ``VIXCLS`` today, in two
    files that nothing forces to agree. This is the single point where the two
    can disagree, so it is the one worth pinning.
    """
    ref = INDICATORS["vol_index"].series["GLOBAL"]

    assert FRED_RISK_SERIES["vix"] == ref.series_id
    assert ref.source == "fred"


# --- what is routed and what is held ----------------------------------------


def test_only_one_risk_identifier_is_routed() -> None:
    """The split the corrected docstring describes, asserted from the registry.

    Asserted as the identity of the routed entry rather than as a count, so
    registering a different one of the six does not pass by arithmetic.
    """
    routed = _registry_identifiers()
    live = {value for value in FRED_RISK_SERIES.values() if value in routed}

    assert live == {ROUTED_RISK_IDENTIFIER}
    assert routed[ROUTED_RISK_IDENTIFIER] == {"vol_index"}


def test_the_three_routed_commodity_identifiers_share_one_key() -> None:
    """The fourth criterion's factual half, including the shared key."""
    routed = _registry_identifiers()
    live = {value for value in FRED_COMMODITY_SERIES.values() if value in routed}

    assert live == set(ROUTED_COMMODITY_IDENTIFIERS)
    for identifier in ROUTED_COMMODITY_IDENTIFIERS:
        assert routed[identifier] == {"commodity_price"}


def test_no_held_identifier_has_been_registered() -> None:
    """The fifth criterion, as the property that would actually break it.

    "`coverage_report()` returns the same figures before and after" cannot be
    asserted across a diff by a test that runs on one side of it. What can be
    asserted is the thing that would have to change first: every entry these
    two mappings hold and the registry does not route is absent from the
    registry entirely. Register one and this fails, which is the moment
    `coverage_report` would start reporting on an indicator nothing scores.
    """
    routed = _registry_identifiers()
    held = {
        value
        for mapping in (FRED_RISK_SERIES, FRED_COMMODITY_SERIES)
        for value in mapping.values()
    } - set(routed)

    assert held == HELD_RISK_IDENTIFIERS | HELD_COMMODITY_IDENTIFIERS


def test_coverage_report_answers_for_the_registry_and_not_for_these_tables() -> None:
    """The other half of the fifth criterion: what the report is a report of.

    `coverage_report` iterates `INDICATORS`, so its key set is the registry's.
    Nothing these mappings hold can reach it without being registered first,
    which is what the test above forbids silently.
    """
    assert set(coverage_report()) == set(INDICATORS)


# --- the docstrings, tied to the code rather than to a phrase ---------------


def test_the_risk_docstring_names_every_key_the_pillar_requires() -> None:
    """The second criterion, tied to `RiskPillar` so a rename fails here.

    Checked against `requires` rather than against a written pair of names, so
    renaming a key or adding a third leaves this failing rather than leaving
    the docstring quietly describing a pillar that no longer exists.
    """
    doc = _docstring("FRED_RISK_SERIES")

    assert set(RiskPillar.requires) == {"world_equity_index", "vol_index"}
    for key in RiskPillar.requires:
        assert key in doc, key


def test_the_risk_docstring_cites_a_section_that_exists() -> None:
    """The second criterion's other half. A citation to nowhere is the same defect.

    The old text asserted a consequence for the model with nothing to check it
    against. The replacement points at the specification, so the pointer is
    checked: both that the docstring makes it and that the section is there.
    """
    doc = _docstring("FRED_RISK_SERIES")

    assert "section 3.7" in doc
    assert "docs/scoring-spec.md" in doc
    assert "### 3.7 RISK" in SPEC.read_text()


def test_each_docstring_names_the_identifiers_its_registry_routes() -> None:
    """The third and fourth criteria, tied to the registry rather than to prose."""
    risk = _docstring("FRED_RISK_SERIES")
    commodity = _docstring("FRED_COMMODITY_SERIES")

    assert ROUTED_RISK_IDENTIFIER in risk
    for identifier in ROUTED_COMMODITY_IDENTIFIERS:
        assert identifier in commodity, identifier


KEY_TO_IDENTIFIER_CHARS = 60
"""How far apart the routed key and its identifier may sit, once whitespace is
normalised to single spaces.

Measured, not chosen: in the text as written they are 16 characters apart, and
the nearest other mention of ``VIXCLS``, in the sentence about what the pillar
requires, is 170 away. 60 leaves room to reword the binding sentence and still
fails when it is deleted, which is the case this exists for.
"""


def test_the_risk_docstring_binds_the_routed_key_to_its_identifier() -> None:
    """The third criterion, which presence alone does not establish.

    ``VIXCLS`` appears twice in that docstring: once saying what the pillar
    requires, once saying which entry of this table is live. Asserting only
    that the string is present passes when the second sentence is deleted,
    which was measured rather than assumed: removing it left every other test
    here green. A reader would then be told the pillar reads ``VIXCLS`` and
    never told which of the seven rows is the one that reaches it.

    So the key and its identifier are required near each other, which is what
    a binding sentence looks like and what two unrelated mentions do not.
    """
    doc = " ".join(_docstring("FRED_RISK_SERIES").split())
    key = "``vix``"
    identifier = f"``{ROUTED_RISK_IDENTIFIER}``"

    keys = _offsets(doc, key)
    identifiers = _offsets(doc, identifier)

    assert keys, f"{key} is not in the docstring"
    assert identifiers, f"{identifier} is not in the docstring"

    gap = min(abs(a - b) for a in keys for b in identifiers)
    assert gap <= KEY_TO_IDENTIFIER_CHARS, (
        f"{key} and {identifier} are {gap} characters apart at their closest, "
        "so the docstring no longer says which entry of this table the "
        "registry routes"
    )


def test_neither_docstring_says_the_pillar_is_short_of_inputs() -> None:
    """The first criterion, against the two claims the issue quotes.

    A prose assertion is normally the wrong instrument, since it fails on an
    innocent rewording and passes on a differently worded falsehood. It is
    warranted for these two: they are the exact sentences #302 was filed
    against, and restoring either would restore the wasted work the issue
    describes. The tests above are what guard the claim generally.
    """
    both = _docstring("FRED_RISK_SERIES") + _docstring("FRED_COMMODITY_SERIES")
    lowered = both.lower()

    assert "reads only vix" not in lowered
    assert "equity wobble" not in lowered
    assert "corroboration" not in lowered


@pytest.mark.parametrize(
    "forbidden", ["proven", "backtested", "win rate", "improves the", "edge"]
)
def test_neither_docstring_claims_an_unmeasured_improvement(forbidden: str) -> None:
    """The seventh criterion, and the standing instruction in CLAUDE.md.

    Nothing in this repository has been measured against out-of-sample returns,
    so a docstring may not say more inputs would make the pillar better. The
    corrected text says the open question belongs to a proposal, which is a
    statement about process rather than about performance.
    """
    both = _docstring("FRED_RISK_SERIES") + _docstring("FRED_COMMODITY_SERIES")

    assert forbidden not in both.lower()
