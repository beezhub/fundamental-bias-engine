"""The ``--risk`` option on ``fbe size`` does not restate the configured band.

The defect these tests pin down. The option carried ``min=0.0, max=0.02``,
which Typer enforces at parse time, before ``--config`` has been read. ``0.02``
was `RiskConfig.risk_per_trade_max` written a second time, and ``0.0`` was not
`RiskConfig.risk_per_trade_min` at all. An operator who lowered their ceiling
to 1.5% after a drawdown, as ``docs/risk-and-execution.md`` tells them to, was
still told by ``--help`` and by the parser's error that the ceiling was 2%.
This is the config-drift trap `engineering-standards` opens with, on the one
surface where the owner types a risk number by hand.

The fix is deletion: `fbe.risk.position_size` already clamps to the configured
band and says so in a warning, so the parser has nothing to add except a floor
at zero, which is a sanity check and not a policy.

``size`` is still a stub, so every invocation that parses ends in
`NotImplementedError` inside the command. The tests here assert on the
parsing step alone: a usage error exits 2 with Click's range message, and
anything else means the number reached the command.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from typer.testing import CliRunner

import fbe.cli as cli_module
from fbe.cli import app
from fbe.config import RiskConfig

runner = CliRunner()

USAGE_ERROR = 2
"""Click's exit code for a value the parser refused."""

RANGE_MESSAGE = "is not in the range"
"""The phrase Click prints when ``min`` or ``max`` on an option refuses a
value. Its presence is what tells a parser refusal from anything else."""


def _config_with_ceiling(tmp_path: Path, ceiling: float) -> Path:
    """Write a config that lowers the per-trade ceiling below the default."""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
        f"risk:\n"
        f"  risk_per_trade_max: {ceiling}\n"
    )
    return path


def _size(path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["--config", str(path), "size", "EURUSD", "--entry", "1.085", "--stop"]
        + ["1.0835", *args],
    )
    return result.exit_code, result.output


def test_a_risk_above_the_shipped_ceiling_reaches_the_command(tmp_path: Path) -> None:
    """Criterion 4. The ceiling is 0.015 in config, and 0.018 must parse.

    Under the old option 0.018 parsed too, against a 0.02 the config no
    longer held, and 0.03 did not. The number that shows the parser has
    stopped restating the band is one above the default ceiling.
    """
    path = _config_with_ceiling(tmp_path, 0.015)

    code, output = _size(path, "--risk", "0.03")

    assert code != USAGE_ERROR
    assert RANGE_MESSAGE not in output

    code, output = _size(path, "--risk", "0.018")

    assert code != USAGE_ERROR
    assert RANGE_MESSAGE not in output


def test_a_negative_risk_is_still_refused_by_the_parser(tmp_path: Path) -> None:
    """Criterion 5. Removing the ceiling did not remove the floor at zero.

    A negative fraction is nonsense rather than a policy question, which is
    the one ground the floor stays on.
    """
    path = _config_with_ceiling(tmp_path, 0.015)

    code, output = _size(path, "--risk", "-0.01")

    assert code == USAGE_ERROR
    assert RANGE_MESSAGE in output


def test_the_help_names_the_config_and_quotes_no_band(tmp_path: Path) -> None:
    """Criteria 3 and 6. ``--help`` under a lowered ceiling prints no range.

    The old text advertised ``0.0<=x<=0.02`` and "the plan's 1-2% band" as
    facts about this run. Both are facts about the default config.
    """
    path = _config_with_ceiling(tmp_path, 0.015)

    result = runner.invoke(app, ["--config", str(path), "size", "--help"])
    help_text = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "RiskConfig" in help_text
    for forbidden in ("0.02", "0.01 ", "1-2%", "1%", "2%", "<=x<="):
        assert forbidden not in help_text, forbidden


def test_the_option_carries_no_max_and_no_configured_min() -> None:
    """Criterion 1, read off the option itself rather than its behaviour."""
    source = ast.parse(inspect.getsource(cli_module.size))
    keywords = {
        keyword.arg: keyword.value
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and any(
            isinstance(arg, ast.Constant) and arg.value == "--risk" for arg in node.args
        )
        for keyword in node.keywords
    }

    assert "max" not in keywords
    floor = keywords.get("min")
    assert floor is None or (isinstance(floor, ast.Constant) and floor.value == 0.0)


def test_no_literal_in_the_cli_duplicates_the_risk_band() -> None:
    """Criterion 2. Neither end of the configured band appears as a number.

    Checked against the live `RiskConfig` defaults rather than against 0.01
    and 0.02 written here, so the test follows the config if it moves.
    """
    band = {RiskConfig().risk_per_trade_min, RiskConfig().risk_per_trade_max}
    source = ast.parse(Path(inspect.getsourcefile(cli_module) or "").read_text())
    literals = {
        node.value
        for node in ast.walk(source)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }

    assert not literals & band, literals & band
