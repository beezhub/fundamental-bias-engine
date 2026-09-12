"""The model's operative loading on headline CPI, which two pillars both carry.

`InflationPillar` loads positively on ``cpi_yoy`` through ``cpi_gap``.
`MonetaryPillar` loads negatively on the same series through
``real_policy_rate``, which is ``policy_rate - cpi_yoy``. The two partly cancel,
so neither pillar's declared weight is the model's response to an inflation
print. ADR 0003 rules that the cancellation stays and the loadings get published.
This module is the test that ruling asks for.

The inputs are the cross-sectional dispersions the worked example in section 7 of
``docs/scoring-spec.md`` publishes, so the arithmetic here is exact rather than
approximate and every assertion is an equality at the published precision. When
the fixture changes, recompute the figures and update the spec. Do not widen a
tolerance: a tolerance wide enough to absorb a sub-weight edit is wide enough to
hide the defect this file exists to catch.

Nothing here measures whether the model predicts anything. These are loadings of
the model on its own inputs, not results about returns.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fbe.config import ScoringConfig
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.scoring import series_loading
from fbe.types import PillarName
from fbe.universe import G10

SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

# Cross-sectional dispersions from the worked example, sections 7.1 and 7.2 of
# docs/scoring-spec.md. Invented but plausible inputs for one run, not a claim
# about any actual print.
SD_HEADLINE_GAP = 0.5362
"""Section 7.2: standard deviation of the headline deviation from target."""
SD_CORE_GAP = 0.4867
"""Section 7.2: standard deviation of the core deviation from target."""
SD_REAL_POLICY_RATE = 1.1233
"""Section 7.1: standard deviation of ``policy_rate - cpi_yoy``."""
INFLATION_BLEND_SD = 0.9711
"""Section 7.2: ``sd(blend)`` for INFLATION, the ``run_local`` divisor."""
MONETARY_BLEND_SD = 0.7001
"""Section 7.1: ``sd(blend)`` for MONETARY, the ``run_local`` divisor."""

PUBLISHED_HEADLINE_LOADING = 0.1008
PUBLISHED_CORE_LOADING = 0.1666
PUBLISHED_REAL_POLICY_RATE_LOADING = -0.0501
PUBLISHED_HEADLINE_ALONE_CANCELLATION = 49.7
PUBLISHED_HEADLINE_AND_CORE_CANCELLATION = 18.7


def _headline_loading(config: ScoringConfig | None = None) -> float:
    """INFLATION's composite loading per 1pp of headline CPI, on the fixture."""
    scoring = config or ScoringConfig()
    return series_loading(
        pillar_weight=scoring.weights[PillarName.INFLATION],
        sub_weight=InflationPillar().component_weights["cpi_gap"],
        sub_indicator_sd=SD_HEADLINE_GAP,
        blend_divisor=INFLATION_BLEND_SD,
        universe_size=len(G10),
    )


def _core_loading(config: ScoringConfig | None = None) -> float:
    """INFLATION's composite loading per 1pp of core CPI, on the fixture.

    Read as part of the headline response only under the assumption the spec
    states, that core moves one for one with headline. A headline-only shock
    does not move this term at all.
    """
    scoring = config or ScoringConfig()
    return series_loading(
        pillar_weight=scoring.weights[PillarName.INFLATION],
        sub_weight=InflationPillar().component_weights["core_gap"],
        sub_indicator_sd=SD_CORE_GAP,
        blend_divisor=INFLATION_BLEND_SD,
        universe_size=len(G10),
    )


def _real_policy_rate_loading(config: ScoringConfig | None = None) -> float:
    """MONETARY's composite loading per 1pp of headline CPI, on the fixture.

    Negative, because ``real_policy_rate`` is ``policy_rate - cpi_yoy`` and the
    series therefore enters with a coefficient of minus one.
    """
    scoring = config or ScoringConfig()
    return series_loading(
        pillar_weight=scoring.weights[PillarName.MONETARY],
        sub_weight=MonetaryPillar().component_weights["real_policy_rate"],
        sub_indicator_sd=SD_REAL_POLICY_RATE,
        blend_divisor=MONETARY_BLEND_SD,
        universe_size=len(G10),
        coefficient=-1.0,
    )


def _section(heading: str) -> str:
    """Return the body of one ``###`` section of the scoring spec."""
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(
        rf"^### {re.escape(heading)}.*?(?=^### |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"section {heading!r} is missing from {SPEC}"
    return match.group(0)


# --- the three loadings, reproduced from the fixture -------------------------


def test_headline_cpi_loading_reproduces_the_published_figure() -> None:
    """INFLATION moves the composite +0.1008 per 1pp of headline CPI.

    That is 0.15 of pillar weight times 0.40 of sub-weight, divided by the
    headline dispersion and by the blend divisor, and taken through the 7/8
    factor that applies because a currency moving on its own also moves the
    cross-sectional mean it is measured against.
    """
    assert round(_headline_loading(), 4) == PUBLISHED_HEADLINE_LOADING


def test_core_cpi_loading_reproduces_the_published_figure() -> None:
    """The core sub-indicator carries +0.1666 per 1pp, larger than headline.

    Core holds 0.60 of the pillar against headline's 0.40 and disperses less
    across the universe, and both of those raise the loading.
    """
    assert round(_core_loading(), 4) == PUBLISHED_CORE_LOADING


def test_real_policy_rate_loading_reproduces_the_published_figure() -> None:
    """MONETARY moves the composite -0.0501 per 1pp of headline CPI.

    The sign is the point. A pillar the config declares at 0.30, whose
    sub-weight table never names inflation, holds a negative loading on the
    series the 0.15 pillar is built from.
    """
    assert round(_real_policy_rate_loading(), 4) == PUBLISHED_REAL_POLICY_RATE_LOADING


def test_headline_alone_cancellation_reproduces_the_published_share() -> None:
    """A headline-only shock loses 49.7% of INFLATION's response to MONETARY.

    This is the worse of the two cases because the core term does not fire, so
    the opposing loading is measured against headline's +0.1008 alone.
    """
    same_sign = _headline_loading()
    opposing = _real_policy_rate_loading()

    cancelled = -opposing / same_sign * 100.0

    assert round(cancelled, 1) == PUBLISHED_HEADLINE_ALONE_CANCELLATION


def test_headline_and_core_cancellation_reproduces_the_published_share() -> None:
    """A broad shock loses 18.7%, because the core term is not opposed at all."""
    same_sign = _headline_loading() + _core_loading()
    opposing = _real_policy_rate_loading()

    cancelled = -opposing / same_sign * 100.0

    assert round(cancelled, 1) == PUBLISHED_HEADLINE_AND_CORE_CANCELLATION


def test_the_two_loadings_carry_opposite_signs() -> None:
    """The invariant behind the whole file, stated without a magnitude.

    If an edit ever makes these agree in sign, the cancellation figures stop
    meaning what the spec says they mean, and this states that in one line
    rather than leaving it implied by two rounded equalities.
    """
    assert _headline_loading() > 0.0
    assert _real_policy_rate_loading() < 0.0


# --- the wire, not the value ------------------------------------------------


def test_the_loading_moves_with_the_configured_pillar_weight() -> None:
    """Doubling INFLATION's weight doubles its loading.

    The published figures must come from the config rather than from a constant
    that happens to match it. The weights in this override do not sum to 1.0 and
    do not need to: nothing is aggregated here, one term's arithmetic is.
    """
    doubled = ScoringConfig(
        weights={**ScoringConfig().weights, PillarName.INFLATION: 0.30}
    )

    assert _headline_loading(doubled) == pytest.approx(_headline_loading() * 2.0)


def test_the_loading_moves_with_the_configured_sub_weight() -> None:
    """A sub-weight edit changes the loading proportionally.

    Sub-weights live on the pillar rather than in ``ScoringConfig``, so this is
    the arm of the wire a re-weighting proposal would actually touch. The 0.20
    here is the figure section 10 item 2 names for equal weighting inside
    MONETARY, which would enlarge the cancellation rather than reduce it.
    """
    scoring = ScoringConfig()
    at_declared = series_loading(
        pillar_weight=scoring.weights[PillarName.MONETARY],
        sub_weight=0.15,
        sub_indicator_sd=SD_REAL_POLICY_RATE,
        blend_divisor=MONETARY_BLEND_SD,
        universe_size=len(G10),
        coefficient=-1.0,
    )
    at_equal_weighting = series_loading(
        pillar_weight=scoring.weights[PillarName.MONETARY],
        sub_weight=0.20,
        sub_indicator_sd=SD_REAL_POLICY_RATE,
        blend_divisor=MONETARY_BLEND_SD,
        universe_size=len(G10),
        coefficient=-1.0,
    )

    assert at_equal_weighting == pytest.approx(at_declared * (0.20 / 0.15))
    assert abs(at_equal_weighting) > abs(at_declared)


def test_the_loading_falls_as_the_cross_section_disperses() -> None:
    """A wider cross-section on a series reduces what one unit of it is worth.

    This is why the figures are fixture-specific and must be recomputed rather
    than absorbed into a tolerance.
    """
    narrow = series_loading(
        pillar_weight=0.15,
        sub_weight=0.40,
        sub_indicator_sd=SD_HEADLINE_GAP,
        blend_divisor=INFLATION_BLEND_SD,
        universe_size=len(G10),
    )
    wide = series_loading(
        pillar_weight=0.15,
        sub_weight=0.40,
        sub_indicator_sd=SD_HEADLINE_GAP * 2.0,
        blend_divisor=INFLATION_BLEND_SD,
        universe_size=len(G10),
    )

    assert wide == pytest.approx(narrow / 2.0)


# --- absence raises, it does not default -------------------------------------


@pytest.mark.parametrize("sd", [0.0, -0.5])
def test_a_degenerate_cross_section_raises_rather_than_defaulting(sd: float) -> None:
    """Zero dispersion has no loading, and inventing one would be a fabrication.

    Every currency printing the same number means the z-score is undefined, not
    that the series carries no weight.
    """
    with pytest.raises(ValueError, match="sub_indicator_sd"):
        series_loading(
            pillar_weight=0.15,
            sub_weight=0.40,
            sub_indicator_sd=sd,
            blend_divisor=INFLATION_BLEND_SD,
            universe_size=len(G10),
        )


def test_a_non_positive_blend_divisor_raises_rather_than_defaulting() -> None:
    """A divisor of zero means the pillar's own components cancelled outright."""
    with pytest.raises(ValueError, match="blend_divisor"):
        series_loading(
            pillar_weight=0.15,
            sub_weight=0.40,
            sub_indicator_sd=SD_HEADLINE_GAP,
            blend_divisor=0.0,
            universe_size=len(G10),
        )


@pytest.mark.parametrize("size", [0, 1])
def test_a_universe_too_small_to_normalise_raises(size: int) -> None:
    """One currency has no cross-section, so it has no cross-sectional loading."""
    with pytest.raises(ValueError, match="universe_size"):
        series_loading(
            pillar_weight=0.15,
            sub_weight=0.40,
            sub_indicator_sd=SD_HEADLINE_GAP,
            blend_divisor=INFLATION_BLEND_SD,
            universe_size=size,
        )


# --- the spec publishes what the code computes -------------------------------


def test_spec_section_3_1_publishes_the_real_policy_rate_loading() -> None:
    """MONETARY's declared 0.30 must sit next to its loading on headline CPI.

    A reader who sees the sub-weight table and not this figure cannot tell that
    the heaviest pillar in the model is partly an inflation pillar with the
    opposite sign to INFLATION.
    """
    section = _section("3.1 MONETARY (weight 0.30)")

    assert f"{_real_policy_rate_loading():+.4f}" in section


def test_spec_section_3_2_publishes_the_loadings_and_both_cancellations() -> None:
    """INFLATION's declared 0.15 must sit next to the model's net response.

    Every cell of both published tables, so a reader can tell which case they
    are in rather than inferring one number from the weight, and so a worked
    table that stops adding up fails the build.
    """
    section = _section("3.2 INFLATION (weight 0.15)")
    headline = _headline_loading()
    core = _core_loading()
    opposing = _real_policy_rate_loading()

    assert f"{headline:+.4f}" in section
    assert f"{core:+.4f}" in section
    assert f"{opposing:+.4f}" in section
    assert f"{headline + core:+.4f}" in section
    assert f"{headline + opposing:+.4f}" in section
    assert f"{headline + core + opposing:+.4f}" in section
    assert f"{-opposing / headline * 100.0:.1f}%" in section
    assert f"{-opposing / (headline + core) * 100.0:.1f}%" in section
