"""``types.py`` states the operative definition of the three derived quantities.

`CurrencyScore.dispersion`, `CurrencyScore.coverage` and `PairBias.agreement`
are computed by `fbe.scoring` and `fbe.bias`, specified in
``docs/scoring-spec.md`` sections 4.2, 4.4 and 5.3, and described in
`src/fbe/types.py`, which is the shared vocabulary every other module imports.
For a while the first two agreed and the third did not.

The gap was not cosmetic. Commit ``aa6705d`` moved dispersion to an
effective-weighted standard deviation about the composite and agreement to a
share of pair weight with ties excluded. ``types.py`` kept describing the
superseded unweighted definitions, so a consumer reproducing the arithmetic from
the contract would compute a different number from the one the engine publishes
and compare it against a threshold calibrated for the other. On the scenario in
the last three tests the two readings of dispersion are 1.071214 and 0.832993,
against a `ScoringConfig.max_dispersion` of 1.20, and the two readings of
agreement are 0.30 and 0.142857, against a `ScoringConfig.min_agreement` of
0.60.

So the tests come in two halves and they are different kinds of test. The
wording tests pin what the contract says. The worked-number tests pin what the
code does, and pin that the superseded reading is a genuinely different number,
which is what stops a later reader making the wording true by changing the
arithmetic instead. Pinning only the words would leave that door open, and
pinning only the numbers would let the contract drift again.

The wording is transcribed from `ScoringConfig.max_dispersion`, `min_coverage`
and `min_agreement` rather than composed afresh. Those went through review on
#50 and #71, and a fourth phrasing of the same fact is how this defect started.

Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import MISSING, fields
from datetime import date
from statistics import pstdev

from fbe import bias, scoring
from fbe.config import ScoringConfig
from fbe.types import CurrencyScore, PairBias, PillarName, PillarScore

ASOF = date(2026, 1, 15)

SUPERSEDED = {
    "dispersion": "Standard deviation across pillar scores",
    "coverage": "Fraction of pillar weight that had usable data",
    "agreement": "Fraction of pillars pointing the same way",
}
"""The exact phrases ``types.py`` carried, kept so the tests and the issue look
at the same characters rather than at two approximations of them."""


def _attribute_docstring(cls: type, name: str) -> str:
    """Return the docstring written under ``name`` in ``cls``'s source.

    Read out of the source rather than from ``__doc__``, because an attribute
    docstring on a dataclass field is not kept at runtime. This is the helper
    `tests/test_agreement_wording.py` uses on `ScoringConfig`, generalised to
    take the class and the field, since three fields across two classes are
    wanted here.
    """
    source = inspect.getsource(cls)
    match = re.search(
        rf'^\s+{name}:[^\n]*\n\s+"""(.*?)"""',
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"no attribute docstring under {cls.__name__}.{name}"
    return match.group(1)


def _dispersion_doc() -> str:
    return _attribute_docstring(CurrencyScore, "dispersion")


def _coverage_doc() -> str:
    return _attribute_docstring(CurrencyScore, "coverage")


def _agreement_doc() -> str:
    return _attribute_docstring(PairBias, "agreement")


def test_dispersion_names_the_weighted_quantity_and_its_section() -> None:
    """Criterion 1. The #50 shape, which `max_dispersion` already follows.

    The deviation is taken about the composite rather than about the unweighted
    mean of the scores, and saying only "standard deviation" leaves the reader
    to assume the mean, which is the assumption that produced 0.832993 where the
    engine publishes 1.071214.
    """
    doc = _dispersion_doc().lower()

    assert "effective-weighted standard deviation" in doc, doc
    assert "composite" in doc, doc
    assert "4.4" in doc, doc


def test_dispersion_says_the_weights_are_post_staleness() -> None:
    """Criterion 1, the second half.

    `PillarScore.weight` means the configured weight before the staleness
    penalty and the effective weight after it, and its own docstring says
    anything reproducing the arithmetic must say which stage its inputs came
    from. A reader holding configured weights gets a different number on every
    currency carrying old data.
    """
    doc = _dispersion_doc().lower()

    assert "post-staleness" in doc, doc


def test_coverage_says_it_is_a_sum_of_effective_weights() -> None:
    """Criterion 2. The freshness discount is the part the old wording dropped.

    "Fraction of pillar weight that had usable data" is true of a count of
    present pillars, which is what a reader would reasonably build from it. The
    quantity is ``sum over p of w_eff(p)``, and a pillar on a 30-day-old input
    contributes half its weight rather than all or none of it.
    """
    doc = _coverage_doc().lower()

    assert "effective pillar weights" in doc, doc
    assert "staleness discount" in doc, doc
    assert "4.2" in doc, doc


def test_coverage_says_it_is_continuous_rather_than_a_count() -> None:
    """Criterion 2, the second half, and the reason the first half matters.

    Without it "fraction of pillar weight" still reads as a tally of the
    pillars that ran. The distinction decides whether 0.60 is a threshold on
    four pillars out of seven or on the weight they carry after ageing.
    """
    doc = _coverage_doc().lower()

    assert "continuous" in doc, doc
    assert "not the fraction of pillars" in doc or "rather than a count" in doc, doc


def test_agreement_says_it_is_a_share_of_pillar_weight() -> None:
    """Criterion 3. The phrase #71 settled, reused verbatim.

    #153 chose "share of pillar weight" for the renderers and `min_agreement`
    because this field had not landed yet. This is the field those three were
    supposed to be reading from, so it repeats the phrase rather than inventing
    a fourth.
    """
    doc = _agreement_doc().lower()

    assert "share of pillar weight" in doc, doc
    assert "5.3" in doc, doc


def test_agreement_says_plainly_that_it_is_not_a_count_of_pillars() -> None:
    """Criterion 3. Saying only what it is leaves the wrong inference available.

    The same assertion `ScoringConfig.min_agreement` carries, for the same
    reason: on the spec's USDJPY case the headcount is 57% and the weight share
    is 0.70, and a reader who meets the field here first should not have to find
    the renderer to learn which one they are holding.
    """
    doc = _agreement_doc().lower()

    assert "not a count" in doc or "not the fraction of pillars" in doc, doc


def test_agreement_says_ties_are_excluded_from_both_sides() -> None:
    """Criterion 3. The exclusion changes the denominator, not just the numerator.

    A pillar scoring the two legs identically expresses no opinion on this pair.
    Leaving it in the denominator would make a thin run look like a disputed
    one, which is the reading the old wording supported.
    """
    doc = _agreement_doc().lower()

    assert "identically" in doc, doc
    assert "numerator" in doc and "denominator" in doc, doc


def test_agreement_says_the_weights_are_post_staleness() -> None:
    """Criterion 3. ``w_pair`` is built from `PillarScore.weight`.

    Same trap as dispersion: handed unpenalised weights the arithmetic still
    produces a plausible number, computed as though every pillar were fresh.
    """
    doc = _agreement_doc().lower()

    assert "post-staleness" in doc, doc


def test_no_superseded_phrase_survives_in_types() -> None:
    """Criteria 1 to 3, from the other side.

    Adding the correct definition beside the old one would pass every test
    above and leave the contract saying both things. That is worse than saying
    one wrong thing, because a reader picks whichever they already believed.
    """
    source = (inspect.getsource(CurrencyScore) + inspect.getsource(PairBias)).lower()

    for field_name, phrase in SUPERSEDED.items():
        assert phrase.lower() not in source, f"{field_name} still carries: {phrase}"


def test_no_field_was_added_removed_or_redefaulted() -> None:
    """Criterion 4. This is a docstring change and nothing else.

    `CLAUDE.md` calls a change to a field in ``types.py`` a breaking change that
    updates every consumer in the same commit. This pull request updates no
    consumer, so it has to be able to show it changed no field. Defaults are
    compared as well as names, because a moved default is the quiet kind.
    """
    currency_score = [
        (f.name, "" if f.default is MISSING else repr(f.default))
        for f in fields(CurrencyScore)
    ]
    pair_bias = [
        (f.name, "" if f.default is MISSING else repr(f.default))
        for f in fields(PairBias)
    ]

    assert currency_score == [
        ("currency", ""),
        ("composite", ""),
        ("pillars", ""),
        ("asof", ""),
        ("rank", "None"),
        ("dispersion", "0.0"),
        ("coverage", "1.0"),
    ]
    assert pair_bias == [
        ("pair", ""),
        ("base", ""),
        ("quote", ""),
        ("spread", ""),
        ("direction", ""),
        ("conviction", ""),
        ("asof", ""),
        ("base_score", "0.0"),
        ("quote_score", "0.0"),
        ("agreement", "0.0"),
        ("tradeable", "True"),
        ("blockers", ""),
    ]


def _weights() -> dict[PillarName, float]:
    return dict(ScoringConfig().weights)


def _pillar(name: PillarName, currency: str, score: float) -> PillarScore:
    """One fresh pillar carrying its configured weight.

    ``weight`` is the configured weight, which for a fresh pillar is also the
    effective weight, so these scores are already past
    `scoring.apply_staleness_penalty` in the sense `bias.agreement` requires.
    """
    return PillarScore(
        pillar=name,
        currency=currency,
        raw=score,
        z=score,
        score=score,
        weight=_weights()[name],
        asof=ASOF,
    )


def _leg(currency: str, scores: dict[PillarName, float]) -> CurrencyScore:
    """A currency scored on all seven pillars, built through the real functions.

    ``composite``, ``dispersion`` and ``coverage`` come from `fbe.scoring`
    rather than from literals, so the fixture cannot drift away from the code
    it is here to pin.
    """
    weights = _weights()
    pillars = {name: _pillar(name, currency, scores[name]) for name in weights}
    return CurrencyScore(
        currency=currency,
        composite=scoring.composite(pillars, weights),
        pillars=pillars,
        asof=ASOF,
        dispersion=scoring.dispersion(pillars, weights),
        coverage=scoring.coverage(pillars, weights),
    )


def _dispersion_case() -> CurrencyScore:
    """MONETARY at +2.0, INFLATION at -1.0, the other five at 0.0, all fresh.

    The scenario from the issue body. Composite is
    ``0.30*2.0 + 0.15*(-1.0) = 0.45``.
    """
    scores = {name: 0.0 for name in _weights()}
    scores[PillarName.MONETARY] = 2.0
    scores[PillarName.INFLATION] = -1.0
    return _leg("USD", scores)


def test_dispersion_is_the_weighted_figure_the_contract_now_describes() -> None:
    """Criterion 5, dispersion half.

    The issue body gives 1.0705 for this case and the triage comment of
    2026-09-23 repeats it. Both are wrong: the figure is 1.071214, as the
    comment of 2026-09-11 already recorded. It is pinned here from
    `scoring.dispersion` rather than transcribed from either.
    """
    leg = _dispersion_case()

    assert round(leg.composite, 6) == 0.45
    assert round(leg.coverage, 6) == 1.0
    assert round(leg.dispersion, 6) == 1.071214


def test_the_superseded_reading_of_dispersion_is_a_different_number() -> None:
    """Criterion 5, and the test that stops the wording being made true.

    "Standard deviation across pillar scores" is 0.832993 on this case against
    the 1.071214 the engine publishes. The gap is 0.238221, which is 19.9% of
    the 1.20 `max_dispersion` threshold the larger figure is calibrated
    against. The issue body puts that gap at 11%, computed from its own two
    wrong figures.

    A later change that made the contract's old wording accurate by switching
    the arithmetic would fail here, which is the point: the weighting is what
    carries the fact that MONETARY at 0.30 disagreeing matters more than
    POSITIONING at 0.10 disagreeing.
    """
    leg = _dispersion_case()
    unweighted = pstdev(pillar.score for pillar in leg.pillars.values())

    assert round(unweighted, 6) == 0.832993
    assert leg.dispersion > unweighted
    assert leg.dispersion - unweighted > 0.15 * ScoringConfig().max_dispersion


def test_agreement_is_a_weight_share_and_not_the_headcount() -> None:
    """Criterion 5, agreement half, on the case in the issue body.

    MONETARY at 0.30 agrees with the headline and the other six at 0.70 do not,
    so the weight share is 0.30 and the headcount is one in seven. Both sit
    below `min_agreement`, so the cap fires either way, but the figure the
    report prints differs by a factor of more than two and the contract has to
    say which one it is.

    MONETARY is at +3.0 rather than +1.0 so that the spread stays positive:
    ``0.30*3.0 - 0.70*1.0 = +0.20``. At +1.0 the six outvote it, the headline
    flips, and the pillar that agrees is the other six.
    """
    weights = _weights()
    base = _leg(
        "USD",
        {name: (3.0 if name is PillarName.MONETARY else -1.0) for name in weights},
    )
    quote = _leg("JPY", {name: 0.0 for name in weights})

    spread = base.composite - quote.composite
    headcount = 1 / len(weights)

    assert round(spread, 6) == 0.2
    assert round(bias.agreement(base, quote), 6) == 0.3
    assert round(headcount, 6) == 0.142857
    assert bias.agreement(base, quote) > 2 * headcount
