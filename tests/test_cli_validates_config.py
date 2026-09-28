"""The config guard that `docs/interfaces.md` specified and nothing built: #188.

`Config.validate` catches a config whose thresholds are ordered wrongly. Until
this change it had exactly one caller in the package, inside `_check_config`,
reached only from `fbe doctor`. So the one command that asked was the diagnostic
one, which computes no score and sizes nothing, and every command that does
compute ran on whatever it was given.

That is the prime directive's case rather than a crash. An empty LOW conviction
band does not raise: `bias.conviction_for` reads the bands in order and a
marginal spread that should have graded LOW comes back MEDIUM, which is a larger
position on a pair the model is least sure of. Nothing in the output says the
band was empty, and the one command that would have said so is the one the
operator had no reason to run because nothing looked wrong.

The failure scenario the issue was filed with is not reproducible today, and the
tests below use a different one. It set ``staleness_full_days`` above
``max_staleness_days`` to inflate the staleness ramp. #126 and #222 replaced that
ramp with the per-leg allowance in `fbe.datasources.registry`, the field is gone
from `ScoringConfig`, and `load_config` now refuses an unknown key before
`validate` is ever reached, so a test written to the issue's literal YAML would
fail on the load rather than on the guard. The conviction bands are the nearest
live equivalent: named in `Config.validate`'s own docstring, invisible
downstream, and the same shape of defect.

Nothing here reaches the network. Every test that gets past the guard replaces
`fbe.cli.collect`, and every test that does not asserts it was never called.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

import fbe.cli as cli_module
from fbe.bias import conviction_for
from fbe.cli import EXIT_OK, EXIT_UNUSABLE, app
from fbe.config import Config, ScoringConfig, load_config
from fbe.datasources.collect import CollectionResult
from fbe.types import Conviction, CurrencyScore, Frequency, Observation

runner = CliRunner()

ASOF = date(2026, 9, 9)

ONE_PROBLEM = """
scoring:
  min_spread_medium: 0.75
"""
"""An empty LOW conviction band, which `Config.validate` rejects.

The default bands are 0.75, 1.50 and 2.50. Dropping the medium threshold onto
the low one leaves no spread that can grade LOW, so every pair that would have
been the model's weakest directional call is promoted to MEDIUM and to the
position size that carries. Every key here exists, so `load_config` accepts the
file and the refusal has to come from the guard.
"""

TWO_PROBLEMS = """
scoring:
  min_spread_medium: 0.75
  min_coverage: 0.90
"""
"""The same, plus a coverage floor above `coverage_demotion`'s default 0.80.

Two independent problems, so a guard that stops at the first leaves the operator
running the command again to find the second.
"""

VALID = """
scoring:
  min_spread_low: 0.70
"""
"""A config the guard must not refuse. It differs from the defaults so the file
is demonstrably read, and every ordering still holds."""


def write(tmp_path: Path, body: str) -> Path:
    """Write a config file under ``tmp_path`` and return its path.

    Never the repository's own `config.yaml`: a test that read the real file
    would pass or fail on whatever an operator last left there.
    """
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


OBSERVATION = Observation(
    indicator="yield_2y",
    currency="USD",
    value=4.25,
    period=date(2026, 9, 1),
    source="fixture",
    series_id="FIXTURE",
    unit="percent",
    frequency=Frequency.DAILY,
)
"""One observation, so `CollectionResult.usable` is true and a run that gets past
the guard is not stopped by the cold-cache check instead."""

UNIVERSE: tuple[CurrencyScore, ...] = (
    CurrencyScore(
        currency="USD",
        composite=1.42,
        pillars={},
        asof=ASOF,
        rank=1,
        dispersion=0.61,
        coverage=1.0,
    ),
    CurrencyScore(
        currency="JPY",
        composite=-1.18,
        pillars={},
        asof=ASOF,
        rank=2,
        dispersion=1.31,
        coverage=0.71,
    ),
)
"""Two currencies, because nothing here counts them."""


def refuse_collect(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `fbe.cli.collect` with one that fails if it is ever reached.

    The criterion is that the refusal happens *before* any number is computed,
    which an exit code alone cannot show: a command that collected, scored and
    then refused would exit the same way. Returns the list the call records into,
    so a test asserts it is still empty.
    """
    calls: list[str] = []

    def fake_collect(_config: object, **_kwargs: object) -> CollectionResult:
        calls.append("collect")
        raise AssertionError("the guard let the command reach the data layer")

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    return calls


def allow_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the collection and the scorer so a valid config reaches an exit 0."""

    def fake_collect(_config: object, **_kwargs: object) -> CollectionResult:
        return CollectionResult(observations=(OBSERVATION,), outcomes=(), gaps={})

    def fake_score_currencies(
        _observations: Sequence[Observation],
        _pillars: Sequence[object],
        _config: object,
        _asof: date,
    ) -> Sequence[CurrencyScore]:
        return UNIVERSE

    monkeypatch.setattr("fbe.cli.collect", fake_collect)
    monkeypatch.setattr("fbe.cli.score_currencies", fake_score_currencies)


# ----------------------------------------------------------------------
# The three implemented commands refuse
# ----------------------------------------------------------------------


@pytest.mark.parametrize("command", ["score", "bias"])
def test_a_scoring_command_refuses_an_invalid_config(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Criteria 1 and 2. Exit 1, the problem named, and no table printed.

    The table is asserted absent by its own header rather than by the output
    being empty, because the refusal itself prints lines.
    """
    path = write(tmp_path, ONE_PROBLEM)
    calls = refuse_collect(monkeypatch)

    result = runner.invoke(
        app, ["--config", str(path), command, "--asof", "2026-09-09"]
    )

    assert result.exit_code == EXIT_UNUSABLE
    assert calls == []
    message = result.stdout + result.stderr
    assert "min_spread_low" in message
    assert "min_spread_medium" in message
    assert "Composite" not in message
    assert "Conviction" not in message


def test_report_refuses_an_invalid_config_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The criterion the audit desk asked for on 26 September.

    `report` was implemented after this issue was filed and is equally
    unguarded. It writes the committed audit trail, and `--compare` reads
    yesterday's file back as today's baseline, so a report written under a bad
    config is not merely wrong once: re-running the date does not recover the
    correct call, and tomorrow's diff reads the mistake as a fundamental move.
    The exit code is not the assertion that matters here. The empty directory is.
    """
    path = write(tmp_path, ONE_PROBLEM)
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    calls = refuse_collect(monkeypatch)

    result = runner.invoke(
        app,
        [
            "--config",
            str(path),
            "report",
            "--asof",
            "2026-09-09",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == EXIT_UNUSABLE
    assert calls == []
    assert list(out_dir.iterdir()) == []


def test_every_problem_is_printed_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both problems, each on its own line.

    `Config.validate` accumulates rather than short-circuiting, and its docstring
    gives the reason: a config wrong in three places should say so once, not over
    three runs. A guard that printed `problems[0]` would undo that and nothing
    else would notice.
    """
    path = write(tmp_path, TWO_PROBLEMS)
    refuse_collect(monkeypatch)

    result = runner.invoke(
        app, ["--config", str(path), "score", "--asof", "2026-09-09"]
    )

    lines = (result.stdout + result.stderr).splitlines()
    assert result.exit_code == EXIT_UNUSABLE
    spread = [line for line in lines if "min_spread_low" in line]
    coverage = [line for line in lines if "min_coverage" in line]
    assert len(spread) == 1
    assert len(coverage) == 1
    assert spread[0] != coverage[0]


def test_the_refusal_goes_to_stderr_so_a_redirected_csv_stays_a_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``fbe score --format csv > monday.csv`` must not write prose into the file.

    On stdout the refusal lands where the parser expects rows, and the operator,
    who is looking at an empty terminal, sees nothing at all. Both halves are the
    wrong way round: the message belongs on the screen and the file belongs
    empty. `bias` already sends its run-level notes to stderr in the CSV path for
    the same reason.
    """
    path = write(tmp_path, ONE_PROBLEM)
    refuse_collect(monkeypatch)

    result = runner.invoke(
        app,
        ["--config", str(path), "score", "--format", "csv", "--asof", "2026-09-09"],
    )

    assert result.exit_code == EXIT_UNUSABLE
    assert "min_spread_low" in result.stderr
    assert result.stdout.strip() == ""


def test_a_valid_config_still_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Criterion 8. A guard that refused everything would pass every test above.

    The file moves `min_spread_low` off its default, so this also shows the
    command is reading the file rather than validating the packaged defaults.
    """
    path = write(tmp_path, VALID)
    assert load_config(path).scoring.min_spread_low == 0.70
    assert load_config(path).validate() == []
    allow_run(monkeypatch)

    result = runner.invoke(
        app, ["--config", str(path), "score", "--asof", "2026-09-09"]
    )

    assert result.exit_code == EXIT_OK
    assert "USD" in result.stdout


# ----------------------------------------------------------------------
# What the guard must not change
# ----------------------------------------------------------------------


def test_doctor_still_reports_rather_than_refusing(tmp_path: Path) -> None:
    """Criterion 4. `doctor` exists to print these problems.

    Routing it through the refusal would hide the very thing the operator ran it
    to see, which is why `_effective_config`'s docstring commits to resolving
    without judging. Its exit code on a failing check is unchanged at 1, so the
    assertion is about what reaches the screen: every problem, under the check
    layout, with the other checks still run.
    """
    path = write(tmp_path, TWO_PROBLEMS)

    result = runner.invoke(app, ["--config", str(path), "doctor"])

    assert result.exit_code == EXIT_UNUSABLE
    assert "min_spread_low" in result.stdout
    assert "min_coverage" in result.stdout
    assert "broker" in result.stdout


@pytest.mark.parametrize(
    "args", [["--help"], ["score", "--help"], ["report", "--help"]]
)
def test_help_works_with_an_invalid_config_in_place(
    args: list[str], tmp_path: Path
) -> None:
    """Criterion 5. Resolution stays lazy, so help is not gated on a good config.

    An operator whose config is wrong is exactly the operator reaching for
    ``--help``. Putting the guard in the group callback would refuse them there.
    """
    path = write(tmp_path, ONE_PROBLEM)

    result = runner.invoke(app, ["--config", str(path), *args])

    assert result.exit_code == EXIT_OK
    assert "min_spread_low" not in result.stdout


# ----------------------------------------------------------------------
# One helper, not three copies
# ----------------------------------------------------------------------


def _callers_of(name: str) -> set[str]:
    """Return the `fbe.cli` functions whose body calls ``name``.

    Read from the source rather than by patching, because the criterion is about
    where the call lives. A command that inlined `config.validate()` would behave
    identically today and would be the copy the next command is written from.
    """
    tree = ast.parse(inspect.getsource(cli_module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            named = isinstance(func, ast.Name) and func.id == name
            attribute = isinstance(func, ast.Attribute) and func.attr == name
            if named or attribute:
                found.add(node.name)
    return found


def test_validate_is_called_from_exactly_two_places() -> None:
    """Criterion 3, the half that keeps the next command from repeating it.

    Two callers and no more. `_check_config` is `doctor`'s, which reports, and
    the guard is everyone else's, which refuses. They cannot be one function
    because they do opposite things with the same list, and any third caller is
    a command that copied the check instead of calling it.
    """
    assert _callers_of("validate") == {"_check_config", "_require_valid_config"}


@pytest.mark.parametrize("command", ["score", "bias", "report"])
def test_each_scoring_command_calls_the_guard(command: str) -> None:
    """Criterion 3, the half that says the guard is actually wired.

    `dashboard` and `size` are still stubs and are deliberately not asserted
    here. They are named in `docs/interfaces.md` as commands that must validate,
    and the point of the shared helper is that they pick it up by calling it
    when they land.
    """
    assert command in _callers_of("_require_valid_config")


# ----------------------------------------------------------------------
# Why the guard cannot live inside the arithmetic
# ----------------------------------------------------------------------


def test_the_arithmetic_cannot_tell_an_empty_band_from_a_valid_one() -> None:
    """Criterion 9, restated against the bands rather than the retired ramp.

    The issue asked for this on `scoring.freshness` with ``staleness_full_days``
    above ``max_staleness_days``. That field no longer exists, so the same point
    is made where it is still live. A spread of 1.00 sits in the LOW band under
    the defaults. Drop ``min_spread_medium`` onto the low threshold and the band
    is empty, and `conviction_for` returns MEDIUM for the identical inputs: no
    exception, no marker, a larger size on the model's least certain call.

    That is the argument for refusing before any number is computed rather than
    checking inside the formula. The formula has no way to know.
    """
    shipped = ScoringConfig()
    broken = ScoringConfig(min_spread_medium=shipped.min_spread_low)
    arguments = (1.00, 1.0, 1.0, 0.2, False)

    assert shipped.min_spread_low == 0.75
    assert conviction_for(*arguments, shipped) is Conviction.LOW
    assert conviction_for(*arguments, broken) is Conviction.MEDIUM

    # The same config that the formula ran on without complaint. This is the
    # pairing that makes the point: the only thing standing between the operator
    # and that MEDIUM is a caller of `validate`, and until this change the
    # scoring commands were not one.
    problems = Config(scoring=broken).validate()
    assert any("min_spread_low" in problem for problem in problems)
