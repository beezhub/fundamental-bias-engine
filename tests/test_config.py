"""Tests for the run configuration.

Two things matter here. The pillar weights must sum to 1.0, or the composite
score is on an unstated scale and the conviction thresholds mean nothing. And
``digest()`` must be stable, because a report that cannot be tied back to the
weights that produced it cannot be evaluated later.

A third concern is provenance. ``RiskConfig`` must say which limits the trading
plan states and which the engine derived, because the plan wins any conflict and
a derived limit wrongly attributed to it cannot be argued with.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import typer
import typer.main
from typer.testing import CliRunner

import fbe.config
from fbe.cli import GlobalOptions, _effective_config, app
from fbe.config import (
    REPO_ROOT,
    Config,
    ConfigError,
    DataConfig,
    RiskConfig,
    ScoringConfig,
    load_config,
)
from fbe.types import PillarName

WEIGHT_TOLERANCE = 1e-9


def _weights(**overrides: float) -> dict[PillarName, float]:
    """Default weights with named pillars overridden."""
    weights = dict(ScoringConfig().weights)
    for name, value in overrides.items():
        weights[PillarName(name)] = value
    return weights


def test_default_weights_sum_to_one(default_config: Config) -> None:
    total = sum(default_config.scoring.weights.values())
    assert total == pytest.approx(1.0, abs=WEIGHT_TOLERANCE)


def test_every_pillar_has_a_weight(default_config: Config) -> None:
    assert set(default_config.scoring.weights) == set(PillarName)


def test_no_weight_is_negative(default_config: Config) -> None:
    assert all(w >= 0.0 for w in default_config.scoring.weights.values())


def test_monetary_carries_the_largest_weight(default_config: Config) -> None:
    weights = default_config.scoring.weights
    assert weights[PillarName.MONETARY] == max(weights.values())


def test_validate_returns_empty_for_defaults(default_config: Config) -> None:
    assert default_config.validate() == []


def test_validate_catches_weights_that_do_not_sum_to_one() -> None:
    config = Config(scoring=ScoringConfig(weights=_weights(monetary=0.50)))
    problems = config.validate()
    assert len(problems) == 1
    assert "weights sum to" in problems[0]


def test_validate_catches_weights_summing_below_one() -> None:
    config = Config(scoring=ScoringConfig(weights=_weights(risk=0.0)))
    assert any("weights sum to" in p for p in config.validate())


def test_validate_catches_a_risk_cap_above_two_percent() -> None:
    config = Config(risk=RiskConfig(risk_per_trade_max=0.05))
    problems = config.validate()
    assert len(problems) == 1
    assert "trading plan" in problems[0]


def test_two_percent_exactly_is_allowed() -> None:
    config = Config(risk=RiskConfig(risk_per_trade_max=0.02))
    assert config.validate() == []


def test_validate_catches_min_risk_above_max_risk() -> None:
    config = Config(risk=RiskConfig(risk_per_trade_min=0.02, risk_per_trade_max=0.01))
    problems = config.validate()
    assert any("exceeds risk_per_trade_max" in p for p in problems)


def test_validate_reports_every_problem_at_once() -> None:
    config = Config(
        risk=RiskConfig(risk_per_trade_min=0.09, risk_per_trade_max=0.05),
        scoring=ScoringConfig(weights=_weights(growth=0.40)),
    )
    assert len(config.validate()) == 3


# --- threshold orderings, issue #16 ------------------------------------------
#
# Each of these configs passed ``validate()`` before the fix and produced a
# downstream formula that was undefined, unreachable or sign-inverted. Every
# problem string must name both offending fields and their values, so the
# message can be acted on without opening the source.


def _scoring_problems(**overrides: Any) -> list[str]:
    """Problems for the default scoring config with fields overridden."""
    return Config(scoring=ScoringConfig(**overrides)).validate()


def test_staleness_ramp_start_above_its_end_is_rejected() -> None:
    """Scenario one: with s0 > S the ramp's denominator is negative."""
    problems = _scoring_problems(max_staleness_days=10)
    assert len(problems) == 1
    assert "staleness_full_days" in problems[0]
    assert "max_staleness_days" in problems[0]
    assert "15" in problems[0] and "10" in problems[0]


def test_coverage_demotion_below_the_hard_floor_is_rejected() -> None:
    """Scenario two: the demotion can never fire below the hard filter."""
    problems = _scoring_problems(coverage_demotion=0.50)
    assert len(problems) == 1
    assert "min_coverage" in problems[0]
    assert "coverage_demotion" in problems[0]
    assert "0.6" in problems[0] and "0.5" in problems[0]


def test_spread_tiers_out_of_order_are_rejected() -> None:
    """Scenario three: LOW becomes an empty band and 0.80 jumps to MEDIUM."""
    problems = _scoring_problems(min_spread_medium=0.50)
    assert len(problems) == 1
    assert "min_spread_low" in problems[0]
    assert "min_spread_medium" in problems[0]
    assert "0.75" in problems[0] and "0.5" in problems[0]


def test_negative_weight_summing_to_one_is_rejected() -> None:
    """Scenario four: the sum is 1.0 and the heaviest pillar subtracts."""
    config = Config(
        scoring=ScoringConfig(weights=_weights(monetary=-0.10, growth=0.55))
    )
    problems = config.validate()
    assert len(problems) == 1
    assert "monetary" in problems[0]
    assert "-0.1" in problems[0]


def test_pillar_missing_from_weights_is_rejected() -> None:
    """Switching a pillar off by omission hides the intent; 0.0 shows it."""
    weights = _weights(monetary=0.40)
    del weights[PillarName.RISK]
    assert sum(weights.values()) == pytest.approx(1.0)
    problems = Config(scoring=ScoringConfig(weights=weights)).validate()
    assert len(problems) == 1
    assert "weights" in problems[0]
    assert "risk" in problems[0]


def test_explicit_zero_weight_is_allowed() -> None:
    problems = Config(
        scoring=ScoringConfig(weights=_weights(monetary=0.40, risk=0.0))
    ).validate()
    assert problems == []


@pytest.mark.parametrize(
    ("overrides", "fields"),
    [
        ({"staleness_full_days": 0}, ("staleness_full_days",)),
        (
            {"staleness_full_days": 45},
            ("staleness_full_days", "max_staleness_days"),
        ),
        ({"min_spread_low": 0.0}, ("min_spread_low",)),
        ({"min_spread_high": 1.0}, ("min_spread_medium", "min_spread_high")),
        ({"min_coverage": 0.0}, ("min_coverage",)),
        ({"min_coverage": 0.90}, ("min_coverage", "coverage_demotion")),
        ({"coverage_demotion": 1.5}, ("coverage_demotion",)),
        ({"min_agreement": 0.0}, ("min_agreement",)),
        ({"min_agreement": 1.5}, ("min_agreement",)),
        ({"max_dispersion": 0.0}, ("max_dispersion",)),
        ({"max_cost_ratio": -0.01}, ("max_cost_ratio",)),
        ({"horizon_days": 0}, ("horizon_days",)),
        ({"score_clip": 0.0}, ("score_clip",)),
        (
            {"min_restandardisation_runs": 100},
            ("min_restandardisation_runs", "restandardisation_window_runs"),
        ),
    ],
)
def test_each_threshold_invariant_is_checked(
    overrides: dict[str, Any], fields: tuple[str, ...]
) -> None:
    problems = _scoring_problems(**overrides)
    assert len(problems) == 1, problems
    for name in fields:
        assert name in problems[0]
    for value in overrides.values():
        assert str(value) in problems[0]


def test_boundary_values_are_allowed() -> None:
    """The inclusive ends of each range validate clean."""
    assert _scoring_problems(min_coverage=0.80) == []
    assert _scoring_problems(coverage_demotion=1.0) == []
    assert _scoring_problems(min_agreement=1.0) == []
    assert _scoring_problems(horizon_days=1) == []
    assert _scoring_problems(min_restandardisation_runs=60) == []


def test_digest_is_stable_across_calls(default_config: Config) -> None:
    assert default_config.digest() == default_config.digest()


def test_digest_is_stable_across_equal_configs(default_config: Config) -> None:
    twin = Config(risk=RiskConfig(), scoring=ScoringConfig(), data=DataConfig())
    assert twin.digest() == default_config.digest()


def test_digest_changes_when_a_weight_changes(default_config: Config) -> None:
    reweighted = replace(
        default_config,
        scoring=ScoringConfig(weights=_weights(monetary=0.35, inflation=0.10)),
    )
    assert reweighted.scoring.weights != default_config.scoring.weights
    assert reweighted.digest() != default_config.digest()


def test_digest_ignores_the_account_balance(default_config: Config) -> None:
    """The balance moves after every closed trade, and ``--compare`` reads a
    digest change as a re-weighting and suppresses the deltas. Hashing the
    balance would make every pair of runs incomparable the day after a trade
    settles. The balance is recorded per ticket on ``PositionSize`` and per
    trade in the journal, which is where a per-trade figure belongs."""
    richer = replace(default_config, risk=RiskConfig(account_balance=5000.0))
    assert richer.risk.account_balance != default_config.risk.account_balance
    assert richer.digest() == default_config.digest()


def test_digest_ignores_the_fred_api_key(default_config: Config) -> None:
    """A credential must not leave a fingerprint in a committed report, and a
    key rotation is not a change to the model."""
    keyed = replace(default_config, data=DataConfig(fred_api_key="abc123"))
    assert keyed.digest() == default_config.digest()


@pytest.mark.parametrize("name", ["cache_dir", "manual_dir", "reports_dir"])
def test_digest_ignores_data_directories(default_config: Config, name: str) -> None:
    """The defaults derive from ``REPO_ROOT``, so a digest that saw them would
    differ between machines and in CI for identical weights."""
    moved = replace(
        default_config,
        data=replace(default_config.data, **{name: Path("/somewhere/else")}),
    )
    assert moved.digest() == default_config.digest()


def test_digest_is_independent_of_repo_root(default_config: Config) -> None:
    """Every data directory pointed away from ``REPO_ROOT`` at once, so the
    hash cannot be carrying the checkout path in any of them."""
    elsewhere = Path("/tmp/fbe-elsewhere")
    relocated = replace(
        default_config,
        data=DataConfig(
            cache_dir=elsewhere / "cache",
            manual_dir=elsewhere / "manual",
            reports_dir=elsewhere / "reports",
        ),
    )
    assert not any(
        str(REPO_ROOT) in str(getattr(relocated.data, name))
        for name in ("cache_dir", "manual_dir", "reports_dir")
    )
    assert relocated.digest() == default_config.digest()


def test_digest_ignores_data_config_entirely(default_config: Config) -> None:
    """Nothing in ``DataConfig`` changes a score. Offline, cache lifetime and
    the blackout windows change where numbers come from and when they may be
    acted on, not what they are."""
    altered = replace(
        default_config,
        data=DataConfig(
            offline=True,
            cache_ttl_hours=1,
            calendar_blackout_before_min=5,
            calendar_blackout_after_min=5,
        ),
    )
    assert altered.digest() == default_config.digest()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("min_spread_low", 0.80),
        ("max_staleness_days", 60),
        ("score_clip", 2.5),
        ("min_agreement", 0.70),
        ("horizon_days", 5),
    ],
)
def test_digest_changes_when_a_scoring_field_changes(
    default_config: Config, name: str, value: float
) -> None:
    """Weights are covered by ``test_digest_changes_when_a_weight_changes``;
    this pins the thresholds, which are as much the model as the weights."""
    assert getattr(default_config.scoring, name) != value
    rescored = replace(
        default_config, scoring=replace(default_config.scoring, **{name: value})
    )
    assert rescored.digest() != default_config.digest()


def test_digest_changes_when_risk_per_trade_max_changes(
    default_config: Config,
) -> None:
    """The percentages are the rule and shape every ``PositionSize``, so a
    report sized under a different cap must not look like the same run."""
    capped = replace(default_config, risk=RiskConfig(risk_per_trade_max=0.015))
    assert capped.digest() != default_config.digest()


def test_digest_changes_when_a_derived_risk_limit_changes(
    default_config: Config,
) -> None:
    looser = replace(default_config, risk=RiskConfig(max_concurrent_positions=5))
    assert looser.digest() != default_config.digest()


def test_digest_is_short_and_hex(default_config: Config) -> None:
    digest = default_config.digest()
    assert len(digest) == 12
    assert all(character in "0123456789abcdef" for character in digest)


def test_risk_defaults_match_the_trading_plan(default_config: Config) -> None:
    risk = default_config.risk
    assert risk.account_currency == "ZAR"
    assert risk.account_balance == pytest.approx(2000.0)
    assert risk.risk_per_trade_min == pytest.approx(0.01)
    assert risk.risk_per_trade_max == pytest.approx(0.02)


DERIVED_RISK_LIMITS = (
    "max_concurrent_positions",
    "max_correlated_exposure",
    "max_daily_loss",
    "max_drawdown_pause",
)
"""Limits the plan does not state. Each is derived from the plan's intent in
``docs/risk-and-execution.md`` section 4, and the config must say so, because
``CLAUDE.md`` lets the plan win any argument and a limit wrongly attributed to
the plan cannot be argued with."""


def _field_docstring(source: str, name: str) -> str:
    """The attribute docstring that follows ``name``'s field definition.

    A string literal after a dataclass field is discarded at runtime, so the
    only way to check it is to read the source. The docstring runs from the
    field's line to the next field or the end of the class.
    """
    lines = source.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.lstrip().startswith(f"{name}:")
    )
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith('"""') and not body:
            return ""
        body.append(stripped)
        if stripped.endswith('"""') and (len(body) > 1 or len(stripped) > 3):
            break
    return " ".join(body)


def test_risk_config_does_not_attribute_every_limit_to_the_plan() -> None:
    doc = RiskConfig.__doc__ or ""
    assert "taken directly from the trading plan" not in doc
    assert "from the plan" in doc
    assert "derived" in doc


@pytest.mark.parametrize("name", DERIVED_RISK_LIMITS)
def test_derived_risk_limit_states_its_provenance(name: str) -> None:
    doc = _field_docstring(inspect.getsource(RiskConfig), name)
    assert doc, f"{name} has no docstring"
    assert "risk-and-execution.md" in doc
    assert "section 4" in doc
    assert "prior" in doc


@pytest.mark.parametrize("name", ["max_concurrent_positions", "account_currency"])
def test_previously_undocumented_risk_fields_have_docstrings(name: str) -> None:
    assert _field_docstring(inspect.getsource(RiskConfig), name)


def test_max_dispersion_names_the_quantity_it_is_compared_against() -> None:
    """1.20 was judged against the weighted dispersion, not the plain spread.

    The two are different numbers on the same cross-section, so a docstring
    describing the plain standard deviation across a currency's pillar scores
    invites a replacement threshold reasoned about the wrong quantity. This is
    read at the point the number is set, which is the only place a re-weighting
    looks, so it has to name what ``scoring.dispersion`` returns.
    """
    doc = _field_docstring(inspect.getsource(ScoringConfig), "max_dispersion")
    assert "effective-weighted standard deviation" in doc
    assert "about its composite" in doc
    assert "post-staleness" in doc
    assert "section 4.4" in doc


def test_min_coverage_says_coverage_carries_the_freshness_discount() -> None:
    """Coverage is a sum of effective weights, not a count of usable pillars.

    The staleness discount is the whole reason it is continuous, and a pillar
    on a 30-day-old input contributing half its weight is what makes 0.60 a
    fraction rather than "at least four pillars present".
    """
    doc = _field_docstring(inspect.getsource(ScoringConfig), "min_coverage")
    assert "effective pillar weights" in doc
    assert "staleness discount" in doc
    assert "fresh data" in doc
    assert "section 4.2" in doc


def test_coverage_demotion_names_the_same_quantity_as_min_coverage() -> None:
    """Both thresholds read the same field, so both must define it.

    An unqualified "Coverage" next to the demotion threshold inherits whatever
    the reader assumed from the filter above it, which is the omission this
    pair of docstrings exists to close.
    """
    doc = _field_docstring(inspect.getsource(ScoringConfig), "coverage_demotion")
    assert "min_coverage" in doc
    assert "freshness discount" in doc
    assert "section 4.2" in doc


def test_nothing_in_src_claims_to_be_taken_directly_from_the_plan() -> None:
    hits = [
        path
        for path in (REPO_ROOT / "src").rglob("*.py")
        if "taken directly from the trading plan" in path.read_text()
    ]
    assert hits == []


@pytest.mark.parametrize("name", DERIVED_RISK_LIMITS)
def test_plan_appendix_maps_each_derived_limit(name: str) -> None:
    plan = (REPO_ROOT / "docs" / "trading-plan.md").read_text()
    appendix = plan.split("# Appendix: what the engine automates", 1)[1]
    assert f"{name}`" in appendix


def test_conviction_thresholds_are_ordered(default_config: Config) -> None:
    scoring = default_config.scoring
    assert scoring.min_spread_low < scoring.min_spread_medium
    assert scoring.min_spread_medium < scoring.min_spread_high


def test_data_defaults_are_offline_safe(default_config: Config) -> None:
    data = default_config.data
    assert data.offline is False
    assert data.cache_ttl_hours > 0
    assert data.calendar_blackout_before_min > 0
    assert data.calendar_blackout_after_min > 0


def test_offline_config_fixture_is_offline(offline_config: Config) -> None:
    assert offline_config.data.offline is True


# ---------------------------------------------------------------------------
# load_config: the three layers, and what it refuses
#
# The layering is defaults, then the file, then the environment. Where a test
# proves an override, it uses three distinct values so a pass cannot come from
# the built-in default happening to match what the test wrote.
# ---------------------------------------------------------------------------

DEFAULT_LOW = ScoringConfig().min_spread_low
FILE_LOW = 0.90
ENV_LOW = 1.10


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point the default file lookup at an empty directory and clear the vars.

    Every test in this section runs against a config file under ``tmp_path``,
    never the repo's own, and with no ``FBE_`` variable inherited from the
    machine running the suite. ``FRED_API_KEY`` goes too, since
    ``default_config`` reads it and CI and a developer laptop differ.
    """
    monkeypatch.setattr(fbe.config, "DEFAULT_CONFIG_FILE", tmp_path / "config.yaml")
    for name in list(os.environ):
        if name.startswith("FBE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    yield tmp_path


def _write(directory: Path, body: str, name: str = "config.yaml") -> Path:
    path = directory / name
    path.write_text(body)
    return path


def test_no_file_and_no_environment_gives_the_built_in_defaults(
    clean_env: Path,
) -> None:
    assert load_config() == fbe.config.default_config()
    assert load_config() == Config()


def test_a_file_value_overrides_the_built_in_default(clean_env: Path) -> None:
    path = _write(clean_env, f"scoring:\n  min_spread_low: {FILE_LOW}\n")
    assert DEFAULT_LOW != FILE_LOW
    assert load_config(path).scoring.min_spread_low == FILE_LOW


def test_the_default_lookup_finds_the_file_without_being_given_a_path(
    clean_env: Path,
) -> None:
    _write(clean_env, f"scoring:\n  min_spread_low: {FILE_LOW}\n")
    assert load_config().scoring.min_spread_low == FILE_LOW


def test_a_field_the_file_does_not_mention_keeps_its_default(
    clean_env: Path,
) -> None:
    path = _write(clean_env, f"scoring:\n  min_spread_low: {FILE_LOW}\n")
    scoring = load_config(path).scoring
    assert scoring.min_spread_low == FILE_LOW
    assert scoring.min_spread_medium == ScoringConfig().min_spread_medium
    assert scoring.max_dispersion == ScoringConfig().max_dispersion


def test_an_environment_variable_overrides_the_file(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three distinct values, so this proves the order and not just that one wins."""
    path = _write(clean_env, f"scoring:\n  min_spread_low: {FILE_LOW}\n")
    monkeypatch.setenv("FBE_SCORING_MIN_SPREAD_LOW", str(ENV_LOW))
    assert len({DEFAULT_LOW, FILE_LOW, ENV_LOW}) == 3
    assert load_config(path).scoring.min_spread_low == ENV_LOW


def test_an_environment_variable_overrides_the_default_with_no_file(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FBE_RISK_ACCOUNT_BALANCE", "3125.50")
    assert load_config().risk.account_balance == 3125.50


@pytest.mark.parametrize(
    ("section", "field_name", "raw", "expected"),
    [
        ("scoring", "horizon_days", "14", 14),
        ("scoring", "max_dispersion", "1.45", 1.45),
        ("risk", "account_currency", "USD", "USD"),
        ("data", "cache_ttl_hours", "6", 6),
        ("data", "fred_api_key", "abc123", "abc123"),
    ],
)
def test_environment_values_are_coerced_to_the_field_type(
    clean_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field_name: str,
    raw: str,
    expected: object,
) -> None:
    """An environment variable is a string; the field's annotation decides the type."""
    monkeypatch.setenv(f"FBE_{section.upper()}_{field_name.upper()}", raw)
    value = getattr(getattr(load_config(), section), field_name)
    assert value == expected
    assert type(value) is type(expected)


def test_a_path_field_from_the_environment_becomes_a_path(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FBE_DATA_CACHE_DIR", "/tmp/fbe-elsewhere/cache")
    assert load_config().data.cache_dir == Path("/tmp/fbe-elsewhere/cache")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_a_boolean_from_the_environment_accepts_the_documented_spellings(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("FBE_DATA_OFFLINE", raw)
    assert load_config().data.offline is expected


@pytest.mark.parametrize("raw", ["maybe", "", "2", "t", "offline"])
def test_an_unparseable_boolean_raises_rather_than_guessing(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """``offline`` wrong in the quiet direction sends a test run at the network."""
    monkeypatch.setenv("FBE_DATA_OFFLINE", raw)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "FBE_DATA_OFFLINE" in str(excinfo.value)


@pytest.mark.parametrize(
    ("variable", "raw"),
    [
        ("FBE_SCORING_HORIZON_DAYS", "ten"),
        ("FBE_SCORING_HORIZON_DAYS", "10.5"),
        ("FBE_SCORING_MAX_DISPERSION", "wide"),
        ("FBE_RISK_ACCOUNT_BALANCE", ""),
    ],
)
def test_an_unparseable_number_raises_naming_the_variable(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, variable: str, raw: str
) -> None:
    monkeypatch.setenv(variable, raw)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert variable in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "field_name"),
    [
        ("scoring:\n  horizon_days: true\n", "horizon_days"),
        ("scoring:\n  lookback_years: false\n", "lookback_years"),
        ("risk:\n  account_balance: true\n", "account_balance"),
        ("data:\n  cache_ttl_hours: true\n", "cache_ttl_hours"),
    ],
)
def test_a_boolean_in_a_numeric_field_raises(
    clean_env: Path, body: str, field_name: str
) -> None:
    """``bool`` subclasses ``int``, so an unguarded read turns ``true`` into 1.

    A horizon of one day, or a balance of one unit of account currency, is the
    shape of defect this repository exists to refuse: plausible, silent, and
    wrong by a factor nobody would look for.
    """
    path = _write(clean_env, body)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert field_name in str(excinfo.value)


def test_a_bad_credential_value_is_not_echoed_into_the_error(
    clean_env: Path,
) -> None:
    """An unquoted numeric key is an int to YAML, and the error must not carry it.

    Config errors are printed at a terminal, pasted into issues and captured
    by CI logs. The setting is named; the value is withheld.
    """
    path = _write(clean_env, "data:\n  fred_api_key: 1234567890\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "fred_api_key" in message
    assert "1234567890" not in message
    assert "<redacted>" in message


def test_a_non_secret_value_is_echoed_so_the_error_can_be_acted_on(
    clean_env: Path,
) -> None:
    """Redaction is for credentials only. Everything else names the bad value."""
    path = _write(clean_env, "scoring:\n  horizon_days: ten\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "ten" in str(excinfo.value)


def test_an_unrecognised_key_in_the_file_raises_naming_key_and_path(
    clean_env: Path,
) -> None:
    """A silently ignored key is a setting the operator believes is applied."""
    path = _write(clean_env, "scoring:\n  min_spread_lo: 0.9\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "min_spread_lo" in message
    assert str(path) in message


def test_an_unrecognised_section_in_the_file_raises_naming_key_and_path(
    clean_env: Path,
) -> None:
    path = _write(clean_env, "scorring:\n  min_spread_low: 0.9\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "scorring" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_an_unrecognised_environment_variable_raises_naming_it(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same reasoning as an unknown file key: a typo must not be a no-op."""
    monkeypatch.setenv("FBE_DATA_OFLINE", "true")
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "FBE_DATA_OFLINE" in str(excinfo.value)


def test_a_variable_without_the_prefix_is_left_alone(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prefix is the whole claim of ownership. The rest is somebody else's."""
    monkeypatch.setenv("DATA_OFFLINE", "true")
    monkeypatch.setenv("FBEDATA", "nonsense")
    assert load_config() == Config()


def test_malformed_yaml_raises_an_error_naming_the_path(clean_env: Path) -> None:
    path = _write(clean_env, "scoring:\n  min_spread_low: [0.9\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_a_file_that_is_not_a_mapping_raises(clean_env: Path) -> None:
    path = _write(clean_env, "- min_spread_low\n- 0.9\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_a_section_that_is_not_a_mapping_raises(clean_env: Path) -> None:
    path = _write(clean_env, "scoring: 0.9\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "scoring" in str(excinfo.value)


def test_an_empty_file_is_the_defaults_not_an_error(clean_env: Path) -> None:
    path = _write(clean_env, "")
    assert load_config(path) == Config()


def test_a_path_given_explicitly_must_exist(clean_env: Path) -> None:
    """A missing file the operator named is a typo, not an instruction to ignore it."""
    missing = clean_env / "nowhere.yaml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(missing)
    assert str(missing) in str(excinfo.value)


def test_an_absent_default_file_is_not_an_error(clean_env: Path) -> None:
    assert not (clean_env / "config.yaml").exists()
    assert load_config() == Config()


# ---------------------------------------------------------------------------
# Weights, which are a mapping rather than a scalar
# ---------------------------------------------------------------------------


def test_weights_from_the_file_replace_the_whole_mapping(clean_env: Path) -> None:
    body = "scoring:\n  weights:\n" + "".join(
        f"    {name.value}: {value}\n"
        for name, value in zip(
            PillarName,
            (0.30, 0.20, 0.15, 0.10, 0.10, 0.10, 0.05),
            strict=True,
        )
    )
    path = _write(clean_env, body)
    weights = load_config(path).scoring.weights
    assert weights[PillarName.INFLATION] == 0.20
    assert weights[PillarName.RISK] == 0.05
    assert abs(sum(weights.values()) - 1.0) < WEIGHT_TOLERANCE


def test_a_partial_weights_block_raises_rather_than_merging(clean_env: Path) -> None:
    """Merging one weight over the defaults leaves a total nobody chose."""
    path = _write(clean_env, "scoring:\n  weights:\n    monetary: 0.40\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "inflation" in message
    assert str(path) in message


def test_an_unknown_pillar_in_the_weights_raises(clean_env: Path) -> None:
    body = "scoring:\n  weights:\n" + "".join(
        f"    {name.value}: 0.10\n" for name in PillarName
    )
    path = _write(clean_env, body + "    momentum: 0.10\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "momentum" in str(excinfo.value)


def test_weights_cannot_be_set_from_the_environment(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused loudly. A half-parsed mapping is the worst of the three outcomes."""
    monkeypatch.setenv("FBE_SCORING_WEIGHTS", "monetary=0.4")
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "FBE_SCORING_WEIGHTS" in str(excinfo.value)


# ---------------------------------------------------------------------------
# cli._effective_config: one config per invocation, and --offline one-way
# ---------------------------------------------------------------------------


def _context(options: GlobalOptions) -> typer.Context:
    """A context carrying the global flags, built the way Typer builds one.

    ``meta`` starts empty on a fresh context, which is what makes the caching
    test meaningful: nothing is carried over between two of these.
    """
    context = typer.Context(typer.main.get_command(app))
    context.obj = options
    return context


def test_effective_config_reads_the_file_once_per_invocation(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two commands in one invocation must not produce two digests."""
    calls: list[Path | None] = []
    real = fbe.config.load_config

    def counting(path: Path | None = None) -> Config:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(fbe.config, "load_config", counting)
    context = _context(GlobalOptions())
    first = _effective_config(context)
    second = _effective_config(context)
    assert calls == [None]
    assert first is second


def test_effective_config_passes_the_config_path_through(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(clean_env, f"scoring:\n  min_spread_low: {FILE_LOW}\n", "other.yaml")
    context = _context(GlobalOptions(config_path=path))
    assert _effective_config(context).scoring.min_spread_low == FILE_LOW


def test_offline_flag_forces_offline_over_a_file_that_says_false(
    clean_env: Path,
) -> None:
    path = _write(clean_env, "data:\n  offline: false\n")
    context = _context(GlobalOptions(config_path=path, offline=True))
    assert _effective_config(context).data.offline is True


def test_omitting_the_offline_flag_leaves_the_file_value(clean_env: Path) -> None:
    """``--offline`` is documented as one-way. Its absence is not ``--online``."""
    path = _write(clean_env, "data:\n  offline: true\n")
    context = _context(GlobalOptions(config_path=path, offline=False))
    assert _effective_config(context).data.offline is True


def test_omitting_the_offline_flag_leaves_a_false_file_value_false(
    clean_env: Path,
) -> None:
    path = _write(clean_env, "data:\n  offline: false\n")
    context = _context(GlobalOptions(config_path=path, offline=False))
    assert _effective_config(context).data.offline is False


def test_the_cached_config_carries_the_offline_flag_on_every_read(
    clean_env: Path,
) -> None:
    path = _write(clean_env, "data:\n  offline: false\n")
    context = _context(GlobalOptions(config_path=path, offline=True))
    assert _effective_config(context).data.offline is True
    assert _effective_config(context).data.offline is True


def test_effective_config_resolves_rather_than_refusing_an_invalid_config(
    clean_env: Path,
) -> None:
    """``doctor`` reports validate problems, so the resolver must not pre-empt it."""
    path = _write(clean_env, "risk:\n  risk_per_trade_max: 0.05\n")
    config = _effective_config(_context(GlobalOptions(config_path=path)))
    assert config.risk.risk_per_trade_max == 0.05
    assert config.validate() != []


def test_help_still_works_with_a_broken_default_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool you cannot ask for help is a poor tool to debug a bad file with."""
    broken = _write(tmp_path, "scoring:\n  min_spread_low: [0.9\n")
    monkeypatch.setattr(fbe.config, "DEFAULT_CONFIG_FILE", broken)
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["doctor", "--help"]).exit_code == 0
    assert (
        runner.invoke(app, ["--config", str(broken), "doctor", "--help"]).exit_code == 0
    )
