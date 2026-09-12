"""Effective configuration for a run of the engine.

Config is layered, later layers winning: built-in defaults below, then
``config.yaml`` at the repo root if present, then ``FBE_*`` environment
variables, then explicit CLI flags. Every run records a digest of the
resulting scoring and risk settings so a report can be tied back to the
weights that made it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

import yaml

from fbe.types import PillarName

__all__ = [
    "REPO_ROOT",
    "DATA_DIR",
    "DEFAULT_CONFIG_FILE",
    "ENV_PREFIX",
    "SECRET_FIELDS",
    "ConfigError",
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

DEFAULT_CONFIG_FILE = REPO_ROOT / "config.yaml"
"""Where ``load_config`` looks when no path is given. Absent is not an error:
the defaults are a working config, and the file is for overriding them."""

ENV_PREFIX = "FBE_"
"""Prefix claiming an environment variable for this engine. A variable that
starts with it and names nothing is a typo and raises; a variable without it
belongs to somebody else and is ignored. ``FRED_API_KEY`` is deliberately
outside this scheme: it is the vendor's own documented name, read by
`default_config`."""


SECRET_FIELDS = frozenset({"fred_api_key"})
"""Fields whose value must not appear in an error message.

An unquoted numeric API key in YAML is read as an int, fails the ``str`` check
and would otherwise be echoed back with the value attached. Errors here are
printed at a terminal, pasted into issues and captured by CI logs, so the
value is replaced by a marker and only the setting is named.
"""


class ConfigError(ValueError):
    """A config file or environment override that cannot be applied.

    Its own type, so a caller cannot swallow it with a stray ``except
    ValueError`` around a float conversion. Every message names the offending
    key or variable and, for a file, the path it came from, because the
    operator fixing it is looking at a file rather than at this module.
    """


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
    """Each pillar's share of the composite. Must sum to 1.0.

    A weight here is a pillar's share of the blend. It is not the model's total
    loading on any one underlying series, and the two come apart wherever a
    series feeds more than one pillar. The live instance is headline CPI:
    INFLATION at 0.15 loads on it positively, and MONETARY at 0.30 loads on it
    negatively through ``real_policy_rate``, which is ``policy_rate - cpi_yoy``.
    The two partly cancel, so the model's response to an inflation print is
    smaller than 0.15 suggests, by roughly a fifth to a half depending on
    whether core moved with headline. ``docs/scoring-spec.md`` section 3.2
    publishes the figures, ``scoring.series_loading`` computes them, and ADR
    0003 records why the opposing term was kept.
    """
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
    """Dispersion above which conviction is demoted one step, in
    ``bias.conviction_for``. Compared against ``CurrencyScore.dispersion``,
    which is the effective-weighted standard deviation of a currency's pillar
    scores about its composite, and not the unweighted standard deviation
    across those scores. The weights are the post-staleness effective weights,
    renormalised to sum to 1.0 so the measure stays on the score band whatever
    the coverage was. ``docs/scoring-spec.md`` section 4.4 defines it and
    ``scoring.dispersion`` computes it. High dispersion means the pillars
    disagree, and a composite that averages a strong disagreement is not the
    same evidence as a composite built from consensus. 1.20 was judged against
    that weighted quantity, where it means the pillars sit typically more than
    a full band unit from the composite, so a replacement reasoned about as
    the plain spread of seven pillar scores will not mean here what it
    appears to mean."""
    min_coverage: float = 0.60
    """Coverage below which a pair is untradeable whatever its spread says,
    blocked with ``coverage`` in ``bias.apply_filters`` on whichever leg is
    thinner. Compared against
    ``CurrencyScore.coverage``, the sum of the effective pillar weights,
    ``sum over p of w_eff(p)``, defined in ``docs/scoring-spec.md`` section
    4.2 and computed by ``scoring.coverage``. Effective weight carries the
    staleness discount, so this is the fraction of pillar weight that had
    usable and fresh data rather than the fraction of pillars present: a
    pillar scoring on a 30-day-old input contributes half its weight, not all
    of it. That is why coverage is continuous and why the threshold is a
    fraction rather than a count. Below it the composite is extrapolated from
    too little to act on."""
    coverage_demotion: float = 0.80
    """Coverage below this demotes conviction one step without blocking the
    trade. The same quantity as ``min_coverage``, the sum of effective pillar
    weights from ``docs/scoring-spec.md`` section 4.2, so it carries the same
    freshness discount. The view still stands here, it just rests on partial
    data."""
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
        """Short hash of the settings that change what a run computes.

        Recorded on every report and journal entry so an old call can be tied
        to the weights and limits that produced it, and read by ``--compare``,
        which treats a digest change as a re-weighting and reports the deltas
        between two runs as not comparable.

        Covered: the whole of ``ScoringConfig``, and ``RiskConfig`` except
        ``account_balance``. The percentages and position limits are the rule
        and shape every ``PositionSize``, so a report sized under a different
        cap must not look like the same run.

        Not covered: ``account_balance``, which moves after every closed trade
        and would make each day incomparable to the last; it is recorded per
        ticket on ``PositionSize`` and per trade in the journal instead.
        ``DataConfig`` is excluded entirely. Nothing in it changes a score:
        the cache lifetime, the offline flag and the blackout windows change
        where numbers come from and when they may be acted on, not what they
        are. It also holds the FRED key and three paths derived from
        ``REPO_ROOT``, and a hash written into committed reports must not
        move with a secret or with the machine it ran on.

        Returns:
            The first twelve hex characters of a SHA-256 over the covered
            fields, serialised with sorted keys so field order cannot move it.

        """
        risk = asdict(self.risk)
        del risk["account_balance"]
        payload = json.dumps(
            {"risk": risk, "scoring": asdict(self.scoring)}, sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def validate(self) -> list[str]:
        """Return a list of configuration problems; empty means usable.

        Every check here guards a downstream formula that would otherwise
        keep producing a plausible number. The scoring thresholds are read as
        ordered bands by the staleness ramp, the conviction ladder and the
        coverage filter, and none of those formulas can tell an inverted band
        from a valid one: a ramp whose start is past its end silently gives a
        pillar full weight or none, an empty LOW band promotes every marginal
        pair to MEDIUM and to 1.5% of balance, and a demotion floor below the
        hard filter is dead code. This is where such a config fails, at load,
        instead of in a report that looks normal. The defaults pass every
        check, and a change to a default that stops them passing is a defect
        in the change.

        Every message names the fields involved and their values, so it can
        be acted on without opening this file. Problems accumulate rather than
        short-circuiting, because a config wrong in three places should say so
        once.
        """
        problems: list[str] = []
        problems.extend(self._weight_problems())
        problems.extend(self._threshold_problems())
        problems.extend(self._risk_problems())
        return problems

    def _weight_problems(self) -> list[str]:
        """Check that the weights cover every pillar, are non-negative and sum to 1.0.

        A pillar missing from the map is switched off by `BasePillar.weight`,
        which returns 0.0 for it. That is rejected here all the same, so that a
        pillar is switched off by writing 0.0 where a reader will see it rather
        than by an omission they have to notice. A negative weight is rejected
        because it inverts the sign convention for that pillar while the sum
        can still come to 1.0.
        """
        problems: list[str] = []
        weights = self.scoring.weights
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            problems.append(f"pillar weights sum to {total:.4f}, expected 1.0")
        for name in PillarName:
            if name not in weights:
                problems.append(
                    f"weights has no entry for {name}: set it to 0.0 to "
                    "switch the pillar off"
                )
        for name, weight in weights.items():
            if weight < 0.0:
                problems.append(
                    f"weights[{name}] is {weight}, expected at or above 0.0"
                )
        return problems

    def _threshold_problems(self) -> list[str]:
        """Check that the scoring thresholds form the bands the formulas assume.

        The orderings, each one read by a named consumer:

            ``0 < staleness_full_days < max_staleness_days``, the ramp in
            ``scoring.apply_staleness_penalty``.

            ``0 < min_spread_low < min_spread_medium < min_spread_high``, the
            tiers in ``bias.conviction_for``.

            ``0 < min_coverage <= coverage_demotion <= 1.0``, the hard filter
            in ``bias.apply_filters`` and the demotion above it.

            ``0 < min_agreement <= 1.0``, a fraction of pillar weight.

            ``max_dispersion``, ``max_cost_ratio`` and ``score_clip`` above 0
            and ``horizon_days`` at least 1, since each divides or bounds a
            quantity that a zero would zero out.

            ``min_restandardisation_runs <= restandardisation_window_runs``,
            or the rolling divisor could never engage.
        """
        problems: list[str] = []
        s = self.scoring
        if s.staleness_full_days <= 0:
            problems.append(
                f"staleness_full_days is {s.staleness_full_days}, expected above 0"
            )
        if s.staleness_full_days >= s.max_staleness_days:
            problems.append(
                f"staleness_full_days {s.staleness_full_days} must be below "
                f"max_staleness_days {s.max_staleness_days}"
            )
        if s.min_spread_low <= 0.0:
            problems.append(f"min_spread_low is {s.min_spread_low}, expected above 0")
        if s.min_spread_low >= s.min_spread_medium:
            problems.append(
                f"min_spread_low {s.min_spread_low} must be below "
                f"min_spread_medium {s.min_spread_medium}"
            )
        if s.min_spread_medium >= s.min_spread_high:
            problems.append(
                f"min_spread_medium {s.min_spread_medium} must be below "
                f"min_spread_high {s.min_spread_high}"
            )
        if s.min_coverage <= 0.0:
            problems.append(f"min_coverage is {s.min_coverage}, expected above 0")
        if s.min_coverage > s.coverage_demotion:
            problems.append(
                f"min_coverage {s.min_coverage} must be at or below "
                f"coverage_demotion {s.coverage_demotion}"
            )
        if s.coverage_demotion > 1.0:
            problems.append(
                f"coverage_demotion is {s.coverage_demotion}, expected at or below 1.0"
            )
        if s.min_agreement <= 0.0 or s.min_agreement > 1.0:
            problems.append(
                f"min_agreement is {s.min_agreement}, expected above 0 and at or "
                "below 1.0"
            )
        for name, value in (
            ("max_dispersion", s.max_dispersion),
            ("max_cost_ratio", s.max_cost_ratio),
            ("score_clip", s.score_clip),
        ):
            if value <= 0.0:
                problems.append(f"{name} is {value}, expected above 0")
        if s.horizon_days < 1:
            problems.append(f"horizon_days is {s.horizon_days}, expected at least 1")
        if s.min_restandardisation_runs > s.restandardisation_window_runs:
            problems.append(
                f"min_restandardisation_runs {s.min_restandardisation_runs} must be "
                "at or below restandardisation_window_runs "
                f"{s.restandardisation_window_runs}"
            )
        return problems

    def _risk_problems(self) -> list[str]:
        """Check that the per-trade band sits inside the plan's 1-2% and is ordered."""
        problems: list[str] = []
        if self.risk.risk_per_trade_max > 0.02:
            problems.append("risk_per_trade_max above 2% contradicts the trading plan")
        if self.risk.risk_per_trade_min > self.risk.risk_per_trade_max:
            problems.append("risk_per_trade_min exceeds risk_per_trade_max")
        return problems


_BOOLEAN_WORDS: Mapping[str, bool] = {
    "true": True,
    "yes": True,
    "on": True,
    "1": True,
    "false": False,
    "no": False,
    "off": False,
    "0": False,
}
"""Accepted spellings of a boolean from a file or the environment.

Fixed and small on purpose. Anything else raises, because the field this
mostly guards is ``DataConfig.offline``, and reading an unrecognised word as
False sends a run that was meant to be cache-only at the network.
"""


def _section_types() -> Mapping[str, type]:
    """Section name to its dataclass, read off `Config` rather than listed here.

    Derived so that adding a fourth section to `Config` is picked up by the
    file reader, the environment reader and their error messages at once. A
    hand-maintained list is the config-drift defect in another costume.
    """
    hints = get_type_hints(Config)
    return {entry.name: hints[entry.name] for entry in fields(Config)}


def _is_mapping_field(hint: object) -> bool:
    """Say whether a field is a mapping rather than a scalar, as ``weights`` is."""
    return get_origin(hint) in (Mapping, dict)


def _shown(value: object, secret: bool) -> str:
    """Render a value for an error message, withholding it if it is a secret."""
    return "<redacted>" if secret else repr(value)


def _coerce(value: object, hint: object, where: str, secret: bool = False) -> Any:
    """Convert one raw value to the type its field is annotated with.

    Handles both sources: YAML gives typed values already, the environment
    gives strings only. Both go through here so that ``offline: yes`` in a
    file and ``FBE_DATA_OFFLINE=yes`` cannot mean different things.

    Args:
        value: The value as read, a string from the environment or whatever
            YAML produced.
        hint: The field's resolved annotation.
        where: How to name this setting in an error, such as a variable name
            or ``<path>: scoring.horizon_days``.
        secret: When true, the value is withheld from any error raised here.

    Returns:
        The value as the field's type. Typed ``Any`` because the type returned
        is decided by ``hint`` at runtime; the caller assigns it into a typed
        field, which is where it is checked.

    Raises:
        ConfigError: When the value cannot be read as that type. Nothing here
            falls back to a default: a setting the operator wrote and this
            module could not read is the quiet-wrong-number failure mode, and
            it stops the run instead.

    """
    if isinstance(hint, UnionType):
        inner = [arg for arg in get_args(hint) if arg is not type(None)]
        if value is None:
            return None
        if len(inner) == 1:
            return _coerce(value, inner[0], where, secret)
        raise ConfigError(f"{where}: no rule for reading {hint}")
    if hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _BOOLEAN_WORDS:
            return _BOOLEAN_WORDS[value.strip().lower()]
        raise ConfigError(
            f"{where}: expected a boolean, got {_shown(value, secret)}. "
            "Write one of "
            f"{', '.join(sorted(_BOOLEAN_WORDS))}."
        )
    if hint is int:
        # bool is a subclass of int, so this order matters: without the guard
        # above, ``horizon_days: true`` would quietly become 1.
        if isinstance(value, bool):
            raise ConfigError(
                f"{where}: expected a whole number, got {_shown(value, secret)}"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: expected a whole number, got {_shown(value, secret)}"
                ) from exc
        raise ConfigError(
            f"{where}: expected a whole number, got {_shown(value, secret)}"
        )
    if hint is float:
        if isinstance(value, bool):
            raise ConfigError(
                f"{where}: expected a number, got {_shown(value, secret)}"
            )
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: expected a number, got {_shown(value, secret)}"
                ) from exc
        raise ConfigError(f"{where}: expected a number, got {_shown(value, secret)}")
    if hint is Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
        raise ConfigError(f"{where}: expected a path, got {_shown(value, secret)}")
    if hint is str:
        if isinstance(value, str):
            return value
        raise ConfigError(f"{where}: expected text, got {_shown(value, secret)}")
    raise ConfigError(f"{where}: no rule for reading {hint}")


def _coerce_weights(value: object, where: str) -> Mapping[PillarName, float]:
    """Read a complete pillar weight mapping from a config file.

    Every pillar must be present. A block naming one pillar could be merged
    over the defaults, but the result would be a set of weights summing to
    something nobody chose, and the composite is on an unstated scale the
    moment that happens. Refusing is the loud option.

    Args:
        value: The mapping as YAML produced it.
        where: How to name this setting in an error.

    Returns:
        One weight per pillar. Weights are fractions of 1.0, not percentages;
        `Config.validate` is what checks they sum to 1.0.

    Raises:
        ConfigError: When the value is not a mapping, names a pillar that does
            not exist, or omits one that does.

    """
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping of pillar to weight")
    known = {pillar.value: pillar for pillar in PillarName}
    parsed: dict[PillarName, float] = {}
    for key, weight in value.items():
        if key not in known:
            raise ConfigError(
                f"{where}: unknown pillar {key!r}; expected one of "
                f"{', '.join(sorted(known))}"
            )
        parsed[known[key]] = _coerce(weight, float, f"{where}.{key}")
    missing = [name for name in known if known[name] not in parsed]
    if missing:
        raise ConfigError(
            f"{where}: every pillar must be listed; missing "
            f"{', '.join(sorted(missing))}. A block naming some of them would "
            "be merged over the defaults and leave a total nobody chose."
        )
    return parsed


def _read_yaml(path: Path) -> Mapping[str, Any]:
    """Parse the config file, or raise naming the path.

    Returns:
        The mapping the file holds. An empty file is an empty mapping, which
        is the defaults, and not an error: a file someone emptied while
        debugging should behave as no file at all.

    Raises:
        ConfigError: When the file cannot be read, is not valid YAML, or holds
            something other than a mapping at the top level.

    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(
            f"{path}: expected a mapping of sections at the top level, got "
            f"{type(loaded).__name__}"
        )
    return loaded


def _file_overrides(path: Path) -> Mapping[str, Mapping[str, object]]:
    """Read one section of overrides per `Config` section from a YAML file.

    Raises:
        ConfigError: On an unknown section, an unknown key within a section, a
            section that is not a mapping, or a value of the wrong type. An
            unknown key is refused rather than ignored because a key that does
            nothing is a setting the operator believes is applied.

    """
    sections = _section_types()
    raw = _read_yaml(path)
    collected: dict[str, dict[str, object]] = {name: {} for name in sections}
    for section_name, body in raw.items():
        if section_name not in sections:
            raise ConfigError(
                f"{path}: unknown section {section_name!r}; expected one of "
                f"{', '.join(sorted(sections))}"
            )
        if not isinstance(body, dict):
            raise ConfigError(
                f"{path}: section {section_name!r} must be a mapping of settings, "
                f"got {type(body).__name__}"
            )
        hints = get_type_hints(sections[section_name])
        for key, value in body.items():
            if key not in hints:
                raise ConfigError(
                    f"{path}: unknown key {key!r} in section {section_name!r}; "
                    f"expected one of {', '.join(sorted(hints))}"
                )
            where = f"{path}: {section_name}.{key}"
            hint = hints[key]
            collected[section_name][key] = (
                _coerce_weights(value, where)
                if _is_mapping_field(hint)
                else _coerce(value, hint, where, key in SECRET_FIELDS)
            )
    return collected


def _environment_overrides(
    environ: Mapping[str, str],
) -> Mapping[str, Mapping[str, object]]:
    """Read overrides from ``FBE_<SECTION>_<FIELD>`` variables.

    The names are derived from the section dataclasses, so there is no table
    to keep in step and no variable that parses two ways. That holds while no
    section name contains an underscore: ``FBE_A_B_C`` would be ambiguous
    between a section ``a_b`` and a field ``b_c``, and a section added with
    one in its name needs an explicit mapping rather than this derivation.

    Raises:
        ConfigError: On a variable carrying the prefix that names no setting,
            or a value of the wrong type. A typo in a variable name has to
            raise for the same reason an unknown file key does.

    """
    sections = _section_types()
    index: dict[str, tuple[str, str, object]] = {}
    for section_name, section_type in sections.items():
        for field_name, hint in get_type_hints(section_type).items():
            variable = f"{ENV_PREFIX}{section_name.upper()}_{field_name.upper()}"
            index[variable] = (section_name, field_name, hint)
    collected: dict[str, dict[str, object]] = {name: {} for name in sections}
    for variable, raw in environ.items():
        if not variable.startswith(ENV_PREFIX):
            continue
        if variable not in index:
            raise ConfigError(
                f"{variable} is not a setting. Expected "
                f"{ENV_PREFIX}<SECTION>_<FIELD>, where SECTION is one of "
                f"{', '.join(sorted(sections))} and FIELD is a field of it."
            )
        section_name, field_name, hint = index[variable]
        if _is_mapping_field(hint):
            raise ConfigError(
                f"{variable}: {section_name}.{field_name} is a mapping and can "
                "only be set in the config file. A half-parsed set of weights "
                "is worse than no override."
            )
        collected[section_name][field_name] = _coerce(
            raw, hint, variable, field_name in SECRET_FIELDS
        )
    return collected


def load_config(path: Path | None = None) -> Config:
    """Build the effective config from defaults, file, and environment.

    The layers apply in that order, each overriding the one before it field by
    field, so a file that sets one threshold leaves the rest at their defaults.

    Environment variables are named ``FBE_<SECTION>_<FIELD>`` in upper case,
    for example ``FBE_DATA_OFFLINE`` or ``FBE_SCORING_MIN_SPREAD_LOW``. A
    variable carrying the prefix that names no field raises, as does an
    unknown key in the file: a setting that silently does nothing is a setting
    the operator believes is applied. ``FRED_API_KEY`` keeps its own name,
    read by `default_config`, because that is the name the vendor documents.

    This resolves and does not judge. `Config.validate` is what reports an
    unusable combination, and ``fbe doctor`` is what prints it, so a config
    wrong enough to need fixing can still be loaded and looked at.

    Args:
        path: Optional YAML file overriding the defaults. Defaults to
            ``config.yaml`` at the repo root when that file exists. A path
            given explicitly must exist; the default one need not.

    Returns:
        The effective `Config`.

    Raises:
        ConfigError: When a named file is missing, unreadable or malformed,
            when a key or variable names nothing, or when a value cannot be
            read as its field's type. Every message names the setting, and for
            a file the path as well.

    """
    base = default_config()
    if path is not None and not path.exists():
        raise ConfigError(f"{path}: config file not found")
    file_path = path if path is not None else DEFAULT_CONFIG_FILE
    overrides: dict[str, dict[str, object]] = {name: {} for name in _section_types()}
    if file_path.exists():
        for section_name, values in _file_overrides(file_path).items():
            overrides[section_name].update(values)
    for section_name, values in _environment_overrides(os.environ).items():
        overrides[section_name].update(values)
    resolved = {
        section_name: replace(getattr(base, section_name), **values)
        if values
        else getattr(base, section_name)
        for section_name, values in overrides.items()
    }
    return replace(base, **resolved)


def default_config() -> Config:
    """Defaults plus the FRED key from the environment. Safe to call anywhere."""
    return Config(data=DataConfig(fred_api_key=os.environ.get("FRED_API_KEY")))
