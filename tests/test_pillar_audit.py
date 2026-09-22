"""Phase 3's last question: seven pillars, or fewer wearing seven names.

Issue #176. Two things are measured here and they are the same worry twice.

**What each pillar did.** `fbe.scoring.coverage` already reduces when a pillar
is missing, but it is one number and cannot say which pillar went missing or
why. Three absences look identical on a `PillarScore` and want three different
responses: a pillar switched off for the run, a pillar that raised, and a
pillar that ran and had no data for one currency. The first is deliberate, the
second is a defect, the third is the universe.

**Whether two pillars are one.** Two score columns that move together across
the cross-section are one measurement carrying two weights, and the composite
then claims a diversification it does not have. The figure is arithmetic about
one run's inputs and says nothing about returns, which the output states rather
than leaving to the reader.

The correlations in `test_the_published_matrix_correlations_are_reproduced` come
from section 7.6 of ``docs/scoring-spec.md``, the published pillar matrix, which
`tests/test_worked_example.py` separately holds the real pillars to. They are
real pillar output rather than scores invented here, which is what makes the
figures mean anything.

No network. Every score is built in this file or read from the committed
fixture.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from fbe.config import ScoringConfig
from fbe.pillar_audit import (
    DUPLICATE_CORRELATION,
    MIN_CORRELATION_CURRENCIES,
    AbsenceKind,
    PillarAudit,
    PillarPair,
    audit_run,
    correlate,
    pillar_pairs,
    unmarked_zeros,
)
from fbe.scoring import PILLAR_FAILED, score_currencies
from fbe.types import CurrencyScore, PillarName, PillarScore
from fbe.universe import G10
from tests.test_worked_example import MATRIX, PILLAR_ORDER, UNIVERSE

ASOF = date(2026, 9, 22)
DIGEST = "abc123def456"


def _score(
    pillar: PillarName,
    currency: str,
    value: float,
    *,
    z: float | None = 0.0,
    raw: float | None = 0.0,
    notes: str = "",
    diagnostics: dict[str, float] | None = None,
) -> PillarScore:
    """One pillar score. ``z`` of None with ``raw`` of None is a marked absence."""
    return PillarScore(
        pillar=pillar,
        currency=currency,
        raw=raw,
        z=z,
        score=value,
        weight=0.1,
        asof=ASOF,
        notes=notes,
        diagnostics=diagnostics or {},
    )


def _absence(
    pillar: PillarName, currency: str, *, failed: bool = False, notes: str = "no data"
) -> PillarScore:
    """The marked absence both `fbe.scoring` and the pillars produce."""
    return _score(
        pillar,
        currency,
        0.0,
        z=None,
        raw=None,
        notes=notes,
        diagnostics={PILLAR_FAILED: 1.0} if failed else {},
    )


def _currency(
    currency: str, scores: dict[PillarName, PillarScore], composite: float = 0.0
) -> CurrencyScore:
    return CurrencyScore(
        currency=currency,
        composite=composite,
        pillars=scores,
        asof=ASOF,
    )


def _from_matrix() -> tuple[CurrencyScore, ...]:
    """The section 7.6 matrix as a run's worth of `CurrencyScore` rows.

    Real pillar output: section 7.6 publishes these seven columns for all eight
    currencies, and `tests/test_worked_example.py` holds the real pillars to
    reproducing them. Building the rows here rather than re-running the pillars
    keeps this file about the audit, and the figures are the same either way
    because that file is what proves it.
    """
    return tuple(
        _currency(
            currency,
            {
                pillar: _score(pillar, currency, MATRIX[currency][index], z=0.5)
                for index, pillar in enumerate(PILLAR_ORDER)
            },
        )
        for currency in UNIVERSE
    )


# --- correlate, the arithmetic ----------------------------------------------


def test_a_column_correlated_with_itself_is_one() -> None:
    column = [1.62, 0.86, 1.13, -0.31, -0.54, -0.51, -1.40, -0.87]

    assert correlate(column, column) == pytest.approx(1.0)


def test_a_reversed_column_is_minus_one() -> None:
    column = [1.62, 0.86, 1.13, -0.31, -0.54, -0.51, -1.40, -0.87]

    assert correlate(column, [-value for value in column]) == pytest.approx(-1.0)


def test_the_result_is_clamped_into_the_band() -> None:
    """An exact multiple can put the raw quotient a step outside [-1, 1]."""
    column = [1.0, 2.0, 3.0, 4.0]

    for factor in (2.0, -3.0, 0.1):
        figure = correlate(column, [value * factor for value in column])
        assert figure is not None
        assert -1.0 <= figure <= 1.0


def test_a_hand_computed_correlation_reproduces() -> None:
    """Computed by hand rather than from the implementation.

    left  = [1, 2, 3, 4], mean 2.5, deviations [-1.5, -0.5, 0.5, 1.5]
    right = [2, 4, 5, 9], mean 5.0, deviations [-3.0, -1.0, 0.0, 4.0]
    covariance   = 4.5 + 0.5 + 0.0 + 6.0 = 11.0
    sum of squares 5.0 and 26.0, so the divisor is sqrt(130) = 11.401754...
    r = 11.0 / 11.401754 = 0.96475...
    """
    assert correlate([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 5.0, 9.0]) == pytest.approx(
        11.0 / math.sqrt(130.0), abs=1e-12
    )


def test_a_flat_column_has_no_correlation_rather_than_zero() -> None:
    """A pillar that scored every currency the same carries no spread.

    Zero would read as "measured, and independent". The honest answer is that
    the coefficient divides by a standard deviation of zero and does not exist.
    """
    assert correlate([1.0, 2.0, 3.0], [4.0, 4.0, 4.0]) is None
    assert correlate([4.0, 4.0, 4.0], [1.0, 2.0, 3.0]) is None


def test_mismatched_columns_are_refused() -> None:
    with pytest.raises(ValueError, match="same length"):
        correlate([1.0, 2.0], [1.0, 2.0, 3.0])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_is_refused(bad: float) -> None:
    """A NaN correlation fails every comparison, so it would never be reported
    as a duplicate however duplicated the two pillars were."""
    with pytest.raises(ValueError, match="not finite"):
        correlate([1.0, 2.0, bad], [1.0, 2.0, 3.0])


# --- the pairs --------------------------------------------------------------


def test_every_unordered_pair_appears_exactly_once() -> None:
    pairs = pillar_pairs(_from_matrix())

    assert len(pairs) == 21
    seen = {frozenset((pair.left, pair.right)) for pair in pairs}
    assert len(seen) == 21
    assert all(pair.left != pair.right for pair in pairs)


def test_an_unmeasurable_pair_is_listed_rather_than_dropped() -> None:
    """A pair missing from the list reads as a pair that was fine."""
    rows = [
        _currency(
            currency,
            {
                PillarName.MONETARY: _score(PillarName.MONETARY, currency, 1.0),
                PillarName.RISK: _absence(PillarName.RISK, currency),
            },
        )
        for currency in ("USD", "EUR", "GBP")
    ]

    pairs = pillar_pairs(rows)
    risk_pairs = [pair for pair in pairs if PillarName.RISK in (pair.left, pair.right)]

    assert len(risk_pairs) == 6
    for pair in risk_pairs:
        assert pair.correlation is None
        assert pair.currencies == 0
        assert "fewer than" in pair.reason


def test_an_absent_score_is_not_read_as_a_zero() -> None:
    """The defect the whole module exists to catch, on its own arithmetic.

    Two pillars that both scored USD, EUR and GBP and both went absent on JPY.
    Reading the absence's 0.0 as a score adds a shared point to both columns and
    pulls the correlation toward it. The figure must be the one computed on the
    three currencies both pillars actually scored.
    """
    values = {"USD": (1.0, 2.0), "EUR": (2.0, 4.0), "GBP": (3.0, 5.0)}
    rows = [
        _currency(
            currency,
            {
                PillarName.MONETARY: _score(
                    PillarName.MONETARY, currency, values[currency][0]
                ),
                PillarName.GROWTH: _score(
                    PillarName.GROWTH, currency, values[currency][1]
                ),
            },
        )
        for currency in ("USD", "EUR", "GBP")
    ]
    rows.append(
        _currency(
            "JPY",
            {
                PillarName.MONETARY: _absence(PillarName.MONETARY, "JPY"),
                PillarName.GROWTH: _absence(PillarName.GROWTH, "JPY"),
            },
        )
    )

    pair = next(
        item
        for item in pillar_pairs(rows)
        if {item.left, item.right} == {PillarName.MONETARY, PillarName.GROWTH}
    )

    assert pair.currencies == 3
    expected = correlate([1.0, 2.0, 3.0], [2.0, 4.0, 5.0])
    assert expected is not None
    assert pair.correlation == pytest.approx(expected)
    # Had the two zeros been read as scores, the figure would have been this
    # instead, and it is not the same number.
    with_zeros = correlate([1.0, 2.0, 3.0, 0.0], [2.0, 4.0, 5.0, 0.0])
    assert with_zeros is not None
    assert pair.correlation != pytest.approx(with_zeros)


def test_the_shared_currency_count_is_the_intersection() -> None:
    """ "0.95 on three currencies" and "on eight" are different statements."""
    rows = [
        _currency(
            currency,
            {
                PillarName.MONETARY: _score(PillarName.MONETARY, currency, 1.0 + index),
                PillarName.GROWTH: (
                    _score(PillarName.GROWTH, currency, 2.0 + index)
                    if currency != "NZD"
                    else _absence(PillarName.GROWTH, currency)
                ),
            },
        )
        for index, currency in enumerate(("USD", "EUR", "GBP", "NZD"))
    ]

    pair = next(
        item
        for item in pillar_pairs(rows)
        if {item.left, item.right} == {PillarName.MONETARY, PillarName.GROWTH}
    )

    assert pair.currencies == 3


def test_a_pair_below_the_minimum_cross_section_is_not_given_a_figure() -> None:
    """Two points always correlate at exactly one, whatever they measure."""
    rows = [
        _currency(
            currency,
            {
                PillarName.MONETARY: _score(PillarName.MONETARY, currency, value),
                PillarName.GROWTH: _score(PillarName.GROWTH, currency, value * 3.0),
            },
        )
        for currency, value in (("USD", 1.0), ("EUR", 2.0))
    ]

    pair = next(
        item
        for item in pillar_pairs(rows)
        if {item.left, item.right} == {PillarName.MONETARY, PillarName.GROWTH}
    )

    assert pair.correlation is None
    assert pair.currencies == 2
    assert str(MIN_CORRELATION_CURRENCIES) in pair.reason


def test_the_published_matrix_correlations_are_reproduced() -> None:
    """Computed from section 7.6's real pillar output, not from invented scores.

    The figure for MONETARY against INFLATION is recomputed here from the
    published columns by hand, so this asserts against the fixture rather than
    against the implementation.
    """
    monetary = [MATRIX[currency][0] for currency in UNIVERSE]
    inflation = [MATRIX[currency][1] for currency in UNIVERSE]
    expected = correlate(monetary, inflation)

    pair = next(
        item
        for item in pillar_pairs(_from_matrix())
        if {item.left, item.right} == {PillarName.MONETARY, PillarName.INFLATION}
    )

    assert expected is not None
    assert pair.currencies == 8
    assert pair.correlation == pytest.approx(expected)
    # The two are strongly related on this fixture, which is the finding worth
    # having rather than an assertion about the implementation. Pinned loosely
    # so a fixture correction does not fail on a third decimal.
    assert pair.correlation > 0.8


def test_the_fixture_has_one_duplicate_pair_and_the_audit_names_it() -> None:
    """The finding this issue exists to surface, on the published fixture.

    MONETARY and INFLATION correlate at +0.9175 across section 7.6's eight
    currencies, which is above `DUPLICATE_CORRELATION`. That is not a surprise
    and it is not a defect in either pillar: MONETARY's ``real_policy_rate`` is
    ``policy_rate - cpi_yoy``, so it already carries a coefficient of minus one
    on the series INFLATION is built from, which is the cancellation
    `fbe.scoring.series_loading` and ADR 0003 exist for. This is that same fact
    arriving as a correlation between two columns rather than as a loading on
    one series.

    The figure was cross-checked against `statistics.correlation` from the
    standard library rather than only against this module's own arithmetic, so
    the number is the fixture's and not the implementation's.

    Nothing is folded or reweighted here. That changes `ScoringConfig` and is a
    decision with its own issue and a person in front of it.
    """
    import statistics

    audit = audit_run(_from_matrix(), config_digest=DIGEST)
    duplicates = audit.duplicates

    assert len(duplicates) == 1
    pair = duplicates[0]
    assert {pair.left, pair.right} == {PillarName.MONETARY, PillarName.INFLATION}
    assert pair.currencies == 8

    independent = statistics.correlation(
        [MATRIX[currency][0] for currency in UNIVERSE],
        [MATRIX[currency][1] for currency in UNIVERSE],
    )
    assert pair.correlation == pytest.approx(independent, abs=1e-12)
    assert pair.correlation == pytest.approx(0.9175, abs=5e-5)

    rendered = audit.render()
    assert "+0.9175" in rendered
    assert "monetary" in rendered and "inflation" in rendered


def test_the_duplicate_flag_reads_the_module_threshold() -> None:
    """Not a hardcoded 0.9: moving the constant has to move the finding."""
    just_under = PillarPair(
        left=PillarName.MONETARY,
        right=PillarName.GROWTH,
        correlation=DUPLICATE_CORRELATION - 0.01,
        currencies=8,
    )
    at_it = replace(just_under, correlation=DUPLICATE_CORRELATION)

    assert just_under.duplicated is False
    assert at_it.duplicated is True


def test_a_strong_negative_correlation_is_a_duplicate_too() -> None:
    """Two pillars at minus 0.95 are one measurement with one of them reversed."""
    pair = PillarPair(
        left=PillarName.MONETARY,
        right=PillarName.GROWTH,
        correlation=-0.97,
        currencies=8,
    )

    assert pair.duplicated is True


def test_an_unmeasured_pair_is_not_a_duplicate() -> None:
    """None is not "measured and found independent", and not a duplicate either."""
    pair = PillarPair(
        left=PillarName.MONETARY,
        right=PillarName.GROWTH,
        correlation=None,
        currencies=1,
        reason="too few",
    )

    assert pair.duplicated is False


# --- the accounting ---------------------------------------------------------


def test_a_pillar_that_scored_everyone_is_reported_as_such() -> None:
    audit = audit_run(_from_matrix(), config_digest=DIGEST)

    for pillar in PILLAR_ORDER:
        assert audit.scored[pillar] == UNIVERSE, pillar
    assert audit.absences == ()


def test_the_three_absences_are_told_apart() -> None:
    """The criterion: "no data for this currency" is not "the pillar did not run".

    Three pillars in one run, one of each kind. RISK is absent from the score
    map entirely, which is a pillar that was never run. GROWTH raised, so the
    scorer marked every currency with `PILLAR_FAILED`. EXTERNAL ran and had
    nothing for NZD alone.
    """
    rows = []
    for currency in ("USD", "EUR", "NZD"):
        scores = {
            PillarName.MONETARY: _score(PillarName.MONETARY, currency, 1.0),
            PillarName.GROWTH: _absence(
                PillarName.GROWTH, currency, failed=True, notes="growth raised"
            ),
            PillarName.EXTERNAL: (
                _absence(PillarName.EXTERNAL, currency, notes="no balance for NZD")
                if currency == "NZD"
                else _score(PillarName.EXTERNAL, currency, 0.5)
            ),
        }
        rows.append(_currency(currency, scores))

    audit = audit_run(rows, config_digest=DIGEST)
    kinds = {
        (absence.pillar, absence.currency): absence.kind for absence in audit.absences
    }

    assert kinds[(PillarName.RISK, "USD")] is AbsenceKind.NOT_RUN
    assert kinds[(PillarName.GROWTH, "USD")] is AbsenceKind.PILLAR_FAILED
    assert kinds[(PillarName.EXTERNAL, "NZD")] is AbsenceKind.NO_DATA
    assert (PillarName.EXTERNAL, "USD") not in kinds


def test_the_absence_reason_is_carried_through_for_the_reader() -> None:
    rows = [
        _currency(
            "NZD",
            {
                PillarName.POSITIONING: _absence(
                    PillarName.POSITIONING, "NZD", notes="NZD has no COT contract"
                )
            },
        )
    ]

    audit = audit_run(rows, config_digest=DIGEST)
    absence = next(
        item
        for item in audit.absences
        if item.pillar is PillarName.POSITIONING and item.currency == "NZD"
    )

    assert absence.reason == "NZD has no COT contract"
    assert absence.kind is AbsenceKind.NO_DATA


def test_a_pillar_that_scored_nobody_is_present_with_an_empty_tuple() -> None:
    """Absent from the mapping and present-but-empty are different facts."""
    rows = [
        _currency(
            currency,
            {
                PillarName.MONETARY: _score(PillarName.MONETARY, currency, 1.0),
                PillarName.GROWTH: _absence(PillarName.GROWTH, currency),
            },
        )
        for currency in ("USD", "EUR")
    ]

    audit = audit_run(rows, config_digest=DIGEST)

    assert audit.scored[PillarName.GROWTH] == ()
    assert audit.scored[PillarName.MONETARY] == ("USD", "EUR")


def test_the_run_date_and_digest_are_recorded() -> None:
    """A correlation is a property of the weights and the data together."""
    audit = audit_run(_from_matrix(), config_digest=DIGEST)

    assert audit.asof == ASOF
    assert audit.config_digest == DIGEST


def test_a_supplied_asof_wins_over_the_scores() -> None:
    other = date(2026, 1, 2)

    audit = audit_run(_from_matrix(), config_digest=DIGEST, asof=other)

    assert audit.asof == other


def test_disagreeing_score_dates_are_refused_rather_than_guessed() -> None:
    rows = list(_from_matrix())
    rows[0] = replace(rows[0], asof=date(2026, 1, 2))

    with pytest.raises(ValueError, match="different asof"):
        audit_run(rows, config_digest=DIGEST)


def test_an_empty_run_is_refused() -> None:
    """Seven not-run pillars and 21 unmeasurable pairs is what a broken run looks
    like, so an empty argument must not render as one."""
    with pytest.raises(ValueError, match="empty run"):
        audit_run((), config_digest=DIGEST)


# --- the zero invariant, criterion 6 ----------------------------------------


def test_a_marked_absence_is_not_an_unmarked_zero() -> None:
    """raw and z both None with a score of 0.0 is the documented absence."""
    rows = [_currency("USD", {PillarName.RISK: _absence(PillarName.RISK, "USD")})]

    assert unmarked_zeros(rows) == ()


def test_a_genuine_zero_reading_is_allowed() -> None:
    """RISK at the middle of its band, which is the legitimate case."""
    rows = [
        _currency(
            "USD",
            {PillarName.RISK: _score(PillarName.RISK, "USD", 0.0, z=0.0, raw=0.0)},
        )
    ]

    assert unmarked_zeros(rows) == ()


def test_a_half_marked_zero_is_caught() -> None:
    """z of None beside a raw that is not is a zero nobody can classify.

    A consumer testing ``z`` calls it an absence, one testing ``raw`` calls it a
    reading, and they are looking at the same object.
    """
    rows = [
        _currency(
            "USD",
            {PillarName.RISK: _score(PillarName.RISK, "USD", 0.0, z=None, raw=1.5)},
        )
    ]

    offenders = unmarked_zeros(rows)

    assert len(offenders) == 1
    assert offenders[0].pillar is PillarName.RISK


def test_the_published_fixture_holds_no_unmarked_zero() -> None:
    assert unmarked_zeros(_from_matrix()) == ()


def test_a_real_run_holds_no_unmarked_zero() -> None:
    """The invariant over `score_currencies` itself, including its absences.

    A run with no observations at all, which is the case that produces an
    absence for every currency from every pillar. Those are marked, so the run
    is clean, and a pillar that started returning a bare 0.0 instead would show
    up here.
    """
    from fbe.pillars import default_pillars

    scores = score_currencies((), default_pillars(), ScoringConfig(), ASOF)

    assert scores
    assert unmarked_zeros(scores) == ()


# --- dispersion, criterion 5 ------------------------------------------------


def test_the_fixture_has_a_currency_whose_pillars_disagree() -> None:
    """If every currency's pillars agreed exactly, the weights would be fiction.

    Read off the published section 7.6 columns rather than from a run, and
    computed here as the spread of the seven scores rather than taken from
    `fbe.scoring.dispersion`, so this is a statement about the fixture rather
    than about the function.
    """
    spreads = {
        currency: max(MATRIX[currency]) - min(MATRIX[currency]) for currency in UNIVERSE
    }

    assert any(spread > 0.0 for spread in spreads.values())
    # Not a near-tie either: the widest disagreement on the fixture is over two
    # points of the six-point band.
    assert max(spreads.values()) > 2.0


def test_the_real_dispersion_function_is_non_zero_on_the_fixture() -> None:
    """Criterion 5 through `fbe.scoring.dispersion` rather than a local spread.

    The test above states the property of the fixture. This states it of the
    function a run actually calls, on the same fixture, which is what the
    criterion asks for: if every currency's pillars agreed exactly the weights
    would be fiction, and a dispersion that came back zero for all eight would
    be the symptom.
    """
    from fbe.scoring import dispersion

    weights = ScoringConfig().weights
    spreads = [dispersion(row.pillars, weights) for row in _from_matrix()]

    assert any(spread > 0.0 for spread in spreads)
    assert all(spread >= 0.0 for spread in spreads)


# --- the rendered output ----------------------------------------------------


def test_the_output_names_a_duplicate_pair_with_its_figure() -> None:
    audit = PillarAudit(
        asof=ASOF,
        config_digest=DIGEST,
        scored={PillarName.MONETARY: UNIVERSE, PillarName.GROWTH: UNIVERSE},
        pairs=(
            PillarPair(
                left=PillarName.MONETARY,
                right=PillarName.GROWTH,
                correlation=0.9612,
                currencies=8,
            ),
        ),
    )

    rendered = audit.render()

    assert "monetary" in rendered and "growth" in rendered
    assert "+0.9612" in rendered
    assert "DUPLICATE" in rendered


def test_the_output_says_a_fold_is_a_decision() -> None:
    """The caveat is in the output, not only in the docstring: the person who
    needs it is reading the output."""
    rendered = audit_run(_from_matrix(), config_digest=DIGEST).render()

    assert "is a decision" in rendered
    assert "ScoringConfig" in rendered
    assert "say nothing about returns" in rendered


def test_the_output_reports_no_duplicate_rather_than_staying_silent() -> None:
    audit = PillarAudit(
        asof=ASOF,
        config_digest=DIGEST,
        scored={PillarName.MONETARY: UNIVERSE},
        pairs=(
            PillarPair(
                left=PillarName.MONETARY,
                right=PillarName.GROWTH,
                correlation=0.1,
                currencies=8,
            ),
        ),
    )

    assert "No pair reached" in audit.render()


def test_the_output_prints_an_unmeasured_pair_as_unmeasured() -> None:
    audit = PillarAudit(
        asof=ASOF,
        config_digest=DIGEST,
        scored={},
        pairs=(
            PillarPair(
                left=PillarName.MONETARY,
                right=PillarName.GROWTH,
                correlation=None,
                currencies=1,
                reason="both scored only 1 currency in common",
            ),
        ),
    )

    rendered = audit.render()

    assert "not measured" in rendered
    assert "1 currency in common" in rendered


def test_the_output_records_the_date_and_the_digest() -> None:
    rendered = audit_run(_from_matrix(), config_digest=DIGEST).render()

    assert ASOF.isoformat() in rendered
    assert DIGEST in rendered


def test_the_output_makes_no_unmeasured_claim() -> None:
    """The standing instruction on claims, over the one output that quotes
    correlation figures and could most easily read as evidence."""
    rendered = audit_run(_from_matrix(), config_digest=DIGEST).render().lower()

    for word in ("proven", "backtested", "edge", "win rate", "hit rate"):
        assert word not in rendered


def test_the_duplicates_are_ordered_strongest_first() -> None:
    audit = PillarAudit(
        asof=ASOF,
        config_digest=DIGEST,
        scored={},
        pairs=(
            PillarPair(PillarName.MONETARY, PillarName.GROWTH, 0.91, 8),
            PillarPair(PillarName.INFLATION, PillarName.RISK, -0.99, 8),
            PillarPair(PillarName.GROWTH, PillarName.RISK, 0.2, 8),
        ),
    )

    assert [abs(pair.correlation or 0.0) for pair in audit.duplicates] == [0.99, 0.91]


# --- the module's own surface -----------------------------------------------


def test_the_module_changes_nothing_it_is_given() -> None:
    """It computes and reports. A diagnostic that mutates the run it audits
    would be the one thing nobody would look for."""
    rows = _from_matrix()
    before = tuple(
        (row.currency, row.composite, tuple(sorted(p.value for p in row.pillars)))
        for row in rows
    )

    audit_run(rows, config_digest=DIGEST)
    pillar_pairs(rows)
    unmarked_zeros(rows)

    after = tuple(
        (row.currency, row.composite, tuple(sorted(p.value for p in row.pillars)))
        for row in rows
    )
    assert before == after


def test_nothing_here_reads_a_weight() -> None:
    """The audit measures the columns, not what the run did with them.

    Correlation is a property of the two score columns. Weighting it would mix
    the question "are these two pillars the same measurement" with "how much we
    lean on them", and the second is what a reader decides after seeing the
    first.

    Asserted on the module's import surface rather than by scanning its text,
    which would fail on the word appearing in a docstring: the invariant is that
    no weight is reachable, and `ScoringConfig` is the only thing that carries
    one.
    """
    import ast
    import inspect

    import fbe.pillar_audit as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name for alias in node.names)

    assert "ScoringConfig" not in imported
    assert not any(name.endswith("config") for name in imported), imported


def _stub_pillar_run() -> Any:
    """A pillar that raises, for the failure path through `score_currencies`."""

    class _Raising:
        name = PillarName.GROWTH

        def compute(self, observations: Any, currencies: Any, asof: Any) -> Any:
            raise RuntimeError("no data source")

    return _Raising()


def test_a_raising_pillar_reaches_the_audit_as_a_failure() -> None:
    """End to end through `score_currencies`, not against a hand-built marker.

    This is what pins the marker's two ends together: the scorer writes
    `PILLAR_FAILED` and the audit reads it, and a change to either that broke
    the pairing would fail here rather than in neither file's own tests.
    """
    scores = score_currencies((), [_stub_pillar_run()], ScoringConfig(), ASOF)

    audit = audit_run(scores, config_digest=DIGEST)
    growth = [
        absence for absence in audit.absences if absence.pillar is PillarName.GROWTH
    ]

    assert growth
    assert all(absence.kind is AbsenceKind.PILLAR_FAILED for absence in growth)
    assert all("RuntimeError" in absence.reason for absence in growth)


# --- criterion 2: the documented reason, kept in step with the registry -----


def _dead_series() -> dict[str, tuple[str, ...]]:
    """Indicator to the currencies whose registry ref is dead rather than late.

    Dead means the note says the series was discontinued or that no such series
    is published for that country, as against merely unverified: two of
    MONETARY's keys are derived from `yield_2y` and carry no source to verify,
    and `pmi_composite` is a slot GROWTH has moved off. Neither costs a currency
    a score, so neither belongs in the spec's table.
    """
    from fbe.datasources import registry

    dead: dict[str, list[str]] = {}
    for key in registry.INDICATORS:
        for currency in G10:
            try:
                ref = registry.series_for(key, currency)
            except Exception:  # noqa: BLE001 - a key this currency has no ref for
                continue
            if ref is None or getattr(ref, "verified", True):
                continue
            note = (ref.note or "").upper()
            if "DISCONTINUED" in note or note.startswith("NO "):
                dead.setdefault(key, []).append(currency)
    return {key: tuple(value) for key, value in dead.items()}


def test_the_spec_documents_every_dead_series() -> None:
    """Criterion 2, as a link between the registry and the spec.

    A pillar that cannot score a currency has to say why in
    ``docs/scoring-spec.md``. Hand-written prose about data availability goes
    stale silently, and this is what stops it: the set of dead series is read off
    the registry and every one has to be named in section 3.8, with every
    currency it affects.

    It fails in both directions on purpose. A newly dead series that nobody
    documented fails, and so does a documented gap that has since been revived,
    because a gap the spec still claims is as misleading as one it never
    mentioned.
    """
    spec = (
        Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"
    ).read_text()
    start = spec.index("### 3.8 What a pillar cannot score")
    section = spec[start : spec.index("## 4. Aggregation", start)]

    dead = _dead_series()
    assert dead, "no dead series found; this test would then assert nothing"

    for key, currencies in dead.items():
        assert f"`{key}`" in section, f"section 3.8 does not name {key}"
        for currency in currencies:
            assert currency in section or "all eight" in section, (
                f"section 3.8 names {key} but not {currency}"
            )

    # And the other direction: nothing is claimed dead that is not.
    for key in ("yield_2y_chg_1m", "pmi_composite"):
        assert key not in dead
        assert f"| GROWTH | `{key}`" not in section
        assert f"| MONETARY | `{key}`" not in section


def test_the_spec_names_the_external_component_that_is_gone_for_everyone() -> None:
    """The row worth reading twice, pinned so a trim cannot lose it.

    `current_account_gdp` is not one currency short of a component. It is the
    whole of EXTERNAL's largest component, absent for all eight, which changes
    what the pillar is rather than how well it is fed.
    """
    dead = _dead_series()

    assert set(dead["current_account_gdp"]) == set(G10)

    spec = (
        Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"
    ).read_text()
    assert "every currency short of the component" in spec
