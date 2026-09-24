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
import math
import statistics
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import (
    CONTRARIAN_CAP,
    CONTRARIAN_SLOPE,
    MOMENTUM_PEAK_Z,
    SIGN_FLIP_Z,
    PositioningPillar,
)
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


NORMAL_SPREAD = 0.5884755850860
"""The exact spread of `PositioningPillar.response` under a standard normal ``p``.

Not a simulated estimate. ``response`` is deterministic and piecewise linear and
``p`` is standard normal, so the spread is a definite integral with a closed
form, and the digits here are the value of that integral rather than the centre
of a confidence interval.

Recomputed in this container two independent ways before it was written down:
piecewise Simpson at 200,000 intervals per smooth piece plus the exact ``erfc``
tail gives ``0.5884755850860195``, and a one-pass trapezoid over the half line
at 4,000,000 intervals gives ``0.5884755850850196``. They agree to eleven
digits, and the one-pass figure is the worse of the two for the reason the split
exists: the integrand has corners at ``|p| = 1, 2, 10/3`` and quadrature that
straddles them converges badly, which is what made the fourth decimal look like
noise.
"""

NORMAL_SPREAD_TOLERANCE = 5e-6
"""How far `spread_under_a_normal_p` may sit from `NORMAL_SPREAD`.

It is not a quadrature bound. At `QUADRATURE_INTERVALS` the measured error is
about 3.5e-14, eight orders tighter, so this is a generous bound rather than a
tight one.

What decides it is the other side: it has to be small enough that moving any of
the four shape constants fails the test. The tightest of those is
`MOMENTUM_PEAK_Z`, where a 1% move shifts the spread by 8.2e-5, sixteen times
this tolerance. `test_moving_any_shape_constant_breaks_the_published_spread`
measures all four rather than leaving that as a claim.

The figure it replaced was 5e-3, which a simulation of 200,000 draws supported
and which was wide enough to pass with `CONTRARIAN_CAP` at 2.05.
"""

QUADRATURE_INTERVALS = 1000
"""Simpson intervals per smooth piece.

Convergence measured here: 100 intervals give an error of 3.9e-10, 500 give
6.2e-13 and 1000 give 3.5e-14, after which it is at the floating-point floor.
1000 costs about 1.6 milliseconds against the 200,000 normal draws this
replaced, and leaves eight orders of margin under `NORMAL_SPREAD_TOLERANCE`.
"""


def _standard_normal_pdf(value: float) -> float:
    """The standard normal density at ``value``."""
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def spread_under_a_normal_p(
    response: Callable[[float], float],
    *,
    peak: float,
    flip: float,
    slope: float,
    cap: float,
) -> float:
    """Return the exact standard deviation of ``response(p)`` for standard normal ``p``.

    Args:
        response: The response function to integrate. The live test passes
            `PositioningPillar.response` so the production function is what is
            measured; the sensitivity test passes a local one built from moved
            constants.
        peak: Where the momentum branch ends, `MOMENTUM_PEAK_Z`.
        flip: Where the response crosses zero, `SIGN_FLIP_Z`.
        slope: The contrarian branch's slope, `CONTRARIAN_SLOPE`.
        cap: Where the contrarian branch saturates, `CONTRARIAN_CAP`.

    Returns:
        ``sd = sqrt( 2 * integral from 0 to inf of f(p)^2 * phi(p) dp )``.
        The mean is zero because ``f`` is odd, so the second moment is the
        variance and no mean term is needed.

    The integral is split at ``peak``, ``flip`` and the saturation point
    ``flip + cap / slope``. Those are the corners of a piecewise-linear
    function, and Simpson's rule assumes a smooth integrand, so a pass that
    straddles a corner converges badly.

    Measured here, the split buys between one and four orders of accuracy
    depending on resolution: at 10 intervals per piece the split form errs by
    3.9e-6 and the unsplit by 3.0e-2, and at `QUADRATURE_INTERVALS` it is
    3.5e-14 against 9.9e-13. So at this resolution both forms clear
    `NORMAL_SPREAD_TOLERANCE` and the split is not what makes the test pass.
    What it buys is the last digits of `NORMAL_SPREAD` being right, and a cheap
    interval count being enough. Removing it is caught by nothing here, which
    is recorded rather than papered over.

    Beyond saturation ``f^2`` is the constant ``cap ** 2``, so that piece is
    ``cap ** 2`` times the normal tail mass and closes in ``erfc`` with no
    quadrature at all. `math.erfc` is standard library, so this adds no
    dependency, which the issue's fourth criterion requires.
    """

    def piece(lower: float, upper: float) -> float:
        width = (upper - lower) / QUADRATURE_INTERVALS
        total = response(lower) ** 2 * _standard_normal_pdf(lower) + response(
            upper
        ) ** 2 * _standard_normal_pdf(upper)
        for index in range(1, QUADRATURE_INTERVALS):
            point = lower + index * width
            weight = 4.0 if index % 2 else 2.0
            total += weight * response(point) ** 2 * _standard_normal_pdf(point)
        return total * width / 3.0

    saturation = flip + cap / slope
    knots = (0.0, peak, flip, saturation)
    smooth = sum(
        piece(lower, upper) for lower, upper in zip(knots, knots[1:], strict=False)
    )
    tail = cap**2 * 0.5 * math.erfc(saturation / math.sqrt(2.0))
    return math.sqrt(2.0 * (smooth + tail))


def test_the_positioning_response_spread_under_a_normal_p() -> None:
    """The 0.5885 the spec publishes, computed rather than sampled.

    This was a seeded Monte Carlo over 200,000 draws asserting three decimals,
    with a docstring calling the fourth decimal the precision "simulation
    actually supports". That is true of simulation and not of the quantity.
    `response` is deterministic and ``p`` is standard normal, so the spread has
    a closed form and the fourth decimal is 5, exactly.

    Integrating `PositioningPillar.response` itself rather than a local copy,
    so a change to the function or to any constant it reads moves this.
    """
    computed = spread_under_a_normal_p(
        PositioningPillar.response,
        peak=MOMENTUM_PEAK_Z,
        flip=SIGN_FLIP_Z,
        slope=CONTRARIAN_SLOPE,
        cap=CONTRARIAN_CAP,
    )

    assert computed == pytest.approx(NORMAL_SPREAD, abs=NORMAL_SPREAD_TOLERANCE)


@pytest.mark.parametrize(
    "constant",
    ["MOMENTUM_PEAK_Z", "SIGN_FLIP_Z", "CONTRARIAN_SLOPE", "CONTRARIAN_CAP"],
)
def test_moving_any_shape_constant_breaks_the_published_spread(constant: str) -> None:
    """The issue's third criterion, measured rather than assumed.

    The tolerance above is only worth having if it is tight enough to catch a
    change to the shape of the response. The figure it replaced was not: at
    5e-3 the test passed with `CONTRARIAN_CAP` at 2.05, so it pinned that
    somebody had once run a simulation rather than pinning the shape.

    Each constant is moved 1% and the spread recomputed from a local response
    built on the moved value. A parametrised case per constant rather than one
    test over four, so a failure names which one stopped mattering.
    """
    moved = {
        "MOMENTUM_PEAK_Z": MOMENTUM_PEAK_Z,
        "SIGN_FLIP_Z": SIGN_FLIP_Z,
        "CONTRARIAN_SLOPE": CONTRARIAN_SLOPE,
        "CONTRARIAN_CAP": CONTRARIAN_CAP,
    }
    moved[constant] *= 1.01

    def response(value: float) -> float:
        magnitude = abs(value)
        sign = 1.0 if value >= 0 else -1.0
        if magnitude <= moved["MOMENTUM_PEAK_Z"]:
            return value
        if magnitude <= moved["SIGN_FLIP_Z"]:
            return sign * (moved["SIGN_FLIP_Z"] - magnitude)
        excess = moved["CONTRARIAN_SLOPE"] * (magnitude - moved["SIGN_FLIP_Z"])
        return -sign * min(excess, moved["CONTRARIAN_CAP"])

    computed = spread_under_a_normal_p(
        response,
        peak=moved["MOMENTUM_PEAK_Z"],
        flip=moved["SIGN_FLIP_Z"],
        slope=moved["CONTRARIAN_SLOPE"],
        cap=moved["CONTRARIAN_CAP"],
    )

    assert abs(computed - NORMAL_SPREAD) > NORMAL_SPREAD_TOLERANCE, (
        f"{constant} moved 1% and the spread shifted only "
        f"{abs(computed - NORMAL_SPREAD):.2e}, inside the tolerance"
    )


def test_the_local_response_matches_the_pillars_when_nothing_is_moved() -> None:
    """The reimplementation above is only evidence if it starts out identical.

    `test_moving_any_shape_constant_breaks_the_published_spread` builds its own
    response so it can move a constant the module does not. That copy could
    drift from `PositioningPillar.response`, and then it would be measuring the
    sensitivity of something the engine does not compute. Checked across every
    branch and both joins.
    """

    def response(value: float) -> float:
        magnitude = abs(value)
        sign = 1.0 if value >= 0 else -1.0
        if magnitude <= MOMENTUM_PEAK_Z:
            return value
        if magnitude <= SIGN_FLIP_Z:
            return sign * (SIGN_FLIP_Z - magnitude)
        excess = CONTRARIAN_SLOPE * (magnitude - SIGN_FLIP_Z)
        return -sign * min(excess, CONTRARIAN_CAP)

    probes = [-4.0, -3.5, -10 / 3, -2.5, -2.0, -1.5, -1.0, -0.5, 0.0]
    probes += [-value for value in probes]

    for probe in probes:
        assert response(probe) == pytest.approx(
            PositioningPillar.response(probe), abs=1e-15
        ), probe


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
