"""Sections 4 to 6 of the spec name their thresholds instead of writing numbers.

`docs/scoring-spec.md` is the implementation contract. Where it writes a bare
number that also lives in `ScoringConfig`, the two agree only at the defaults,
and an owner who follows section 8's re-weighting procedure ends up with two
readings of the same rule that both look correct. Issue #13 records the worked
case: with ``max_staleness_days = 30`` the spec's old ``s0 = S / 3`` and the
config's ``staleness_full_days`` put a 20-day-old pillar at an effective weight
of 0.150 and 0.200 respectively, on a 0.30 pillar. Neither raises.

So this is the config-drift trap the `engineering-standards` skill names, applied
to the document rather than to the code, and it is checked the same way: not by
reading the prose but by comparing it against `ScoringConfig`.

The rule enforced here is narrow on purpose. Prose may quote a number freely,
because "more than 5% of the expected move is paid to the broker" reads better
than the field name and misleads nobody. What may not happen is a **threshold
comparison** written against a bare number: a line that says what is compared
against what must name the field doing the comparing, so that a reader
implementing from the spec reads the same rule as one implementing from the
config.

Section 4.1 is inside the span this file checks and was deliberately left
untouched by the pull request that added it: 4.1's arithmetic belongs to issue
#8, which landed the per-indicator ramp, and its stamping convention to issue
#27. It passes the check as scoped. Read that as "nothing this check looks for
is wrong there" rather than as a clean bill of health, since the sweep covers
fractional defaults only and 4.1's figures are integers and worked results. Its
formula does name its fields.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from fbe.config import DataConfig, ScoringConfig

SPEC = Path(__file__).resolve().parents[1] / "docs" / "scoring-spec.md"

FIRST_SECTION = "## 4. Aggregation to a currency composite"
LAST_SECTION = "## 7. Worked example"

COMPARISON = re.compile(r"[<>]=?|\babove\b|\bbelow\b|\bexceeds\b")
"""What makes a line a threshold statement rather than a mention of a number.

A comparison is the thing that turns a number into a rule. ``horizon_days = 10``
inside a formula block is an assignment showing a default, which the criterion
explicitly allows; ``cost_ratio > 0.05`` is a rule, and it has to name the field.
"""


def _configured_values() -> Mapping[float, tuple[str, ...]]:
    """The fractional numeric defaults in `ScoringConfig` and `DataConfig`.

    Keyed by value rather than by name because that is the direction the check
    runs: it finds a number in the document and asks which field owns it. Two
    fields sharing a default (``min_agreement`` and ``min_coverage`` are both
    0.60) means either name satisfies the line, which is the right answer: the
    check is about the reader being able to find the field, not about guessing
    which of two it is.

    Integer-valued defaults are excluded, and the exclusion is a real limit on
    this sweep rather than a convenience. ``lookback_years`` is 5,
    ``horizon_days`` is 10, ``max_staleness_days`` is 45,
    ``calendar_blackout_after_min`` is 60. Those are small integers that occur
    throughout the prose as percentages, dates, counts and unrelated day
    figures: "more than 5% of the expected move", "2026-09-10", "ADR 0002 rule
    3". Sweeping for them produces findings that are nearly all false, and a
    check whose output is mostly noise gets an allowlist bolted on, which is the
    same silencing this file exists to prevent.

    So the sweep covers the fractional thresholds, which are distinctive enough
    to match only themselves, and the integer fields the issue names are covered
    by ``test_each_section_names_the_fields_it_depends_on`` instead, which asks
    whether the name is present rather than hunting the number.
    """
    values: dict[float, list[str]] = {}
    for config in (ScoringConfig, DataConfig):
        for field in dataclasses.fields(config):
            default = field.default
            if isinstance(default, bool) or not isinstance(default, int | float):
                continue
            if float(default).is_integer():
                continue
            values.setdefault(float(default), []).append(field.name)
    return {value: tuple(names) for value, names in values.items()}


def _spec_lines() -> list[str]:
    return SPEC.read_text().splitlines()


def _sections_four_to_six(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(line number, text)`` for the span this check covers."""
    start = next(i for i, line in enumerate(lines) if line.startswith(FIRST_SECTION))
    end = next(i for i, line in enumerate(lines) if line.startswith(LAST_SECTION))
    for i in range(start, end):
        yield i + 1, lines[i]


def _written_forms(value: float) -> set[str]:
    """How a fractional default might legitimately be typed in the document.

    0.8 appears as ``0.8`` in one place and ``0.80`` in another, and both are
    the same threshold. Matching the written forms rather than parsing every
    number in the line keeps the check anchored to values that exist in config.
    """
    return {f"{value:g}", f"{value:.1f}", f"{value:.2f}"}


def _unnamed_thresholds() -> list[str]:
    """Lines stating a threshold against a configured value without naming it.

    The field name is accepted on the line itself or on the one before it, since
    the document wraps at 80 columns and a name can legitimately end up on the
    previous line from the number it governs.
    """
    lines = _spec_lines()
    values = _configured_values()
    findings = []

    for number, line in _sections_four_to_six(lines):
        if not COMPARISON.search(line):
            continue
        previous = lines[number - 2] if number >= 2 else ""
        context = f"{previous}\n{line}"
        for value, names in values.items():
            if any(name in context for name in names):
                continue
            for form in _written_forms(value):
                if re.search(rf"(?<![\w.]){re.escape(form)}(?![\w.])", line):
                    findings.append(
                        f"line {number}: {form} is "
                        f"{' or '.join(names)} but no field is named\n"
                        f"    {line.strip()}"
                    )
                    break
    return findings


def test_no_threshold_in_sections_four_to_six_is_written_as_a_bare_number() -> None:
    """Criterion 3 of issue #13, as a check that runs rather than a grep once.

    A grep run by hand closes the issue and then rots. This file's own reason
    for existing is that the document and the config drift apart silently, and a
    one-off check has exactly that failure mode.

    If this fails, name the field on the offending line rather than deleting the
    number. The criterion allows the default in parentheses beside the name.
    """
    findings = _unnamed_thresholds()

    assert not findings, "unnamed thresholds in sections 4 to 6:\n" + "\n".join(
        findings
    )


@pytest.mark.parametrize(
    ("section", "field_names"),
    [
        (
            "### 5.4 Conviction",
            (
                "min_spread_low",
                "min_spread_medium",
                "min_spread_high",
                "min_agreement",
                "coverage_demotion",
                "max_dispersion",
            ),
        ),
        (
            "## 6. Hard filters",
            ("max_cost_ratio", "min_coverage", "horizon_days"),
        ),
    ],
)
def test_each_section_names_the_fields_it_depends_on(
    section: str, field_names: tuple[str, ...]
) -> None:
    """Criterion 2, stated positively so a deleted rule is caught too.

    The check above only sees numbers that are present. If a row were removed
    from a table the bare-number check would pass on the absence, so the fields
    each section is built on are listed here by name.
    """
    lines = _spec_lines()
    start = next(i for i, line in enumerate(lines) if line.startswith(section))
    end = next(
        i
        for i, line in enumerate(lines)
        if i > start and line.startswith(("## ", "### "))
    )
    body = "\n".join(lines[start:end])

    missing = [name for name in field_names if name not in body]
    assert not missing, f"{section} does not name {missing}"


def test_the_check_notices_a_threshold_written_as_a_bare_number() -> None:
    """The check above is worthless if it cannot fail, so prove that it can.

    Rewrites one real line of section 6 into the form this issue exists to
    forbid, runs the same detector over it, and asserts the line is reported.
    Nothing is written to disk.
    """
    lines = _spec_lines()
    values = _configured_values()
    offender = "| `cost` | yes | `cost_ratio > 0.05` | Dealing cost eats too much |"

    assert COMPARISON.search(offender)
    names = values[ScoringConfig().max_cost_ratio]
    assert "max_cost_ratio" in names
    assert not any(name in offender for name in names)
    assert any(
        re.search(rf"(?<![\w.]){re.escape(form)}(?![\w.])", offender)
        for form in _written_forms(ScoringConfig().max_cost_ratio)
    )

    # And the real document does not contain that line any more.
    assert offender not in "\n".join(lines)
