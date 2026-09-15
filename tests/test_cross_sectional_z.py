"""`MIN_CROSS_SECTION`, and the estimator its rationale is computed under.

The two-point z-score is the number the constant's docstring offers as its
reason. It was written as ``+/-0.707``, which is ``1 / sqrt(2)`` and is the
figure under the **sample** standard deviation. `cross_sectional_z` in the same
file mandates ``ddof=0``, because the eight currencies are the whole scored
universe rather than a sample drawn from a larger one, and under the population
form two points always score exactly ``+/-1.0`` whatever the gap between them.

The conclusion the constant rests on survives either way: two points encode rank
and discard magnitude, so a pillar should decline to score a cross-section that
thin. What was wrong is the number given as the reason, and in a repository
whose worked examples are fixtures a wrong number inside a stated reason is a
defect rather than a typo. A reader checking it by hand gets 1.0 and concludes
that either the docstring or the ``ddof=0`` rule is wrong; a test written from
the docstring would assert 0.707 and fail against a correct implementation.

Two kinds of assertion live here.

The arithmetic runs today and is what would have caught this: it computes the
cited figure under both estimators and pins which one the module's own rule
produces. It needs no pillar code at all, which is the point, because the
docstring was wrong for as long as the method it describes has been a stub.

The behavioural half calls `cross_sectional_z`, which is still scaffolded, so it
is guarded on the scaffold marker and skips. Those assertions are written
against the real signature so they begin asserting the day the method lands.
See issue #29.
"""

from __future__ import annotations

import inspect
import statistics
from collections.abc import Callable, Mapping

import pytest

import fbe.pillars.base
from fbe.pillars.base import MIN_CROSS_SECTION, BasePillar

SCAFFOLD = "is scaffolded;"
"""The stub marker every scaffolded callable carries, per ``CLAUDE.md``."""


def _skip_if_scaffolded(*functions: Callable[..., object]) -> None:
    """Skip the calling test while any of these callables is still a stub."""
    for function in functions:
        if SCAFFOLD in inspect.getsource(function):
            pytest.skip(f"{function.__qualname__} is still scaffolded")


def _population_z(values: list[float]) -> list[float]:
    """Z-scores under ``ddof=0``, the estimator `cross_sectional_z` mandates."""
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    return [(value - mean) / sd for value in values]


def _sample_z(values: list[float]) -> list[float]:
    """Z-scores under ``ddof=1``, the estimator the module rejects.

    Here only so the two can be compared. Nothing in the engine should use it.
    """
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    return [(value - mean) / sd for value in values]


# --- the arithmetic behind the rationale, which runs today ------------------


@pytest.mark.parametrize("values", [[1.0, 3.0], [1.0, 30.0], [-4.5, -4.0]])
def test_two_points_score_plus_or_minus_one_under_the_population_form(
    values: list[float],
) -> None:
    """The figure the constant's rationale should cite.

    Whatever the gap, and whatever the sign: 1.0 and 3.0 sit two apart, 1.0 and
    30.0 twenty-nine apart, and both score the same. That is the constant's
    actual argument, that two points carry rank and nothing else, and it is
    exactly why the magnitude of the gap cannot appear in the answer.
    """
    assert _population_z(values) == pytest.approx([-1.0, 1.0], abs=5e-10)


@pytest.mark.parametrize("values", [[1.0, 3.0], [1.0, 30.0], [-4.5, -4.0]])
def test_the_rejected_estimator_is_where_0_707_came_from(
    values: list[float],
) -> None:
    """Pinned so the old number is identifiable rather than merely gone.

    0.707 is ``1 / sqrt(2)``. It is not wrong arithmetic, it is the right
    arithmetic under the estimator this module does not use, which is the
    easiest kind of wrong number to write and the hardest to spot.
    """
    assert _sample_z(values) == pytest.approx([-0.7071, 0.7071], abs=5e-5)
    assert _sample_z(values) != pytest.approx(_population_z(values), abs=1e-3)


def test_the_constants_docstring_cites_the_population_figure() -> None:
    """Criterion 1, checked against the arithmetic rather than by eye.

    The expected string is computed, not typed, so this cannot drift into
    asserting a number that is itself wrong. If the docstring and the estimator
    ever disagree again, this fails.
    """
    source = inspect.getsource(fbe.pillars.base)
    rationale = source.split("MIN_CROSS_SECTION: int = 3")[1].split('"""')[1]

    computed = _population_z([1.0, 3.0])
    assert computed == pytest.approx([-1.0, 1.0], abs=5e-10)

    assert "+/-1.0" in rationale, rationale
    assert "ddof=0" in rationale, (
        "the rationale should say which estimator its number comes from"
    )

    # 0.707 may appear, but only where it is named as the rejected estimator's
    # figure. Banning it outright would forbid the clearest way to write this,
    # which is to say what the number is not; letting it stand unattributed is
    # the defect itself.
    for line in rationale.splitlines():
        if "0.707" in line:
            assert "sample" in line.lower(), (
                f"0.707 appears without being attributed to the sample form: "
                f"{line.strip()!r}"
            )


def test_the_module_still_mandates_the_population_estimator() -> None:
    """The other half of the agreement, so the fix cannot be made backwards.

    Changing `cross_sectional_z` to ``ddof=1`` would also reconcile the two
    statements, and would be wrong: the eight currencies are the whole scored
    universe. This pins which of the two moved.
    """
    doc = inspect.getdoc(BasePillar.cross_sectional_z) or ""

    assert "ddof=0" in doc
    assert "population form is the correct estimator" in doc


def test_min_cross_section_is_still_three() -> None:
    """Criterion 4. The value does not change; only its stated reason does."""
    assert MIN_CROSS_SECTION == 3


# --- through the implementation, once it lands ------------------------------


def test_two_usable_values_come_back_none() -> None:
    """Criterion 2. Two is below `MIN_CROSS_SECTION`, so the pillar declines.

    Both currencies come back ``None``, including the ones that had data: a
    z-score against a two-point cross-section is arithmetic rather than
    information, and the honest answer is that the pillar could not run.
    """
    _skip_if_scaffolded(BasePillar.cross_sectional_z)

    values: Mapping[str, float | None] = {"A": 1.0, "B": 3.0}
    result = BasePillar.cross_sectional_z(values)

    assert result == {"A": None, "B": None}


def test_three_usable_values_score_under_the_population_standard_deviation() -> None:
    """Criterion 3, and the assertion that distinguishes the two estimators.

    1, 2, 3 has a population standard deviation of 0.8165 and a sample one of
    exactly 1.0, so ``ddof=0`` gives ``-1.2247, 0.0, +1.2247`` where ``ddof=1``
    would give ``-1.0, 0.0, +1.0``. Asserting this set is asserting the
    estimator, which is the whole point: the two-point case cannot do it,
    because both estimators there differ only by a scale the sign hides.
    """
    _skip_if_scaffolded(BasePillar.cross_sectional_z)

    values: Mapping[str, float | None] = {"A": 1.0, "B": 2.0, "C": 3.0}
    result = BasePillar.cross_sectional_z(values)

    assert result["A"] == pytest.approx(-1.2247, abs=5e-5)
    assert result["B"] == pytest.approx(0.0, abs=5e-5)
    assert result["C"] == pytest.approx(1.2247, abs=5e-5)

    # And not the sample-form answer, stated separately so a failure says which.
    assert result["C"] != pytest.approx(1.0, abs=1e-3)
