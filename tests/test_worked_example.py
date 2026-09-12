"""Section 7 of ``docs/scoring-spec.md`` as an executable fixture.

Section 9 of the spec lists this as an obligation: "The worked example in section
7 reproduces to two decimal places. It is a fixture, not an illustration." Until
this file existed, nothing compared the two. Commit ``5415b9d`` records what that
costs: section 2.3 and `blend_divisor` had diverged, the divergence survived
because nothing compared them, and the worked example had to be recomputed from
its raw inputs to establish whether any published figure had moved.

**A failure here means the specification and the code have diverged, and both are
suspects.** Do not assume the document is right and patch the code, and do not
assume the code is right and edit the document. Work out which one moved, from
the commit that moved it. The one thing that must never happen is a published
figure adjusted until an assertion passes: a fixture quietly fitted to the code
is worse than no fixture, because it then certifies whatever the code does.

Every number here is transcribed from section 7 and from nowhere else, with its
subsection beside it and a label into `SPEC_ANCHORS`, which carries the line and
is itself checked. Nothing is computed and then written down.

Two kinds of assertion live here, and they fail for different reasons.

The first kind runs today. It checks the document against itself and against the
committed configuration: that the z-score tables follow from the raw inputs
above them, that the pillar matrix and `ScoringConfig.weights` produce the
published composites, that the sub-weights the example blends with are the ones
the pillar classes return, and that the currency metadata it quotes is the
metadata in `fbe.universe`. This is the half that catches the regression the
issue named, someone moving a sub-weight or a clip and silently invalidating the
worked example, and it catches it now rather than when the engine is finished.

The second kind is guarded on the scaffold marker and skips. `fbe.scoring` is
six stubs and `fbe.bias` is five, so the published figures cannot yet be put
through the functions that are supposed to produce them. Those assertions start
biting on the day each layer lands, which is the day they are most needed.

The issue asked for ``pytest.importorskip`` on `fbe.pillars.base`, `fbe.scoring`
and `fbe.bias`. That does not skip anything: all three modules import cleanly and
it is their contents that raise. The guard here matches on the scaffold message
instead, which is the same test ``tests/test_stubs.py`` enforces and is precise
about which callable is missing.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path

import pytest

from fbe.config import ScoringConfig
from fbe.pillars.base import BasePillar
from fbe.pillars.employment import EmploymentPillar
from fbe.pillars.external import ExternalPillar
from fbe.pillars.growth import GrowthPillar
from fbe.pillars.inflation import InflationPillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.pillars.positioning import PositioningPillar
from fbe.types import Conviction, CurrencyScore, Direction, PillarName, PillarScore
from fbe.universe import ALL_PAIRS, G10, meta, split_pair

ASOF = date(2026, 6, 30)
"""Run date for the objects the guarded assertions build.

Section 7 never dates itself, so this is the suite's fixed as-of from
``tests/conftest.py`` rather than anything transcribed. Nothing in the example
depends on it: the staleness the example does use is carried explicitly by
`NZD_STALE_DAYS`.
"""

SCAFFOLD = "is scaffolded;"
"""The stub marker every scaffolded callable carries, per ``CLAUDE.md``."""


def _skip_if_scaffolded(*functions: Callable[..., object]) -> None:
    """Skip the calling test while any of these callables is still a stub."""
    for function in functions:
        if SCAFFOLD in inspect.getsource(function):
            pytest.skip(f"{function.__qualname__} is still scaffolded")


SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

SPEC_ANCHORS: Mapping[str, tuple[int, str]] = {
    "7.1 raw inputs": (869, "| Currency | `policy_rate` % |"),
    "7.1 z-scores": (884, "| `policy_rate` | 2.7250 | 1.5236 |"),
    "7.1 sub-weights": (
        890,
        "Blending with sub-weights 0.15 / 0.25 / 0.20 / 0.25 / 0.15",
    ),
    "7.1 usd blend": (892, "0.15*(+1.165) + 0.25*(+1.104)"),
    "7.1 blend sd": (909, "sd(blend) = 0.7001"),
    "7.1 monetary scores": (924, "| MONETARY score |"),
    "7.2 targets": (934, "Targets come from `CurrencyMeta.inflation_target`"),
    "7.2 deviations": (937, "| Currency | `cpi_yoy` | headline deviation |"),
    "7.2 stats": (954, "Headline deviation: mean +0.4500, sd 0.5362."),
    "7.2 sub-weights": (955, "Blending 0.40 headline and 0.60 core"),
    "7.3 growth inputs": (983, "| Currency | `gdp_yoy` | `pmi_composite` |"),
    "7.3 scores": (1007, "| Pillar | `sd(blend)` | scaling |"),
    "7.4 positioning": (1018, "| Currency | net non-commercial, % of OI |"),
    "7.4 jpy": (1033, "computed as `-(-1) * 1.5 * (2.40 - 2.00) = +0.60`"),
    "7.4 saturation": (
        1036,
        "No currency in this run reaches the contrarian saturation",
    ),
    "7.5 components": (1045, "dd_component  = clip(-6.5 / 10.0)"),
    "7.5 risk scores": (1051, "| Currency | `risk_beta` | RISK score |"),
    "7.6 matrix": (1064, "| Currency | MON 0.30 | INF 0.15 |"),
    "7.6 nzd staleness": (
        1089,
        "**NZD composite**, demonstrating the staleness discount.",
    ),
    "7.6 nzd dispersion": (1112, "**NZD dispersion**, with `w_tilde(p)"),
    "7.6 no demotion": (1124, "Under the 1.20 threshold, so no dispersion demotion."),
    "7.7 nzdusd": (1134, "**NZDUSD**"),
    "7.7 nzdusd agreement": (1151, "considered = 0.300+0.150+0.150+0.100+0.075"),
    "7.7 nzdusd cost": (1164, "expected_move = 62 * sqrt(10)"),
    "7.7 usdjpy": (1178, "**USDJPY**"),
    "7.7 eurcad": (1223, "For a clean NONE in the same run, take EURCAD"),
    "7.7 nzdjpy": (1228, "**NZDJPY**"),
    "7.7 nzdjpy cost": (1236, "so `expected_move = 88 * 3.1623 = 278.28`"),
    "7.8 shortlist": (1264, "Shortlist for the run: NZDUSD short at MEDIUM"),
}
"""Where each transcribed table lives, as ``{label: (line, text on that line)}``.

The issue asked for a line reference beside every constant. A bare line number
is the wrong tool: section 6 grew by 29 lines the same morning this file was
written and every reference in it silently became wrong, pointing at real lines
with different content, which is worse than pointing at nothing.

So the references are labels into this table, the table carries the line number
and a snippet of what should be on it, and
``test_every_spec_anchor_points_at_what_it_claims`` checks all thirty. A line
that drifts fails with the label and the true line number, which is a one-line
correction rather than a hunt.
"""


# --- transcribed from section 7 ---------------------------------------------

UNIVERSE: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")
"""Column order of every table in section 7. Not `universe.G10`'s order by luck:
``test_the_section_7_column_order_is_the_g10_order`` pins that they agree."""

# 7.1, raw inputs. Anchor "7.1 raw inputs".
MONETARY_RAW: Mapping[str, tuple[float, ...]] = {
    "policy_rate": (4.50, 2.50, 4.25, 0.50, 0.25, 3.00, 4.10, 2.70),
    "yield_2y": (4.10, 2.20, 4.05, 0.85, 0.30, 2.85, 3.95, 2.50),
    "chg_1m": (18.0, -5.0, 12.0, 22.0, -3.0, -10.0, 8.0, -16.0),
    "chg_3m": (35.0, -12.0, 20.0, 45.0, -8.0, -25.0, 15.0, -32.0),
    "real_policy_rate": (1.60, 0.40, 1.05, -2.30, -0.35, 0.80, 0.70, 0.80),
}

# 7.1, cross-sectional statistics. Anchor "7.1 z-scores".
# ``{key: (mean, sd)}``.
MONETARY_STATS: Mapping[str, tuple[float, float]] = {
    "policy_rate": (2.7250, 1.5236),
    "yield_2y": (2.6000, 1.3583),
    "chg_1m": (3.2500, 12.8525),
    "chg_3m": (4.7500, 26.3427),
    "real_policy_rate": (0.3375, 1.1233),
}

# 7.1, component z-scores, published to three decimals. Anchor "7.1 z-scores".
MONETARY_Z: Mapping[str, tuple[float, ...]] = {
    "policy_rate": (1.165, -0.148, 1.001, -1.460, -1.624, 0.180, 0.902, -0.016),
    "yield_2y": (1.104, -0.294, 1.068, -1.288, -1.693, 0.184, 0.994, -0.074),
    "chg_1m": (1.148, -0.642, 0.681, 1.459, -0.486, -1.031, 0.370, -1.498),
    "chg_3m": (1.148, -0.636, 0.579, 1.528, -0.484, -1.129, 0.389, -1.395),
    "real_policy_rate": (1.124, 0.056, 0.634, -2.348, -0.612, 0.412, 0.323, 0.412),
}

# 7.1, the run_local divisor, this run's own sd(blend). Anchor "7.1 blend sd".
MONETARY_BLEND_SD = 0.7001

# 7.1, the worked USD blend. Anchor "7.1 usd blend".
USD_MONETARY_BLEND = 1.13595

# 7.2, targets, also `CurrencyMeta.inflation_target`. Anchor "7.2 targets".
INFLATION_TARGETS: Mapping[str, float] = {
    "USD": 2.0,
    "EUR": 2.0,
    "GBP": 2.0,
    "JPY": 2.0,
    "CHF": 1.0,
    "CAD": 2.0,
    "AUD": 2.5,
    "NZD": 2.0,
}

# 7.2, deviations. Anchor "7.2 deviations". ``{currency: (headline, core)}``.
INFLATION_DEVIATIONS: Mapping[str, tuple[float, float]] = {
    "USD": (0.9, 1.1),
    "EUR": (0.1, 0.4),
    "GBP": (1.2, 1.6),
    "JPY": (0.8, 0.5),
    "CHF": (-0.4, -0.1),
    "CAD": (0.2, 0.5),
    "AUD": (0.9, 0.7),
    "NZD": (-0.1, 0.3),
}

# 7.2, deviation statistics. Anchor "7.2 stats".
INFLATION_STATS: Mapping[str, tuple[float, float]] = {
    "headline": (0.4500, 0.5362),
    "core": (0.6250, 0.4867),
}
INFLATION_BLEND_SD = 0.9711

# 7.3, growth inputs. Anchor "7.3 growth inputs".
GROWTH_RAW: Mapping[str, tuple[float, ...]] = {
    "gdp_yoy": (2.4, 0.9, 1.1, 0.6, 1.3, 1.6, 1.8, 0.1),
    "pmi_composite": (53.1, 49.8, 51.2, 50.4, 48.9, 50.9, 51.8, 47.6),
    "indpro_yoy": (1.2, -1.8, -0.6, 0.4, 2.1, 0.9, 1.4, -1.1),
    "retail_sales_yoy": (3.2, 1.1, 1.9, 1.4, 0.8, 2.2, 2.6, 0.2),
}

# 7.3, per-pillar blend spreads. Anchor "7.3 scores". ``{pillar: sd(blend)}``.
BLEND_SD: Mapping[str, float] = {
    "MONETARY": MONETARY_BLEND_SD,
    "INFLATION": INFLATION_BLEND_SD,
    "GROWTH": 0.8907,
    "EMPLOYMENT": 0.8450,
    "EXTERNAL": 0.7669,
}

# 7.4. Anchor "7.4 positioning".
# ``{currency: (net_pct_oi, mean_3y, sd_3y, p, f_of_p)}``.
POSITIONING: Mapping[str, tuple[float, float, float, float, float]] = {
    "USD": (26.2, 9.8, 8.63, 1.90, 0.10),
    "EUR": (-2.4, 3.6, 10.00, -0.60, -0.60),
    "GBP": (7.9, 3.7, 10.50, 0.40, 0.40),
    "JPY": (-31.5, -6.3, 10.50, -2.40, 0.60),
    "CHF": (-14.1, -2.0, 11.00, -1.10, -0.90),
    "CAD": (-12.5, -4.5, 10.00, -0.80, -0.80),
    "AUD": (5.4, -1.6, 10.00, 0.70, 0.70),
    "NZD": (18.4, 4.1, 11.00, 1.30, 0.70),
}

# 7.5. Anchor "7.5 components".
EQUITY_DRAWDOWN_PCT = -6.5
VOL_SIGMA = 1.2
RISK_R = -0.625

# 7.5. Anchor "7.5 risk scores". ``{currency: (risk_beta, risk_score)}``.
RISK_SCORES: Mapping[str, tuple[float, float]] = {
    "USD": (-0.5, 0.62),
    "EUR": (0.1, -0.12),
    "GBP": (0.3, -0.38),
    "JPY": (-0.9, 1.12),
    "CHF": (-0.7, 0.88),
    "CAD": (0.4, -0.50),
    "AUD": (0.9, -1.12),
    "NZD": (0.8, -1.00),
}

# 7.6, the pillar matrix. Anchor "7.6 matrix". Columns match PILLAR_ORDER.
PILLAR_ORDER: tuple[PillarName, ...] = (
    PillarName.MONETARY,
    PillarName.INFLATION,
    PillarName.GROWTH,
    PillarName.EMPLOYMENT,
    PillarName.EXTERNAL,
    PillarName.POSITIONING,
    PillarName.RISK,
)

MATRIX: Mapping[str, tuple[float, ...]] = {
    "USD": (1.62, 0.95, 1.67, 0.69, -0.78, 0.10, 0.62),
    "AUD": (0.86, 0.44, 0.99, 1.26, 1.36, 0.70, -1.12),
    "GBP": (1.13, 1.81, -0.01, -0.79, -0.51, 0.40, -0.38),
    "JPY": (-0.31, 0.11, -0.38, 0.59, 0.90, 0.60, 1.12),
    "EUR": (-0.54, -0.55, -0.82, 0.54, 0.61, -0.60, -0.12),
    "CAD": (-0.51, -0.35, 0.51, -0.84, -1.45, -0.80, -0.50),
    "CHF": (-1.40, -1.57, -0.19, 0.48, 0.93, -0.90, 0.88),
    "NZD": (-0.87, -0.84, -1.77, -1.93, -1.06, 0.70, -1.00),
}

# 7.6, the same table's right-hand columns. Anchor "7.6 matrix".
# ``{currency: (coverage, composite, dispersion, rank)}``.
CURRENCY_RESULTS: Mapping[str, tuple[float, float, float, int]] = {
    "USD": (1.000, 0.9420, 0.7756, 1),
    "AUD": (1.000, 0.6925, 0.6607, 2),
    "GBP": (1.000, 0.4810, 0.8729, 3),
    "JPY": (1.000, 0.1875, 0.5426, 4),
    "EUR": (1.000, -0.3245, 0.4819, 5),
    "CAD": (1.000, -0.4880, 0.5168, 6),
    "CHF": (1.000, -0.5450, 0.9665, 7),
    "NZD": (0.950, -0.9774, 0.7056, 8),
}

# 7.6, anchor "7.6 nzd staleness". New Zealand's external data is 30 days old, so
# that pillar's effective weight is discounted and coverage falls below 1.0.
NZD_STALE_PILLAR = PillarName.EXTERNAL
NZD_STALE_DAYS = 30
NZD_STALE_PHI = 0.500
NZD_STALE_EFFECTIVE_WEIGHT = 0.050

# 7.7, the three pairs carried to a conclusion, anchors "7.7 nzdusd",
# "7.7 usdjpy" and "7.7 nzdjpy", plus the clean NONE at "7.7 eurcad".
# ``{pair: (spread, direction, conviction, agreement)}``.
PAIRS: Mapping[str, tuple[float, Direction, Conviction, float | None]] = {
    "NZDUSD": (-1.9194, Direction.SHORT, Conviction.MEDIUM, 0.8974),
    "USDJPY": (0.7545, Direction.LONG, Conviction.LOW, 0.7000),
    "NZDJPY": (-1.1649, Direction.SHORT, Conviction.LOW, 0.8974),
    "EURCAD": (0.1635, Direction.NEUTRAL, Conviction.NONE, None),
}

# 7.7, NZDUSD agreement, the only one worked in full.
# Anchor "7.7 nzdusd agreement".
NZDUSD_CONSIDERED = 0.975
NZDUSD_AGREEING = 0.875
NZDUSD_EXTERNAL_PAIR_WEIGHT = 0.075

# 7.7, cost filter. Anchors "7.7 nzdusd cost" and "7.7 nzdjpy cost".
# ``{pair: (spread_pips, atr_20d_pips, cost_ratio)}``.
COSTS: Mapping[str, tuple[float, float, float]] = {
    "NZDUSD": (1.8, 62.0, 0.0092),
    "NZDJPY": (3.2, 88.0, 0.0115),
}


# --- helpers ----------------------------------------------------------------


def _population_z(values: Sequence[float]) -> tuple[float, float, list[float]]:
    """Mean, population standard deviation, and z-scores, per section 1.3."""
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return mean, sd, [(v - mean) / sd for v in values]


def _effective_weights(currency: str, config: ScoringConfig) -> list[float]:
    """Per-pillar effective weights for one currency in the worked example.

    Every currency carries the configured weights except NZD, whose EXTERNAL
    pillar is 30 days old and discounted to `NZD_STALE_EFFECTIVE_WEIGHT` by the
    section 4.1 ramp.
    """
    weights = [config.weights[pillar] for pillar in PILLAR_ORDER]
    if currency == "NZD":
        weights[PILLAR_ORDER.index(NZD_STALE_PILLAR)] = NZD_STALE_EFFECTIVE_WEIGHT
    return weights


def _spread(base: str, quote: str) -> float:
    """Published composite of `base` less published composite of `quote`."""
    return CURRENCY_RESULTS[base][1] - CURRENCY_RESULTS[quote][1]


def _tier(spread: float, config: ScoringConfig) -> Conviction:
    """Base conviction tier from the spread alone, per section 5.4."""
    magnitude = abs(spread)
    if magnitude >= config.min_spread_high:
        return Conviction.HIGH
    if magnitude >= config.min_spread_medium:
        return Conviction.MEDIUM
    if magnitude >= config.min_spread_low:
        return Conviction.LOW
    return Conviction.NONE


def _direction(spread: float, config: ScoringConfig) -> Direction:
    """Direction on the base currency from the spread alone, per section 5.2."""
    if spread >= config.min_spread_low:
        return Direction.LONG
    if spread <= -config.min_spread_low:
        return Direction.SHORT
    return Direction.NEUTRAL


# --- the example against itself and against the committed config -------------


def test_every_spec_anchor_points_at_what_it_claims() -> None:
    """The references beside the transcribed tables are checked, not assumed.

    A wrong line reference is worse than none: it sends the next reader to a
    real line with unrelated content. If this fails, correct the line number in
    `SPEC_ANCHORS` rather than deleting the anchor, and check whether the table
    it points at moved in content as well as in position.
    """
    lines = SPEC.read_text().splitlines()
    wrong = []
    for label, (line_number, snippet) in SPEC_ANCHORS.items():
        actual = lines[line_number - 1] if line_number <= len(lines) else ""
        if snippet not in actual:
            found = [i + 1 for i, line in enumerate(lines) if snippet in line]
            wrong.append(f"{label}: claims line {line_number}, found at {found}")

    assert not wrong, "spec anchors have drifted:\n  " + "\n  ".join(wrong)


def test_the_section_7_column_order_is_the_g10_order() -> None:
    """Every table is read positionally, so the two orders must agree."""
    assert UNIVERSE == G10


def test_the_pillar_matrix_covers_every_currency_and_pillar() -> None:
    """A transcription guard. A dropped row or column would weaken the rest."""
    assert set(MATRIX) == set(G10)
    assert set(CURRENCY_RESULTS) == set(G10)
    assert len(PILLAR_ORDER) == len(set(PILLAR_ORDER)) == len(PillarName)
    for currency, scores in MATRIX.items():
        assert len(scores) == len(PILLAR_ORDER), currency


def test_the_matrix_header_weights_are_the_configured_weights() -> None:
    """Section 7.6's header reads "MON 0.30 INF 0.15 ... RSK 0.10".

    Those are `ScoringConfig.weights`, written into the document. If a weight
    moves in config and the example is not recomputed, every composite in
    section 7.6 becomes wrong, which is the regression this file exists for.
    """
    config = ScoringConfig()
    assert config.weights[PillarName.MONETARY] == 0.30
    assert config.weights[PillarName.INFLATION] == 0.15
    assert config.weights[PillarName.GROWTH] == 0.15
    assert config.weights[PillarName.EMPLOYMENT] == 0.10
    assert config.weights[PillarName.EXTERNAL] == 0.10
    assert config.weights[PillarName.POSITIONING] == 0.10
    assert config.weights[PillarName.RISK] == 0.10


@pytest.mark.parametrize(
    ("pillar", "sub_weights"),
    [
        # 7.1 blends with 0.15 / 0.25 / 0.20 / 0.25 / 0.15, anchor "7.1 sub-weights".
        (
            MonetaryPillar(),
            {
                "policy_rate": 0.15,
                "yield_2y": 0.25,
                "yield_2y_chg_1m": 0.20,
                "yield_2y_chg_3m": 0.25,
                "real_policy_rate": 0.15,
            },
        ),
        # 7.2 blends 0.40 headline and 0.60 core, anchor "7.2 sub-weights".
        (InflationPillar(), {"cpi_gap": 0.40, "core_gap": 0.60}),
        # 7.3 uses the section 3.3 sub-weights.
        (
            GrowthPillar(),
            {
                "gdp_yoy": 0.30,
                "pmi_composite": 0.30,
                "indpro_yoy": 0.20,
                "retail_sales_yoy": 0.20,
            },
        ),
        (
            EmploymentPillar(),
            {"unemployment_6m": 0.50, "employment_trend": 0.50},
        ),
        (
            ExternalPillar(),
            {
                "current_account_gdp": 0.40,
                "trade_trend": 0.30,
                "terms_of_trade": 0.30,
            },
        ),
    ],
)
def test_the_sub_weights_the_example_blends_with_are_the_pillars_own(
    pillar: BasePillar, sub_weights: Mapping[str, float]
) -> None:
    """The example's blends are only reproducible if the sub-weights match.

    Transcribed from the section 7 subsection that uses them, then compared to
    what the pillar class returns. INFLATION's component names are read from the
    pillar rather than from section 7, which names the two columns "headline
    deviation" and "core deviation" in prose and not as keys.
    """
    assert dict(pillar.component_weights) == dict(sub_weights)


@pytest.mark.parametrize("indicator", sorted(MONETARY_RAW))
def test_the_monetary_z_scores_follow_from_the_raw_inputs(indicator: str) -> None:
    """Criterion 1, checked against section 7.1's own inputs.

    The z-scores are published to three decimals and the statistics to four, so
    the tolerances are half a unit in the last published place. If a raw input
    in the table at anchor "7.1 raw inputs" is edited without recomputing
    line 794, this is what notices.
    """
    mean, sd, z = _population_z(MONETARY_RAW[indicator])
    published_mean, published_sd = MONETARY_STATS[indicator]

    assert mean == pytest.approx(published_mean, abs=5e-5)
    assert sd == pytest.approx(published_sd, abs=5e-5)
    for currency, computed, published in zip(
        UNIVERSE, z, MONETARY_Z[indicator], strict=True
    ):
        assert computed == pytest.approx(published, abs=5e-4), currency


def test_the_usd_monetary_blend_is_the_published_working() -> None:
    """Section 7.1 works this one by hand, at anchor "7.1 usd blend"."""
    weights = {
        "policy_rate": 0.15,
        "yield_2y": 0.25,
        "chg_1m": 0.20,
        "chg_3m": 0.25,
        "real_policy_rate": 0.15,
    }
    blend = sum(weight * MONETARY_Z[key][0] for key, weight in weights.items())

    assert blend == pytest.approx(USD_MONETARY_BLEND, abs=5e-6)
    assert blend / MONETARY_BLEND_SD == pytest.approx(1.62, abs=5e-3)


def test_the_monetary_scores_follow_from_the_component_z_scores() -> None:
    """Criterion 2, for the MONETARY column, carried from 7.1's z table.

    Section 7.1 says every later step uses the published rounded values, so the
    blend is taken over `MONETARY_Z` rather than over z-scores recomputed from
    the raw inputs. The sub-weight keys are the example's column headings; the
    test above pins that they are the pillar's own keys in the same order.
    """
    sub_weights = (0.15, 0.25, 0.20, 0.25, 0.15)
    columns = (
        MONETARY_Z["policy_rate"],
        MONETARY_Z["yield_2y"],
        MONETARY_Z["chg_1m"],
        MONETARY_Z["chg_3m"],
        MONETARY_Z["real_policy_rate"],
    )
    blend = [
        sum(w * column[i] for w, column in zip(sub_weights, columns, strict=True))
        for i in range(len(UNIVERSE))
    ]
    mean, sd, _ = _population_z(blend)

    assert sd == pytest.approx(MONETARY_BLEND_SD, abs=5e-5)
    index = PILLAR_ORDER.index(PillarName.MONETARY)
    for currency, value in zip(UNIVERSE, blend, strict=True):
        assert round((value - mean) / sd, 2) == pytest.approx(
            MATRIX[currency][index]
        ), currency


def test_the_growth_scores_follow_from_the_growth_inputs() -> None:
    """Section 7.3 gives inputs, `sd(blend)` and final scores, so it closes.

    GROWTH is the one of the three pillars in 7.3 whose sub-weights are all
    published and whose components need no construction, so it is the one that
    can be carried from raw input to published score here. EMPLOYMENT inverts a
    column and EXTERNAL derives a terms-of-trade term; both are the pillar
    layer's work, and their assertions are in the guarded half below.
    """
    sub_weights = GrowthPillar().component_weights
    columns = {key: _population_z(values)[2] for key, values in GROWTH_RAW.items()}
    blend = [
        sum(sub_weights[key] * columns[key][i] for key in GROWTH_RAW)
        for i in range(len(UNIVERSE))
    ]
    mean, sd, _ = _population_z(blend)

    assert sd == pytest.approx(BLEND_SD["GROWTH"], abs=5e-5)
    index = PILLAR_ORDER.index(PillarName.GROWTH)
    for currency, value in zip(UNIVERSE, blend, strict=True):
        assert (value - mean) / sd == pytest.approx(MATRIX[currency][index], abs=5e-3)


def test_the_positioning_p_values_follow_from_the_reported_positions() -> None:
    """Section 7.4's `p` is a time-series z, not a cross-sectional one."""
    for currency, (net, mean, sd, p, _) in POSITIONING.items():
        assert (net - mean) / sd == pytest.approx(p, abs=5e-3), currency


def test_the_positioning_responses_reproduce_through_the_pillar() -> None:
    """Criterion 2, for the POSITIONING column, through `response`.

    `PositioningPillar.response` is the section 3.6 piecewise function and is
    implemented, so this runs against the real code rather than against
    arithmetic restated here. It is the one pillar column in section 7.6 that
    can be carried all the way to a published score today, because positioning
    is not cross-sectionally re-standardised: `f(p)` is already on the band.
    """
    index = PILLAR_ORDER.index(PillarName.POSITIONING)
    for currency, (_, _, _, p, published) in POSITIONING.items():
        response = PositioningPillar.response(p)

        assert response is not None, currency
        assert round(response, 2) == pytest.approx(published), currency
        assert MATRIX[currency][index] == pytest.approx(published), currency


def test_the_jpy_contrarian_case_is_still_past_the_boundary() -> None:
    """Section 7.4 calls JPY "the one job it exists to do", at anchor "7.4 jpy".

    The passage turns on `p = -2.40` sitting past the contrarian boundary of
    2.00 and short of the saturation point of 3.33, and on the sign of `f(p)`
    being opposite to the sign of `p`. If the boundaries move, the example's
    most discussed pillar reading becomes a different reading, so it fails here
    rather than silently.
    """
    p = POSITIONING["JPY"][3]
    response = PositioningPillar.response(p)

    assert response is not None
    assert p < 0 < response, "a crowded short must score positively"
    contrarian = -math.copysign(1.0, p) * 1.5 * (abs(p) - 2.00)
    assert response == pytest.approx(contrarian, abs=5e-4)


def test_no_currency_in_the_run_reaches_positioning_saturation() -> None:
    """Stated at anchor "7.4 saturation", and load-bearing for the column above.

    If any reading were past saturation the published `f(p)` would be capped and
    the worked values would not follow from the formula the example quotes.
    """
    assert max(abs(row[3]) for row in POSITIONING.values()) < 3.33


def test_the_risk_construction_follows_from_the_two_components() -> None:
    """Section 7.5. `risk_beta` comes from the universe.

    Rounded rather than compared with a tolerance, because four of the eight sit
    exactly on a half: USD computes to +0.6250 and publishes as +0.62, not
    +0.63. Section 7 rounds half to even throughout, which is what `round` does,
    and a tolerance wide enough to accept +0.6250 as +0.62 would also accept
    +0.63 and stop testing the thing.
    """
    dd_component = max(-1.0, min(1.0, EQUITY_DRAWDOWN_PCT / 10.0))
    vol_component = max(-1.0, min(1.0, -VOL_SIGMA / 2.0))

    assert dd_component == pytest.approx(-0.650)
    assert vol_component == pytest.approx(-0.600)
    assert 0.5 * dd_component + 0.5 * vol_component == pytest.approx(RISK_R)

    index = PILLAR_ORDER.index(PillarName.RISK)
    for currency, (beta, score) in RISK_SCORES.items():
        assert meta(currency).risk_beta == pytest.approx(beta), currency
        assert round(2.0 * RISK_R * beta, 2) == pytest.approx(score), currency
        assert MATRIX[currency][index] == pytest.approx(score), currency


def test_the_inflation_targets_are_the_universe_metadata() -> None:
    """Section 7.2 quotes `CurrencyMeta.inflation_target`, anchor "7.2 targets".

    The deviations in the table below it are computed from these, so a target
    changing in `fbe.universe` invalidates the whole of 7.2.
    """
    for currency, target in INFLATION_TARGETS.items():
        assert meta(currency).inflation_target == pytest.approx(target), currency


def test_the_inflation_deviation_statistics_follow_from_the_deviations() -> None:
    """Section 7.2, anchor "7.2 stats"."""
    headline = [INFLATION_DEVIATIONS[c][0] for c in UNIVERSE]
    core = [INFLATION_DEVIATIONS[c][1] for c in UNIVERSE]

    for label, values in (("headline", headline), ("core", core)):
        mean, sd, _ = _population_z(values)
        published_mean, published_sd = INFLATION_STATS[label]
        assert mean == pytest.approx(published_mean, abs=5e-5), label
        assert sd == pytest.approx(published_sd, abs=5e-5), label


def test_the_inflation_scores_follow_from_the_deviations() -> None:
    """Carried from the deviation table to the published INFLATION row."""
    sub_weights = InflationPillar().component_weights
    headline_weight = sub_weights["cpi_gap"]
    core_weight = sub_weights["core_gap"]
    _, _, z_headline = _population_z([INFLATION_DEVIATIONS[c][0] for c in UNIVERSE])
    _, _, z_core = _population_z([INFLATION_DEVIATIONS[c][1] for c in UNIVERSE])
    blend = [
        headline_weight * h + core_weight * c
        for h, c in zip(z_headline, z_core, strict=True)
    ]
    mean, sd, _ = _population_z(blend)

    assert sd == pytest.approx(INFLATION_BLEND_SD, abs=5e-5)
    index = PILLAR_ORDER.index(PillarName.INFLATION)
    for currency, value in zip(UNIVERSE, blend, strict=True):
        assert (value - mean) / sd == pytest.approx(MATRIX[currency][index], abs=5e-3)


def test_every_published_composite_follows_from_the_pillar_matrix() -> None:
    """Criterion 3, for the composites, against `ScoringConfig.weights`.

    The published composites are given to four decimals, and this reproduces
    them to that precision for all eight currencies, NZD's discounted pillar
    included.
    """
    config = ScoringConfig()
    for currency, scores in MATRIX.items():
        weights = _effective_weights(currency, config)
        coverage, composite, _, _ = CURRENCY_RESULTS[currency]

        assert sum(weights) == pytest.approx(coverage, abs=5e-4), currency
        weighted = sum(w * s for w, s in zip(weights, scores, strict=True))
        assert weighted / sum(weights) == pytest.approx(composite, abs=5e-5), currency


def test_the_nzd_staleness_discount_is_the_section_4_1_ramp() -> None:
    """Section 7.6, anchor "7.6 nzd staleness", against the configured ramp."""
    config = ScoringConfig()
    phi = (config.max_staleness_days - NZD_STALE_DAYS) / (
        config.max_staleness_days - config.staleness_full_days
    )

    assert phi == pytest.approx(NZD_STALE_PHI)
    assert config.weights[NZD_STALE_PILLAR] * phi == pytest.approx(
        NZD_STALE_EFFECTIVE_WEIGHT
    )
    assert CURRENCY_RESULTS["NZD"][0] == pytest.approx(0.950)


def test_every_published_dispersion_follows_from_the_pillar_matrix() -> None:
    """Section 4.4's coverage-weighted spread, worked at "7.6 nzd dispersion"."""
    config = ScoringConfig()
    for currency, scores in MATRIX.items():
        weights = _effective_weights(currency, config)
        coverage = sum(weights)
        composite = CURRENCY_RESULTS[currency][1]
        variance = sum(
            (w / coverage) * (s - composite) ** 2
            for w, s in zip(weights, scores, strict=True)
        )

        assert math.sqrt(variance) == pytest.approx(
            CURRENCY_RESULTS[currency][2], abs=5e-5
        ), currency


def test_no_dispersion_in_the_run_triggers_a_demotion() -> None:
    """Stated at "7.6 no demotion" for NZD. True of the matrix, so pinned."""
    config = ScoringConfig()
    for currency, (_, _, dispersion, _) in CURRENCY_RESULTS.items():
        assert dispersion < config.max_dispersion, currency


def test_the_ranks_order_the_composites() -> None:
    """Section 4.5. Rank 1 is the strongest currency."""
    by_rank = sorted(CURRENCY_RESULTS, key=lambda c: CURRENCY_RESULTS[c][3])
    by_composite = sorted(
        CURRENCY_RESULTS, key=lambda c: CURRENCY_RESULTS[c][1], reverse=True
    )

    assert by_rank == by_composite
    assert [CURRENCY_RESULTS[c][3] for c in by_rank] == list(range(1, len(G10) + 1))


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_every_published_pair_reproduces(pair: str) -> None:
    """Criterion 3, for the four pairs section 7.7 carries to a conclusion."""
    config = ScoringConfig()
    base, quote = split_pair(pair)
    published_spread, direction, conviction, _ = PAIRS[pair]

    assert _spread(base, quote) == pytest.approx(published_spread, abs=5e-5)
    assert _tier(published_spread, config) is conviction
    assert _direction(published_spread, config) is direction


def test_the_nzdusd_agreement_is_the_published_working() -> None:
    """Section 7.7, anchor "7.7 nzdusd agreement".

    The EXTERNAL pair weight is the mean of the two legs' effective weights,
    which is where New Zealand's stale pillar reaches the pair layer.
    """
    config = ScoringConfig()
    external = (NZD_STALE_EFFECTIVE_WEIGHT + config.weights[PillarName.EXTERNAL]) / 2
    assert external == pytest.approx(NZDUSD_EXTERNAL_PAIR_WEIGHT)

    considered = sum(
        external if pillar is PillarName.EXTERNAL else config.weights[pillar]
        for pillar in PILLAR_ORDER
    )
    agreeing = considered - config.weights[PillarName.POSITIONING]

    assert considered == pytest.approx(NZDUSD_CONSIDERED)
    assert agreeing == pytest.approx(NZDUSD_AGREEING)
    assert agreeing / considered == pytest.approx(PAIRS["NZDUSD"][3], abs=5e-5)


def test_no_published_agreement_triggers_a_conviction_cap() -> None:
    """Spec lines 1063 and 1100 both say so. Pinned against the configured floor."""
    config = ScoringConfig()
    for pair, (_, _, _, agreement) in PAIRS.items():
        if agreement is not None:
            assert agreement >= config.min_agreement, pair


def test_the_published_cost_ratios_pass_the_filter() -> None:
    """Section 6's cost filter, worked at the two cost anchors."""
    config = ScoringConfig()
    for pair, (spread_pips, atr, published) in COSTS.items():
        expected_move = atr * math.sqrt(config.horizon_days)
        ratio = spread_pips / expected_move

        assert ratio == pytest.approx(published, abs=5e-5), pair
        assert ratio < config.max_cost_ratio, pair


def test_the_usdjpy_threshold_case_is_still_on_the_threshold() -> None:
    """Spec lines 1098 to 1117 turn on this pair clearing LOW by 0.0045.

    The passage is an argument about bucketing a continuous quantity, and it
    stops making sense if `min_spread_low` moves. Pinned so that a change to
    the threshold fails here rather than silently turning the spec's most
    discussed example into a different example.
    """
    config = ScoringConfig()
    spread = PAIRS["USDJPY"][0]

    assert spread - config.min_spread_low == pytest.approx(0.0045, abs=5e-5)
    assert _tier(spread, config) is Conviction.LOW


# --- criteria 5 and 6, over all 28 pairs ------------------------------------


def test_the_spread_is_antisymmetric_across_all_28_pairs() -> None:
    """Criterion 5. Section 9 lists this as an obligation in its own right.

    Every pair in `ALL_PAIRS` is built from two currencies in the matrix, so the
    worked example supplies a full cross-section and the property can be checked
    on all 28 rather than on the three the document carries to a conclusion.
    """
    assert len(ALL_PAIRS) == 28
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        assert _spread(base, quote) == pytest.approx(-_spread(quote, base))


def test_direction_and_conviction_never_disagree_across_all_28_pairs() -> None:
    """Criterion 6, both halves.

    NEUTRAL exactly when the tier is NONE, and NONE exactly when the spread is
    inside the neutral band. A pair the model will not back at any size must not
    carry a direction, and a pair with a direction must be backed.
    """
    config = ScoringConfig()
    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        spread = _spread(base, quote)
        direction = _direction(spread, config)
        conviction = _tier(spread, config)

        assert (direction is Direction.NEUTRAL) is (conviction is Conviction.NONE), pair
        assert (conviction is Conviction.NONE) is (
            abs(spread) < config.min_spread_low
        ), pair


def test_the_three_pairs_the_example_carries_all_clear_the_neutral_band() -> None:
    """Section 7.8's shortlist, anchor "7.8 shortlist", against all 28 pairs.

    Section 7.7 works three crosses because they are the three followed
    currencies, and 7.8 presents those three as the run's shortlist. Extending
    the same arithmetic to all 28 says something 7.8 does not: **AUDNZD also
    reaches MEDIUM**, at a spread of +1.6699, second only to NZDUSD at -1.9194.
    Both legs are in the 7.6 matrix, so it is the example's own number.

    So this asserts what section 7 demonstrates rather than what 7.8 summarises:
    NZDUSD is the widest pair and the only MEDIUM among the three carried, and
    all three clear the neutral band at the published tiers. The AUDNZD gap is
    reported on issue #19 for the architect rather than papered over here, and
    deliberately not asserted as correct in either direction: whether 7.8 should
    name it is an editorial call on the document, not a fact about the code.
    """
    config = ScoringConfig()
    carried = ("NZDUSD", "NZDJPY", "USDJPY")
    tiers = {
        pair: _tier(_spread(*split_pair(pair)), config)
        for pair in ALL_PAIRS
        if _tier(_spread(*split_pair(pair)), config) is not Conviction.NONE
    }

    for pair in carried:
        assert pair in tiers, pair
        assert tiers[pair] is PAIRS[pair][2], pair

    assert not [p for p, t in tiers.items() if t is Conviction.HIGH]
    widest = max(tiers, key=lambda p: abs(_spread(*split_pair(p))))
    assert widest == "NZDUSD"
    assert {p for p in carried if tiers[p] is Conviction.MEDIUM} == {"NZDUSD"}


# --- the implementation, once it lands --------------------------------------


def test_the_monetary_divisor_takes_the_run_local_path() -> None:
    """Criterion 4, first half. `blend_divisor` is implemented, so this runs.

    Section 7 is a single run with no stored history, so `blend_divisor` cannot
    reach `min_restandardisation_runs` and must fall back to this run's own
    `sd(blend)`. The whole of section 7 is that fallback path, and if this ever
    returned "rolling" every score in the example would be on a different scale.
    """
    pillar = MonetaryPillar()
    assert not pillar.blend_sd_history

    divisor, path = pillar.blend_divisor(MONETARY_BLEND_SD)

    assert path == "run_local"
    assert divisor == pytest.approx(MONETARY_BLEND_SD)


def test_the_rolling_path_needs_more_history_than_the_example_has() -> None:
    """The other side of criterion 4, so the assertion above is not vacuous.

    Section 9 forbids asserting a population standard deviation of exactly 1.0
    under the rolling path, which is the behaviour section 2.3 exists to avoid.
    This asserts only which path is taken.
    """
    config = ScoringConfig()
    history = [MONETARY_BLEND_SD] * config.min_restandardisation_runs

    _, path = MonetaryPillar(blend_sd_history=history).blend_divisor(0.5)
    assert path == "rolling"

    _, path = MonetaryPillar(
        blend_sd_history=history[:-1],
    ).blend_divisor(0.5)
    assert path == "run_local"


def test_every_published_score_is_inside_the_clip_band() -> None:
    """Section 9's band obligation, on `clip_and_scale`, which is implemented."""
    config = ScoringConfig()
    for currency, scores in MATRIX.items():
        for pillar, score in zip(PILLAR_ORDER, scores, strict=True):
            assert BasePillar.clip_and_scale(score, config.score_clip) == pytest.approx(
                score
            ), f"{currency} {pillar.value}"


def _pillar_scores(
    currency: str, config: ScoringConfig
) -> dict[PillarName, PillarScore]:
    """The section 7.6 row for one currency as `PillarScore` objects.

    ``weight`` carries the effective weight, after the staleness penalty, which
    is what `PillarScore.weight` means downstream of `apply_staleness_penalty`.
    NZD's EXTERNAL pillar is the one entry in the example where the two differ.

    ``raw`` is ``None`` because section 7.6 publishes scores and not headline
    numbers in natural units, so there is nothing to put there. That is not the
    absence marker: absence is ``z is None``, and every pillar in the matrix is
    present, NZD's discounted EXTERNAL included. It is stale, not missing.
    """
    weights = _effective_weights(currency, config)
    return {
        pillar: PillarScore(
            pillar=pillar,
            currency=currency,
            raw=None,
            z=score,
            score=score,
            weight=weight,
            asof=ASOF,
            staleness_days=(
                NZD_STALE_DAYS
                if currency == "NZD" and pillar is NZD_STALE_PILLAR
                else 0
            ),
        )
        for pillar, score, weight in zip(
            PILLAR_ORDER, MATRIX[currency], weights, strict=True
        )
    }


def test_the_monetary_z_scores_reproduce_through_cross_sectional_z() -> None:
    """Criterion 1, through the implementation rather than through arithmetic.

    The assertion above checks section 7.1 against its own inputs. This checks it
    against the function that is supposed to produce it. Both are needed: the
    first catches the document drifting, this one catches the code drifting.
    """
    _skip_if_scaffolded(BasePillar.cross_sectional_z)

    for indicator, raw in MONETARY_RAW.items():
        values: Mapping[str, float | None] = dict(zip(UNIVERSE, raw, strict=True))
        computed = BasePillar.cross_sectional_z(values)
        for currency, published in zip(UNIVERSE, MONETARY_Z[indicator], strict=True):
            assert computed[currency] == pytest.approx(published, abs=5e-4), (
                f"{indicator} {currency}"
            )


def test_the_composites_reproduce_through_the_scorer() -> None:
    """Criterion 3, for the composites, through `fbe.scoring.composite`."""
    import fbe.scoring

    _skip_if_scaffolded(fbe.scoring.composite)

    config = ScoringConfig()
    for currency in G10:
        scores = _pillar_scores(currency, config)
        weights = {p: s.weight for p, s in scores.items()}
        assert fbe.scoring.composite(scores, weights) == pytest.approx(
            CURRENCY_RESULTS[currency][1], abs=5e-5
        ), currency


def test_the_coverage_and_dispersion_reproduce_through_the_scorer() -> None:
    """Criterion 2's coverage and dispersion columns, through `fbe.scoring`."""
    import fbe.scoring

    _skip_if_scaffolded(fbe.scoring.coverage, fbe.scoring.dispersion)

    config = ScoringConfig()
    for currency in G10:
        scores = _pillar_scores(currency, config)
        weights = {p: s.weight for p, s in scores.items()}
        published_coverage, _, published_dispersion, _ = CURRENCY_RESULTS[currency]

        assert fbe.scoring.coverage(scores, weights) == pytest.approx(
            published_coverage, abs=5e-4
        ), currency
        assert fbe.scoring.dispersion(scores, weights) == pytest.approx(
            published_dispersion, abs=5e-5
        ), currency


def test_the_directions_reproduce_through_the_bias_layer() -> None:
    """Criterion 3's direction column, through `fbe.bias.direction_for`."""
    import fbe.bias

    _skip_if_scaffolded(fbe.bias.direction_for)

    config = ScoringConfig()
    for pair, (spread, direction, _, _) in PAIRS.items():
        assert fbe.bias.direction_for(spread, config) is direction, pair

    for pair in ALL_PAIRS:
        base, quote = split_pair(pair)
        assert fbe.bias.direction_for(_spread(base, quote), config) is _direction(
            _spread(base, quote), config
        ), pair


def test_the_conviction_tiers_reproduce_through_the_bias_layer() -> None:
    """Criterion 3's conviction column, through `fbe.bias.conviction_for`.

    Section 7.7 states for each pair that no demotion applies: agreement is above
    `min_agreement`, coverage above `coverage_demotion`, dispersion under
    `max_dispersion`, and no event inside 24 hours. So the published tier is the
    base tier, and passing the published inputs must return it.
    """
    import fbe.bias

    _skip_if_scaffolded(fbe.bias.conviction_for)

    config = ScoringConfig()
    for pair, (spread, _, conviction, agreement) in PAIRS.items():
        base, quote = split_pair(pair)
        coverage = min(CURRENCY_RESULTS[base][0], CURRENCY_RESULTS[quote][0])
        dispersion = max(CURRENCY_RESULTS[base][2], CURRENCY_RESULTS[quote][2])

        assert (
            fbe.bias.conviction_for(
                spread,
                config.min_agreement if agreement is None else agreement,
                coverage,
                dispersion,
                False,
                config,
            )
            is conviction
        ), pair


def test_the_agreement_reproduces_through_the_bias_layer() -> None:
    """Section 7.7's agreement column, through `fbe.bias.agreement`."""
    import fbe.bias

    _skip_if_scaffolded(fbe.bias.agreement)

    config = ScoringConfig()
    legs = {
        currency: CurrencyScore(
            currency=currency,
            composite=CURRENCY_RESULTS[currency][1],
            pillars=_pillar_scores(currency, config),
            asof=ASOF,
            rank=CURRENCY_RESULTS[currency][3],
            dispersion=CURRENCY_RESULTS[currency][2],
            coverage=CURRENCY_RESULTS[currency][0],
        )
        for currency in G10
    }

    for pair, (_, _, _, published) in PAIRS.items():
        if published is None:
            continue
        base, quote = split_pair(pair)
        assert fbe.bias.agreement(legs[base], legs[quote]) == pytest.approx(
            published, abs=5e-5
        ), pair
