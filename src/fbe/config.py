"""Effective configuration for a run of the engine.

Config is layered, later layers winning: built-in defaults below, then
``config.yaml`` at the repo root if present, then ``FBE_*`` environment
variables, then explicit CLI flags. Every run records a digest of the
resulting config so a report can be tied back to the weights that made it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fbe.types import PillarName

__all__ = [
    "REPO_ROOT",
    "DATA_DIR",
    "RiskConfig",
    "ScoringConfig",
    "DataConfig",
    "Config",
    "load_config",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
"""Root of the on-disk data tree. Exported because the journal and the cache
both resolve their own paths beneath it rather than each guessing at the
repository layout."""


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Risk limits. Some are the trading plan's own text, the rest are derived.

    Two categories, and each field says which it is in. The distinction
    matters because ``CLAUDE.md`` lets the plan win any conflict: a limit the
    plan states is not up for negotiation here, while a limit derived from the
    plan's intent can be argued with on its merits.

    From the plan: ``account_currency``, ``account_balance``,
    ``risk_per_trade_min`` and ``risk_per_trade_max``. The plan fixes 1-2% of
    balance per trade and quotes R20-R40 as the money equivalent, which
    implies a roughly R2,000 account. Balance is configurable because it
    moves; the percentages are the rule and should not, and
    ``Config.validate()`` refuses a ceiling above 2%.

    Derived from the plan's intent: ``max_concurrent_positions``,
    ``max_correlated_exposure``, ``max_daily_loss`` and ``max_drawdown_pause``.
    The plan names the behaviour each one guards against but gives it no
    number. The numbers are priors, reasoned in ``docs/risk-and-execution.md``
    section 4 and not measured, on the same standing as the pillar weights.
    """

    account_currency: str = "ZAR"
    """Currency the balance and every risk amount are denominated in. From the
    plan, which quotes its per-trade risk as R20-R40. No G10 cross has a ZAR
    leg, so sizing always needs an explicit conversion rate; see
    ``docs/risk-and-execution.md`` section 1."""
    account_balance: float = 2000.0
    risk_per_trade_min: float = 0.01
    risk_per_trade_max: float = 0.02
    max_concurrent_positions: int = 3
    """Derived, not from the plan. Serves "Avoid Overtrading" and "Be
    Selective" (Keep in mind 2, Enhance your focus 8). The plan sets no
    position count. This one is a prior for how many 1h and 4h charts can be
    managed by hand at once, reasoned in ``docs/risk-and-execution.md``
    section 4, and it has not been measured."""
    max_correlated_exposure: float = 0.04
    """Derived, not from the plan. Serves "Trade Size Matters" and the 1-2%
    rule (Keep in mind 10, Trading plan 2): two longs against USD are one
    short-USD bet wearing two tickets, and counting them as two independent
    trades understates the risk. Fraction of ``account_balance`` at risk
    across all positions sharing a leg, with the full risk of a position
    attributed to both of its legs. The plan gives no number. This one is a
    prior from ``docs/risk-and-execution.md`` section 4, not a measured
    result."""
    max_daily_loss: float = 0.04
    """Derived, not from the plan. Serves "Avoid Revenge Trading" (Enhance
    your focus 11, Keep in mind 12), which the plan states as psychology with
    no number. Fraction of ``account_balance`` lost in one day, realised
    losses only, after which the session is over; an open trade underwater
    does not count. This one is a prior from ``docs/risk-and-execution.md``
    section 4, not a measured result."""
    max_drawdown_pause: float = 0.10
    """Derived, not from the plan. Serves "Be Ready for Drawdowns" (Keep in
    mind 16), which the plan states as psychology with no number. Fraction of
    peak equity; falling this far stops trading for a review of the model,
    since a drawdown this deep more likely means the weights are wrong than
    that variance was unkind. This one is a prior from
    ``docs/risk-and-execution.md`` section 4, not a measured result."""


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Pillar weights and the thresholds that turn scores into decisions.

    Weights must sum to 1.0. The defaults lean on monetary policy and
    inflation because rate expectations dominate G10 FX over the multi-day to
    multi-week horizon this engine targets; see ``docs/scoring-spec.md`` for
    the reasoning and for how to re-weight without breaking the band.
    """

    weights: Mapping[PillarName, float] = field(
        default_factory=lambda: {
            PillarName.MONETARY: 0.30,
            PillarName.INFLATION: 0.15,
            PillarName.GROWTH: 0.15,
            PillarName.EMPLOYMENT: 0.10,
            PillarName.EXTERNAL: 0.10,
            PillarName.POSITIONING: 0.10,
            PillarName.RISK: 0.10,
        }
    )
    score_clip: float = 3.0
    """Scores are clipped to +/- this before weighting, so one runaway pillar
    cannot carry a currency on its own."""
    lookback_years: int = 5
    """History used for time-series normalisation where a pillar needs it."""
    restandardisation_window_runs: int = 60
    """How many recent runs the blend divisor is estimated over. A pillar built
    from several sub-indicators blends down to a standard deviation below one,
    so it must be rescaled or it speaks more quietly than its weight says. What
    that rescaling tracks is the correlation between a pillar's own components,
    which moves on a macro-regime timescale, so the window wants to outlast a
    fortnight of odd data without being blind to a real change. Roughly a
    quarter of daily runs."""
    min_restandardisation_runs: int = 20
    """Runs of history required before the rolling divisor engages. Below this
    a run divides by its own cross-sectional standard deviation instead, which
    is recorded on the score because the two are not on the same scale. The
    fallback is deliberately the weaker option: dividing by the current run
    amplifies a pillar exactly when its own components disagree, which is when
    it has earned less influence rather than more."""
    min_spread_low: float = 0.75
    """Score spread below which a pair is called neutral."""
    min_spread_medium: float = 1.50
    min_spread_high: float = 2.50
    min_agreement: float = 0.60
    """Fraction of pillars that must point the same way for anything above
    low conviction."""
    staleness_full_days: int = 15
    """Inputs newer than this carry full weight. Past it the freshness factor
    decays linearly to zero at ``max_staleness_days``, rather than falling off
    a cliff, so a series does not swing a score the day it crosses a boundary."""
    max_staleness_days: int = 45
    max_dispersion: float = 1.20
    """Standard deviation across a currency's pillar scores above which
    conviction is demoted. High dispersion means the pillars disagree, and a
    composite that averages a strong disagreement is not the same evidence as
    a composite built from consensus."""
    min_coverage: float = 0.60
    """Fraction of pillar weight that must have usable data for a pair to be
    tradeable at all. Below this the composite is extrapolated from too little
    to act on."""
    coverage_demotion: float = 0.80
    """Coverage below this demotes conviction without blocking the trade."""
    max_cost_ratio: float = 0.05
    """Ceiling on spread cost as a share of the expected move over the
    horizon. Above it the spread consumes too much of the move to be worth
    trading, whatever the bias says."""
    horizon_days: int = 10
    """The horizon the bias is meant to describe. Fundamental repricing runs
    slower than the 1h and 4h charts the entry is timed on, which is why bias
    is a filter and a size modifier rather than a trigger."""


@dataclass(frozen=True, slots=True)
class DataConfig:
    """Where data comes from and where it is cached."""

    fred_api_key: str | None = None
    cache_dir: Path = DATA_DIR / "cache"
    manual_dir: Path = DATA_DIR / "manual"
    reports_dir: Path = DATA_DIR / "reports"
    cache_ttl_hours: int = 12
    offline: bool = False
    """When true, sources read cache only and never touch the network. Makes
    runs reproducible and keeps tests honest."""
    calendar_blackout_before_min: int = 30
    calendar_blackout_after_min: int = 60
    """The plan says avoid trading around high-impact news. These are the
    default windows either side of a high-impact release."""


@dataclass(frozen=True, slots=True)
class Config:
    """The full effective configuration for one run."""

    risk: RiskConfig = field(default_factory=RiskConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    data: DataConfig = field(default_factory=DataConfig)

    def digest(self) -> str:
        """Stable short hash of the config, recorded on every report."""
        import hashlib
        import json

        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def validate(self) -> list[str]:
        """Return a list of configuration problems; empty means usable."""
        problems: list[str] = []
        total = sum(self.scoring.weights.values())
        if abs(total - 1.0) > 1e-6:
            problems.append(f"pillar weights sum to {total:.4f}, expected 1.0")
        if self.risk.risk_per_trade_max > 0.02:
            problems.append("risk_per_trade_max above 2% contradicts the trading plan")
        if self.risk.risk_per_trade_min > self.risk.risk_per_trade_max:
            problems.append("risk_per_trade_min exceeds risk_per_trade_max")
        return problems


def load_config(path: Path | None = None) -> Config:
    """Build the effective config from defaults, file, and environment.

    Args:
        path: Optional YAML file overriding the defaults. Defaults to
            ``config.yaml`` at the repo root when that file exists.

    Returns:
        The effective `Config`.

    """
    raise NotImplementedError(
        "Config layering is scaffolded but not implemented; see docs/roadmap.md"
    )


def default_config() -> Config:
    """Defaults plus the FRED key from the environment. Safe to call anywhere."""
    return Config(data=DataConfig(fred_api_key=os.environ.get("FRED_API_KEY")))
