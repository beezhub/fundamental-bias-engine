"""Tests for the run configuration.

Two things matter here. The pillar weights must sum to 1.0, or the composite
score is on an unstated scale and the conviction thresholds mean nothing. And
``digest()`` must be stable, because a report that cannot be tied back to the
weights that produced it cannot be evaluated later.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig, load_config
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


def test_digest_changes_when_risk_changes(default_config: Config) -> None:
    richer = replace(default_config, risk=RiskConfig(account_balance=5000.0))
    assert richer.digest() != default_config.digest()


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
