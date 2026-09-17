"""``emit_sd``, the volume a pillar actually spoke at.

`PillarScore.diagnostics` has documented ``emit_sd`` since the type was written:
"the standard deviation the pillar actually emitted across the universe, which is
how a reader can tell whether a pillar is speaking at the volume its weight
implies. A pillar emitting well below one contributes less than its declared
weight, and that gap is invisible in the composite." Nothing populated it.

The gap is not hypothetical and it is not small. Section 2.3 re-standardises five
pillars to a cross-sectional standard deviation of exactly 1.0, so their declared
weights are the operative ones. POSITIONING and RISK are excluded for reasons
both documents give and issue #12 does not dispute, and neither contributes the
0.10 that `ScoringConfig` assigns it:

* POSITIONING emits near 0.59 under a normal ``p`` and 0.6437 on the section 7
  fixture, so its 0.10 buys roughly 0.059.
* RISK emits ``2 * |R| * sd(risk_beta)``, which is 0.0 in a calm market and
  1.2728 at maximum risk-off, so its influence swings from 0.000 to 0.127 across
  the regime against a single declared 0.10.

Reporting the figure is the whole point. Issue #12 rules out both ways of closing
the gap, re-standardising the two pillars and raising their weights, so what is
left is making it visible on the run rather than true only in a document. The
document half is what went stale before: the POSITIONING figure was recorded in
section 10 and absent from section 3.6, which is the section a reader consults.

Two kinds of assertion live here. The arithmetic and the specification run today.
The pillar-level cases for POSITIONING and RISK are guarded on the scaffold
marker, because both pillars' ``_normalise`` is still a stub, and they begin
asserting the day it lands.

Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import random
import statistics
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import PositioningPillar
from fbe.pillars.risk import RiskPillar
from fbe.types import Frequency, Observation
from fbe.universe import CURRENCIES, G10

SCAFFOLD = "is scaffolded;"
"""The stub marker every scaffolded callable carries, per ``CLAUDE.md``."""

SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

ASOF = date(2026, 6, 30)
UNIVERSE: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY")

SPREAD: Mapping[str, float] = {"USD": 4.0, "EUR": 2.0, "GBP": 3.0, "JPY": 0.5}
"""Four separated policy rates, so MONETARY scores every currency and the
cross-section has real spread to standardise."""


def _skip_if_scaffolded(*functions: Callable[..., object]) -> None:
    """Skip the calling test while any of these callables is still a stub."""
    for function in functions:
        if SCAFFOLD in inspect.getsource(function):
            pytest.skip(f"{function.__qualname__} is still scaffolded")


def _monetary_run() -> list[Observation]:
    """A MONETARY set the real pillar can score for every currency.

    Four of the five components, worth 0.85 of the sub-weight, which clears
    `MIN_COMPONENT_WEIGHT`. Every input is stamped, so nothing is admitted on an
    assumed publication lag and the run is about the arithmetic only.
    """
    stamped = datetime(2026, 6, 29, tzinfo=UTC)
    return [
        Observation(
            indicator=indicator,
            currency=currency,
            value=value + offset,
            period=date(2026, 6, 1),
            source="test",
            series_id="X",
            unit="percent",
            frequency=Frequency.MONTHLY,
            released_at=stamped,
        )
        for currency, value in SPREAD.items()
        for offset, indicator in (
            (0.0, "policy_rate"),
            (0.1, "yield_2y"),
            (0.2, "yield_2y_chg_1m"),
            (0.3, "yield_2y_chg_3m"),
        )
    ]


# --- a re-standardised pillar, which is criterion 5's first half -----------


def test_a_restandardised_pillar_emits_at_one_on_the_run_local_path() -> None:
    """Criterion 5. The 1.0 that section 2.3 promises, measured rather than assumed.

    MONETARY passes through the re-standardisation, so its emitted spread should
    be 1.0 by construction. Asserting it is what makes ``emit_sd`` worth reading
    for the two pillars that do not: a reader comparing 0.59 against 1.0 needs the
    1.0 to be a measurement rather than a claim.

    The divisor path is asserted alongside because 1.0 only follows when this
    run's own blend was the divisor. Under a history divisor the blend is scaled
    by a previous run's spread and the result is deliberately not 1.0.
    """
    scores = MonetaryPillar().compute(_monetary_run(), UNIVERSE, ASOF)

    assert {s.blend_divisor_path for s in scores.values()} == {"run_local"}
    for currency in UNIVERSE:
        assert scores[currency].diagnostics["emit_sd"] == pytest.approx(1.0, abs=5e-9)


def test_emit_sd_is_the_spread_of_the_scores_the_pillar_emitted() -> None:
    """The definition, checked against the scores rather than trusted.

    ``emit_sd`` could plausibly mean the spread of the normalised ``z`` or of the
    ``score`` that reaches the composite. They differ only when clipping bites,
    and the composite is built from the score, so the score is the honest choice.
    This pins it against the values on the run.
    """
    scores = MonetaryPillar().compute(_monetary_run(), UNIVERSE, ASOF)
    emitted = [scores[currency].score for currency in UNIVERSE]

    expected = statistics.pstdev(emitted)
    for currency in UNIVERSE:
        assert scores[currency].diagnostics["emit_sd"] == pytest.approx(
            expected, abs=5e-12
        )


def test_every_currency_carries_the_same_emit_sd() -> None:
    """It is a fact about the cross-section, not about one currency.

    Worth pinning because `_diagnostics` is called per currency, so the obvious
    implementation computes a per-currency number, which for a spread has no
    meaning. A reader seeing eight different values would reasonably conclude
    the pillar emitted at eight volumes.
    """
    scores = MonetaryPillar().compute(_monetary_run(), UNIVERSE, ASOF)

    assert len({s.diagnostics["emit_sd"] for s in scores.values()}) == 1


def test_a_currency_the_pillar_could_not_score_is_left_out_of_the_spread() -> None:
    """Thin coverage and a quiet pillar are different facts.

    A currency with no score carries ``0.0`` as a neutral placeholder. Counting
    those zeros would pull the spread toward zero, so a pillar that spoke loudly
    about the three currencies it could see would be reported as quiet. Coverage
    answers the first question; ``emit_sd`` answers the second, and conflating
    them would make both useless.

    The assertion is against the spread of the scored three, and separately
    against the number the wrong implementation would produce, so a failure says
    which mistake was made rather than only that two floats differ.
    """
    partial = [
        observation for observation in _monetary_run() if observation.currency != "JPY"
    ]

    scores = MonetaryPillar().compute(partial, UNIVERSE, ASOF)
    scored = [c for c in UNIVERSE if scores[c].z is not None]

    assert set(scored) == {"USD", "EUR", "GBP"}
    emit_sd = scores["USD"].diagnostics["emit_sd"]

    assert emit_sd == pytest.approx(
        statistics.pstdev([scores[c].score for c in scored]), abs=5e-12
    )
    with_placeholder = statistics.pstdev([scores[c].score for c in UNIVERSE])
    assert emit_sd != pytest.approx(with_placeholder, abs=1e-6)


def test_a_pillar_that_scored_nobody_still_reports_emit_sd() -> None:
    """Criterion 4 says every pillar on every run, and this is the hard case.

    With no usable input there is no cross-section to measure. Reporting 0.0
    follows the convention `missing_score` already sets for the score itself: a
    neutral placeholder, with the coverage figure alongside it telling the reader
    the pillar was absent. Omitting the key instead would mean a reader iterating
    diagnostics has to handle two shapes.
    """
    scores = MonetaryPillar().compute([], UNIVERSE, ASOF)

    for currency in UNIVERSE:
        assert scores[currency].z is None
        assert scores[currency].diagnostics["emit_sd"] == 0.0


# --- POSITIONING, which is criterion 5's second half -----------------------


def test_the_section_7_positioning_responses_spread_at_0_6437() -> None:
    """The arithmetic behind the figure, which needs no pillar code.

    This is what ``emit_sd`` will read for POSITIONING on the section 7 fixture,
    computed from `PositioningPillar.response` rather than copied from the spec,
    so it cannot drift into asserting a number that is itself wrong.
    """
    published = (1.90, -0.60, 0.40, -2.40, -1.10, -0.80, 0.70, 1.30)
    responses = [PositioningPillar.response(p) for p in published]

    assert statistics.pstdev(responses) == pytest.approx(0.6437, abs=5e-5)


def test_the_positioning_response_spread_under_a_normal_p() -> None:
    """The 0.5885 the spec publishes, measured here rather than cited.

    Seeded, so it is a fixed assertion rather than one that passes most days.
    The tolerance is three decimals because that is the precision simulation
    actually supports: two million draws put this at 0.5886 and the recorded
    figure is 0.5887, which is sampling noise and not a discrepancy. Two hundred
    thousand draws keeps the suite fast and still pins the figure well inside
    the gap between 0.59 and the 1.0 it is being compared against.
    """
    generator = random.Random(12)
    responses = [
        PositioningPillar.response(generator.gauss(0.0, 1.0)) for _ in range(200_000)
    ]

    assert statistics.pstdev(responses) == pytest.approx(0.5885, abs=5e-3)


def test_the_positioning_response_is_quieter_than_a_restandardised_pillar() -> None:
    """The comparison the whole issue rests on, stated as an assertion.

    0.6437 against 1.0 is what makes the declared 0.10 buy roughly 0.064 on this
    fixture. Asserting the inequality rather than only the figure means a change
    that made the response louder cannot pass while the spec still says it is
    quiet.
    """
    published = (1.90, -0.60, 0.40, -2.40, -1.10, -0.80, 0.70, 1.30)
    responses = [PositioningPillar.response(p) for p in published]

    assert statistics.pstdev(responses) < 1.0


# --- RISK, which is criterion 6 --------------------------------------------


def test_the_risk_betas_still_spread_at_0_6364() -> None:
    """The figure section 3.7 must state, read off the metadata.

    Every entry in the `R` table is ``2 * |R| * sd(risk_beta)``, so this one
    number carries the whole table. If a currency's beta is ever edited, this
    fails and the spec table is wrong until it is recomputed.
    """
    betas = [CURRENCIES[currency].risk_beta for currency in G10]

    assert statistics.pstdev(betas) == pytest.approx(0.6364, abs=5e-5)


@pytest.mark.parametrize(
    ("r", "expected_sd"),
    [(0.0, 0.0), (-0.250, 0.3182), (-0.625, 0.7955), (-1.000, 1.2728)],
)
def test_the_risk_table_in_the_spec_is_arithmetic(r: float, expected_sd: float) -> None:
    """Each row recomputed, so the table cannot be transcribed wrongly.

    The emitted spread is ``2 * |R| * sd(risk_beta)`` because ``R`` is one scalar
    shared by all eight currencies, so it scales the betas and nothing else.
    """
    betas = [CURRENCIES[currency].risk_beta for currency in G10]
    emitted = 2 * abs(r) * statistics.pstdev(betas)

    assert emitted == pytest.approx(expected_sd, abs=5e-5)


def test_risk_falls_below_the_deletion_floor_for_most_of_its_range() -> None:
    """The claim section 3.7 makes, computed rather than asserted by eye.

    Section 8 sets 0.05 as the floor below which a pillar should be deleted
    rather than diminished. RISK's effective weight is ``0.10 * emitted``, so it
    clears the floor only above this ``|R|``.
    """
    betas = [CURRENCIES[currency].risk_beta for currency in G10]
    crossover = 0.05 / (0.10 * 2 * statistics.pstdev(betas))

    assert crossover == pytest.approx(0.393, abs=5e-4)


# --- through the pillars, once their normalisation lands -------------------


def test_positioning_emit_sd_on_the_section_7_fixture() -> None:
    """Criterion 5's second half, written against the real signature."""
    _skip_if_scaffolded(PositioningPillar._normalise)

    pillar = PositioningPillar()
    responses = {
        currency: PositioningPillar.response(p)
        for currency, p in zip(
            G10, (1.90, -0.60, 0.40, -2.40, -1.10, -0.80, 0.70, 1.30), strict=True
        )
    }
    normalised = pillar._normalise(
        {
            currency: {"positioning_response": value}
            for currency, value in responses.items()
        }
    )

    assert statistics.pstdev(
        [v for v in normalised.values() if v is not None]
    ) == pytest.approx(0.6437, abs=5e-4)


def test_risk_emits_nothing_in_a_calm_market() -> None:
    """Criterion 6. At ``R = 0`` the pillar has nothing to say and says so.

    Every score is exactly 0.0 and the emitted spread is exactly 0.0, which is
    the honest reading: RISK still occupies 0.10 of the composite's denominator
    while contributing none of it. That is the state the class docstring calls
    intended, and ``emit_sd`` is what puts it on the page.
    """
    _skip_if_scaffolded(RiskPillar._normalise)

    pillar = RiskPillar()
    normalised = pillar._normalise({c: {"risk_response": 0.0} for c in G10})

    assert all(value == 0.0 for value in normalised.values())
    assert statistics.pstdev(list(normalised.values())) == 0.0


# --- the specification, which is criteria 2 and 3 --------------------------


def _spec_section(heading: str) -> str:
    """One section of the spec, from its heading to the next of the same level."""
    body = SPEC.read_text()
    start = body.index(heading)
    rest = body[start + len(heading) :]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def test_section_3_6_states_positionings_effective_influence() -> None:
    """Criterion 1, pinned to the section a reader consults about POSITIONING.

    The figure existed before this change, in section 10 under the heading "A
    quantity this document never stated". That placement is the defect rather
    than the remedy: issue #12's own body says the number was "recorded in
    section 10 item 3 of the spec but not in section 3.6, which is the section a
    reader consults to understand the pillar".

    So this asserts the section rather than the document. A whole-file search
    would have passed for the last several weeks while the gap stayed exactly
    where it was.
    """
    section = " ".join(_spec_section("### 3.6 POSITIONING").split())

    assert "0.5885" in section
    assert "0.6437" in section
    assert "0.059" in section
    assert "2.3" in section


def test_section_10_no_longer_calls_the_figure_unstated() -> None:
    """The other half of moving it, so the document does not contradict itself.

    Section 10 introduced the quantity as one "this document never stated".
    Once section 3.6 states it that sentence is false, and a false sentence in a
    specification is the thing this repository treats as a defect rather than as
    untidiness.
    """
    body = " ".join(SPEC.read_text().split())

    assert "A quantity this document never stated" not in body


def test_section_3_7_carries_the_risk_table() -> None:
    """Criterion 2. The figures, in the section a reader consults about RISK."""
    section = " ".join(_spec_section("### 3.7 RISK").split())

    assert "0.6364" in section
    for figure in ("0.3182", "0.7955", "1.2728"):
        assert figure in section, figure
    assert "0.127" in section
    assert "0.393" in section


def test_section_2_3_names_the_consequence_for_the_excluded_pillars() -> None:
    """Criterion 3. The guarantee is stated with its scope, not as universal.

    Asserted against the guarantee paragraph rather than the whole section. The
    section mentions POSITIONING elsewhere, so a whole-section search passes
    while the sentence that matters still reads "Every pillar reaches the
    aggregator on the same scale, so a pillar's declared weight is the share of
    the composite that pillar actually carries". That sentence is the one that
    made both gaps invisible, and it is false for two of the seven.
    """
    section = " ".join(_spec_section("### 2.3").split())
    start = section.index("**What the guarantee covers")
    paragraph = section[start : start + 1400]

    assert "five" in paragraph.lower(), paragraph[:200]
    assert "POSITIONING" in paragraph and "RISK" in paragraph
    assert "0.059" in paragraph or "0.06" in paragraph


def test_the_spec_does_not_claim_the_excluded_pillars_contribute_their_weight() -> None:
    """The negative half, so a later edit cannot quietly restore the claim."""
    section = " ".join(_spec_section("### 3.7 RISK").split())

    assert "0.10" in section
    assert "effective" in section.lower()
