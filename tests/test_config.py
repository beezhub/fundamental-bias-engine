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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fbe.config import (
    REPO_ROOT,
    Config,
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


def test_load_config_is_not_implemented_yet() -> None:
    with pytest.raises(NotImplementedError, match="roadmap"):
        load_config()
