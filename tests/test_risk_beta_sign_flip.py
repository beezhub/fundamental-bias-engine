"""Issue #22, measurement one: what a `risk_beta` sign flip costs this model.

`docs/answers/framework.md` Q9 objects to `risk_beta` as a constant on one
episode: April 2025, when the dollar fell alongside equities while
``CurrencyMeta.risk_beta("USD")`` is ``-0.5``, so the RISK pillar scored USD
positive on exactly the days the pillar exists to handle. `scoring-maths.md`
Roadmap 4 answers "keep static, document the error band", and its simulation
models the true beta as a process centred on the stored value, which cannot
produce a sign flip. So the published cost table's parameter range never
contains the episode the objection is about.

This is the measurement that closes that gap. It is the only one of the issue's
three that is not blocked: measurements two and three need a rolling realised
beta, which needs `src/fbe/datasources/prices.py`, which is still scaffolded.

**These numbers are a perturbation of the section 7 worked example. They are not
a historical result and nothing here has been backtested.** What the fixture
bounds is the size of the move, not how often a conviction boundary is standing
in its way. On a run where the dollar sits nearer a boundary than it does in
section 7, the same move changes more tiers.

The measurement runs the real code rather than the spec's arithmetic. RISK is
recomputed for all eight currencies from `RISK_SCALE`, the other six pillar
columns are section 7.6 as published, and the composite, coverage, dispersion,
agreement, direction and conviction all come from `fbe.scoring` and `fbe.bias`.
`test_the_harness_reproduces_the_published_fixture` is what makes the rest
meaningful: if the harness cannot reproduce section 7 before anything is
flipped, no number it produces afterwards is worth reading.

The flip is applied to a local copy of the beta. Nothing in `src/fbe/universe.py`
is patched, monkeypatched or committed, and
`test_the_stored_beta_is_untouched` asserts it.

Two conventions, both deliberate:

The recomputed RISK column is rounded to two decimals, because the six columns
it sits beside are published to two decimals and a composite that mixes
precisions is not the fixture's composite. It has a visible effect: at
``R = -0.625`` the exact score is ``+/-0.625`` and rounds to ``+/-0.62``, so the
measured move is 0.124 rather than 0.125.

The published tables are imported from `tests.test_worked_example` rather than
copied. A second transcription of section 7 is the config-drift defect this
repository keeps finding, and the coupling is the point: this measurement is a
perturbation of that fixture, so it should break when that fixture moves.

Nothing here reaches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

import fbe.bias
import fbe.scoring
from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.pillars.risk import RISK_SCALE
from fbe.types import (
    Conviction,
    CurrencyScore,
    Direction,
    PairBias,
    PillarName,
    PillarScore,
)
from fbe.universe import ALL_PAIRS, G10, meta
from tests.test_worked_example import (
    ASOF,
    CURRENCY_RESULTS,
    MATRIX,
    NZD_STALE_DAYS,
    NZD_STALE_EFFECTIVE_WEIGHT,
    NZD_STALE_PILLAR,
    PILLAR_ORDER,
)

CONFIG = ScoringConfig()
RUN_CONFIG = Config(risk=RiskConfig(), scoring=CONFIG, data=DataConfig())
"""Dataclass defaults throughout, never `fbe.config.default_config()`.

That one reads the environment, which would let this measurement differ
between a developer's machine and CI.
"""

R_VALUES: tuple[float, ...] = (-0.25, -0.625, -1.0)
"""The three regime readings the ruling on #22 asks for.

``-0.625`` is section 7's own regime. ``-1.0`` is saturation, which is what
April 2025 reaches: a drawdown near 19% saturates the drawdown component and a
volatility z-score far above 2 saturates the other. ``-0.25`` is an ordinary
risk-off day, included because the error scales with the regime and vanishes in
a calm run.
"""

STORED_USD_BETA = -0.5
"""``CurrencyMeta.risk_beta("USD")``, asserted against the universe below."""

RISK_INDEX = PILLAR_ORDER.index(PillarName.RISK)

USD_PAIRS: tuple[str, ...] = tuple(p for p in ALL_PAIRS if "USD" in p)
"""The seven pairs with a dollar leg, reported apart from the other 21.

A single currency's beta is wrong in one direction, so its error reaches every
pair it appears in, all the same way. An average over 28 pairs divides that by
four and hides it.
"""


class Measured(NamedTuple):
    """One row of the measurement, at one `R`."""

    risk_usd_stored: float
    risk_usd_flipped: float
    composite_stored: float
    composite_flipped: float
    dispersion_stored: float
    dispersion_flipped: float
    usd_pair_move: float
    tiers_changed_usd: int
    tiers_changed_other: int
    ranks_changed: int


EXPECTED: dict[float, Measured] = {
    -0.25: Measured(0.25, -0.25, 0.9050, 0.8550, 0.7986, 0.8519, 0.050, 2, 0, 0),
    -0.625: Measured(0.62, -0.62, 0.9420, 0.8180, 0.7756, 0.9054, 0.124, 1, 0, 0),
    -1.0: Measured(1.00, -1.00, 0.9800, 0.7800, 0.7682, 0.9706, 0.200, 0, 0, 0),
}
"""The measurement, pinned so a later change to the model has to restate it.

Computed by the helpers below and by `fbe.scoring` and `fbe.bias`, not
transcribed from the earlier working on the issue. The two agree, which is
worth saying because the earlier working was arithmetic on the spec formula and
this is the code.
"""

PUBLISHED_WORST_ROW = "| -1.000 | 0.50 | 1.0000 | 0.1000 | 0.2000 | 26.7% |"
"""The worst row of the beta-error table in `docs/answers/scoring-maths.md`.

Regime, beta error, pillar error, composite error, worst pair spread error,
share of the neutral band. Read back from the document below so the comparison
recorded on #22 cannot go stale while this file keeps asserting it.
"""

PUBLISHED_WORST_BETA_ERROR = 0.50
PUBLISHED_WORST_COMPOSITE_ERROR = 0.1000
PUBLISHED_WORST_SPREAD_ERROR = 0.2000

SIGN_FLIP_BETA_ERROR = 1.0
"""A flip from ``-0.5`` to ``+0.5`` is a beta error of 1.0, twice that row."""

DOCS = Path(__file__).resolve().parents[1] / "docs"
SCORING_MATHS = DOCS / "answers" / "scoring-maths.md"


# --- the harness ------------------------------------------------------------


def _risk_score(currency: str, r: float, usd_beta: float) -> float:
    """One currency's RISK score at regime `r`, per section 3.7.

    ``score = clip(RISK_SCALE * R * risk_beta, score_clip)``. The clip never
    binds in this measurement: the largest magnitude reached is AUD's
    ``2.0 * -1.0 * 0.9``, which is 1.8 against a clip of 3.0. It is applied
    anyway because leaving it out would be measuring a different formula.
    """
    beta = usd_beta if currency == "USD" else meta(currency).risk_beta
    value = RISK_SCALE * r * beta
    return round(max(-CONFIG.score_clip, min(CONFIG.score_clip, value)), 2)


def _pillar_weights(currency: str) -> list[float]:
    """Effective weights for one currency, after section 7.6's one discount.

    NZD's EXTERNAL pillar is 30 days old in the worked example and carries the
    discounted weight. Every other entry is the configured weight.
    """
    weights = [CONFIG.weights[pillar] for pillar in PILLAR_ORDER]
    if currency == "NZD":
        weights[PILLAR_ORDER.index(NZD_STALE_PILLAR)] = NZD_STALE_EFFECTIVE_WEIGHT
    return weights


def _legs(r: float, usd_beta: float) -> dict[str, CurrencyScore]:
    """The whole universe scored with RISK recomputed at `r` and `usd_beta`.

    The six non-RISK columns are section 7.6 as published. Composite, coverage
    and dispersion come from `fbe.scoring`, and the rank is the composite order,
    so a rank move is observable rather than assumed absent.
    """
    built: dict[str, tuple[dict[PillarName, PillarScore], float, float, float]] = {}
    for currency in G10:
        row = list(MATRIX[currency])
        row[RISK_INDEX] = _risk_score(currency, r, usd_beta)
        scores = {
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
                PILLAR_ORDER, row, _pillar_weights(currency), strict=True
            )
        }
        weights = {pillar: score.weight for pillar, score in scores.items()}
        built[currency] = (
            scores,
            fbe.scoring.composite(scores, weights),
            fbe.scoring.coverage(scores, weights),
            fbe.scoring.dispersion(scores, weights),
        )

    ranked = sorted(G10, key=lambda c: built[c][1], reverse=True)
    return {
        currency: CurrencyScore(
            currency=currency,
            composite=built[currency][1],
            pillars=built[currency][0],
            asof=ASOF,
            rank=ranked.index(currency) + 1,
            dispersion=built[currency][3],
            coverage=built[currency][2],
        )
        for currency in G10
    }


def _rows(legs: dict[str, CurrencyScore]) -> dict[str, PairBias]:
    """Every pair in `ALL_PAIRS`, through `fbe.bias.build_pair_biases`.

    The engine's own row builder rather than a local reconstruction of it. That
    matters more here than it usually would: which leg's coverage and which
    leg's dispersion reach `conviction_for`, and the rule that a NONE tier
    forces the direction to NEUTRAL, are decisions this measurement must not
    make for itself. Reimplementing them would leave the measurement able to
    disagree with the model it is measuring.

    ``event_horizon_guard`` is ``None``, which applies no 24-hour cap. Section
    7.7 states no event is inside 24 hours for the pairs it carries, and a
    blackout would mask the thing being measured.
    """
    biases = fbe.bias.build_pair_biases(
        [legs[currency] for currency in G10], RUN_CONFIG, ASOF
    )
    return {bias.pair: bias for bias in biases}


def _measure(r: float) -> Measured:
    """Score the fixture twice at `r` and report what the flip moved."""
    stored, flipped = _legs(r, STORED_USD_BETA), _legs(r, -STORED_USD_BETA)
    before, after = _rows(stored), _rows(flipped)

    return Measured(
        risk_usd_stored=_risk_score("USD", r, STORED_USD_BETA),
        risk_usd_flipped=_risk_score("USD", r, -STORED_USD_BETA),
        composite_stored=stored["USD"].composite,
        composite_flipped=flipped["USD"].composite,
        dispersion_stored=stored["USD"].dispersion,
        dispersion_flipped=flipped["USD"].dispersion,
        usd_pair_move=max(abs(after[p].spread - before[p].spread) for p in USD_PAIRS),
        tiers_changed_usd=sum(
            before[p].conviction is not after[p].conviction for p in USD_PAIRS
        ),
        tiers_changed_other=sum(
            before[p].conviction is not after[p].conviction
            for p in ALL_PAIRS
            if p not in USD_PAIRS
        ),
        ranks_changed=sum(stored[c].rank != flipped[c].rank for c in G10),
    )


# --- what makes the rest meaningful -----------------------------------------


def test_the_stored_beta_is_untouched() -> None:
    """The fence on this issue. The flip is local, and nothing is committed."""
    assert meta("USD").risk_beta == pytest.approx(STORED_USD_BETA)


def test_the_harness_reproduces_the_published_fixture() -> None:
    """Section 7.6 through this file's own code, before anything is flipped.

    Without this every assertion below could be measuring a harness that does
    not score the way the engine does, and the numbers would look like a
    measurement while being an artefact. The composites are published to four
    decimals, so they are asserted there.
    """
    legs = _legs(-0.625, STORED_USD_BETA)

    for currency in G10:
        coverage, composite, dispersion, rank = CURRENCY_RESULTS[currency]
        assert legs[currency].composite == pytest.approx(composite, abs=5e-5), currency
        assert legs[currency].coverage == pytest.approx(coverage, abs=5e-4), currency
        assert legs[currency].dispersion == pytest.approx(dispersion, abs=5e-5), (
            currency
        )
        assert legs[currency].rank == rank, currency


def test_the_seven_dollar_pairs_are_the_seven_dollar_pairs() -> None:
    """Guards the split the whole measurement is reported across.

    If this ever returned fewer, the 'other 21' figures would quietly absorb a
    dollar pair and the separation the ruling asks for would stop holding.
    """
    assert len(USD_PAIRS) == 7
    assert len(ALL_PAIRS) == 28


# --- the measurement --------------------------------------------------------


@pytest.mark.parametrize("r", R_VALUES)
def test_the_sign_flip_measurement(r: float) -> None:
    """The three rows recorded on #22, recomputed on every run.

    Pinned rather than merely printed: a change to the weights, to the clip, to
    the conviction ladder or to section 7.6 moves these, and the issue's
    conclusion rests on them.
    """
    measured, expected = _measure(r), EXPECTED[r]

    assert measured.risk_usd_stored == pytest.approx(expected.risk_usd_stored)
    assert measured.risk_usd_flipped == pytest.approx(expected.risk_usd_flipped)
    assert measured.composite_stored == pytest.approx(
        expected.composite_stored, abs=5e-5
    )
    assert measured.composite_flipped == pytest.approx(
        expected.composite_flipped, abs=5e-5
    )
    assert measured.dispersion_stored == pytest.approx(
        expected.dispersion_stored, abs=5e-5
    )
    assert measured.dispersion_flipped == pytest.approx(
        expected.dispersion_flipped, abs=5e-5
    )
    assert measured.usd_pair_move == pytest.approx(expected.usd_pair_move, abs=5e-5)
    assert measured.tiers_changed_usd == expected.tiers_changed_usd
    assert measured.tiers_changed_other == expected.tiers_changed_other
    assert measured.ranks_changed == expected.ranks_changed


@pytest.mark.parametrize("r", R_VALUES)
def test_the_error_lands_on_the_seven_dollar_pairs_and_nowhere_else(r: float) -> None:
    """The point the issue body makes, measured rather than asserted.

    One leg moves, so all seven dollar pairs move by exactly the same amount and
    the other 21 do not move at all. "Largest single pair spread move" and
    "every dollar pair's spread move" are therefore the same number here, which
    is why the table reports one figure.
    """
    before, after = _rows(_legs(r, STORED_USD_BETA)), _rows(_legs(r, -STORED_USD_BETA))
    composite_move = abs(
        _legs(r, -STORED_USD_BETA)["USD"].composite
        - _legs(r, STORED_USD_BETA)["USD"].composite
    )

    for pair in USD_PAIRS:
        assert abs(after[pair].spread - before[pair].spread) == pytest.approx(
            composite_move, abs=5e-9
        ), pair

    for pair in ALL_PAIRS:
        if pair in USD_PAIRS:
            continue
        assert after[pair].spread == pytest.approx(before[pair].spread, abs=5e-12), pair


def test_the_flip_neutralises_usdjpy_rather_than_reversing_anything() -> None:
    """The direction column, which the tier count does not show.

    No pair changes side. USDJPY stops having a side at all, at the two regimes
    where its spread was inside the band to begin with. That is the model
    failing toward silence rather than toward the opposite call, and it is worth
    separating from the tier count because a reversal would be a different kind
    of harm.
    """
    for r in R_VALUES:
        before = _rows(_legs(r, STORED_USD_BETA))
        after = _rows(_legs(r, -STORED_USD_BETA))
        changed = {
            p for p in ALL_PAIRS if before[p].direction is not after[p].direction
        }

        assert changed <= {"USDJPY"}, r
        for pair in changed:
            assert before[pair].direction is Direction.LONG
            assert after[pair].direction is Direction.NEUTRAL

    assert {
        r
        for r in R_VALUES
        if _rows(_legs(r, STORED_USD_BETA))["USDJPY"].direction
        is not _rows(_legs(r, -STORED_USD_BETA))["USDJPY"].direction
    } == {-0.25, -0.625}


@pytest.mark.parametrize("r", R_VALUES)
def test_the_flip_puts_gbpusd_agreement_under_the_floor(r: float) -> None:
    """A demotion the tier count cannot show, because the pair is already NONE.

    GBPUSD's agreement falls from 0.65 to 0.55 at every regime, under
    `min_agreement` of 0.60, which caps conviction at LOW. It changes no tier
    here only because GBPUSD's spread is 0.461, inside the neutral band, so the
    pair is NONE before the cap is reached. On a run where that pair carried a
    tier, this flip would demote it as well as move its spread, and the tier
    count on this fixture would understate the cost.
    """
    before = _rows(_legs(r, STORED_USD_BETA))["GBPUSD"]
    after = _rows(_legs(r, -STORED_USD_BETA))["GBPUSD"]

    assert before.agreement == pytest.approx(0.65)
    assert after.agreement == pytest.approx(0.55)
    assert before.agreement >= CONFIG.min_agreement
    assert after.agreement < CONFIG.min_agreement
    assert before.conviction is Conviction.NONE
    assert after.conviction is Conviction.NONE


@pytest.mark.parametrize("r", R_VALUES)
def test_no_dispersion_demotion_fires_on_the_flipped_run(r: float) -> None:
    """USD's own pillars disagree more after the flip, but not enough to demote.

    The dispersion rise is the second-order effect of the flip and it is worth
    stating that it stops short of the threshold, because if it crossed, the
    tier counts above would be measuring two mechanisms at once.
    """
    flipped = _legs(r, -STORED_USD_BETA)

    assert flipped["USD"].dispersion > _legs(r, STORED_USD_BETA)["USD"].dispersion
    for currency in G10:
        assert flipped[currency].dispersion <= CONFIG.max_dispersion, currency


def test_the_published_table_still_says_what_the_comparison_claims() -> None:
    """Criterion 4, asserted against the document rather than from memory.

    The comparison recorded on #22 only means anything while this row reads the
    way it does. Asserted as the whole row: checking for "0.2000" alone passed
    against three other rows of the same table.
    """
    assert PUBLISHED_WORST_ROW in SCORING_MATHS.read_text()


def test_the_sign_flip_sits_outside_the_published_range_on_two_columns_of_three() -> (
    None
):
    """Criterion 4. The comparison, stated precisely, because it is easy to
    state loosely and be wrong in one column.

    A sign flip is a beta error of 1.0, twice the largest the published table
    runs to. Its composite error at ``R = -1.0`` is 0.200, twice that row's
    0.1000. Its pair spread move is 0.200, which equals that row's 0.2000, and
    the equality is a coincidence of two factors of two rather than a match: the
    published column doubles the composite error to model both legs erring in
    opposite directions, and a one-currency sign flip moves one leg. On the
    published table's own convention, both legs flipping against each other
    would be 0.400.

    So the sign-flip case is outside the published range on beta error and on
    composite error, and lands on its worst row only on the spread column.
    """
    assert SIGN_FLIP_BETA_ERROR == 2 * PUBLISHED_WORST_BETA_ERROR

    measured = EXPECTED[-1.0]
    composite_error = abs(measured.composite_flipped - measured.composite_stored)

    assert composite_error == pytest.approx(
        2 * PUBLISHED_WORST_COMPOSITE_ERROR, abs=5e-5
    )
    assert measured.usd_pair_move == pytest.approx(
        PUBLISHED_WORST_SPREAD_ERROR, abs=5e-5
    )
    assert measured.usd_pair_move == pytest.approx(composite_error, abs=5e-5)
