"""Shared fixtures for the test suite.

Everything here is built from the three modules that are actually implemented:
``fbe.types``, ``fbe.universe`` and ``fbe.config``. Nothing in this file
imports a module that is still in progress, so the suite stays green while the
rest of the engine lands.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime

import pytest

from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.types import Frequency, Observation, PillarName
from fbe.universe import G10

ASOF = date(2026, 6, 30)
"""Fixed as-of date. Tests must never depend on today, or they rot."""

RELEASED_AT = datetime(2026, 6, 30, 12, 30, tzinfo=UTC)


@pytest.fixture
def asof() -> date:
    return ASOF


@pytest.fixture
def default_config() -> Config:
    """A Config built from the dataclass defaults, with no environment read.

    Deliberately not ``fbe.config.default_config()``: that one picks up
    ``FRED_API_KEY`` from the environment, which would make results differ
    between a developer machine and CI.
    """
    return Config(risk=RiskConfig(), scoring=ScoringConfig(), data=DataConfig())


@pytest.fixture
def offline_config(default_config: Config) -> Config:
    """The default config forced to cache-only, for any source-facing test."""
    from dataclasses import replace

    return replace(default_config, data=replace(default_config.data, offline=True))


# Plausible mid-2026 values, one per currency. These exist to give the pillar
# tests something shaped like real data to chew on. They are illustrative
# inputs, not a claim about any actual print.
_POLICY_RATE: Mapping[str, float] = {
    "USD": 3.75,
    "EUR": 2.00,
    "GBP": 3.75,
    "JPY": 0.75,
    "CHF": 0.00,
    "CAD": 2.25,
    "AUD": 3.60,
    "NZD": 2.50,
}

_YIELD_2Y: Mapping[str, float] = {
    "USD": 3.55,
    "EUR": 1.95,
    "GBP": 3.80,
    "JPY": 0.90,
    "CHF": 0.15,
    "CAD": 2.60,
    "AUD": 3.40,
    "NZD": 2.95,
}

_CPI_YOY: Mapping[str, float] = {
    "USD": 2.90,
    "EUR": 2.10,
    "GBP": 3.40,
    "JPY": 2.60,
    "CHF": 0.30,
    "CAD": 2.20,
    "AUD": 2.80,
    "NZD": 2.40,
}

_UNEMPLOYMENT: Mapping[str, float] = {
    "USD": 4.40,
    "EUR": 6.30,
    "GBP": 4.90,
    "JPY": 2.50,
    "CHF": 2.90,
    "CAD": 6.80,
    "AUD": 4.30,
    "NZD": 5.20,
}


def _observation(
    indicator: str,
    currency: str,
    value: float,
    unit: str = "percent",
    frequency: Frequency = Frequency.MONTHLY,
    series_id: str | None = None,
) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=ASOF,
        source="fixture",
        series_id=series_id or f"{indicator.upper()}_{currency}",
        unit=unit,
        frequency=frequency,
        released_at=RELEASED_AT,
    )


@pytest.fixture
def sample_observations() -> tuple[Observation, ...]:
    """One observation per indicator per G10 currency, plus one global series.

    Four indicators across all eight currencies, so a cross-sectional pillar
    has a full universe to normalise against, and a ``GLOBAL`` VIX print for
    the risk pillar. Every value carries the fixed ``ASOF`` period, so nothing
    in the set is stale relative to anything else.
    """
    observations: list[Observation] = []
    for currency in G10:
        observations.append(
            _observation(
                "policy_rate",
                currency,
                _POLICY_RATE[currency],
                frequency=Frequency.IRREGULAR,
            )
        )
        observations.append(
            _observation(
                "yield_2y",
                currency,
                _YIELD_2Y[currency],
                frequency=Frequency.DAILY,
            )
        )
        observations.append(_observation("cpi_yoy", currency, _CPI_YOY[currency]))
        observations.append(
            _observation("unemployment_rate", currency, _UNEMPLOYMENT[currency])
        )
    observations.append(
        _observation(
            "vix",
            "GLOBAL",
            17.4,
            unit="index",
            frequency=Frequency.DAILY,
            series_id="VIXCLS",
        )
    )
    return tuple(observations)


@pytest.fixture
def observations_by_currency(
    sample_observations: Sequence[Observation],
) -> Mapping[str, tuple[Observation, ...]]:
    """``sample_observations`` grouped by currency code, GLOBAL included."""
    grouped: dict[str, list[Observation]] = {}
    for observation in sample_observations:
        grouped.setdefault(observation.currency, []).append(observation)
    return {code: tuple(items) for code, items in grouped.items()}


@pytest.fixture
def pillar_names() -> tuple[PillarName, ...]:
    return tuple(PillarName)


@pytest.fixture
def no_fred_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove FRED_API_KEY so a test cannot accidentally reach the network."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    yield
