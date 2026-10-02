"""The two-year yield changes ADR 0004 settled and nothing built: #322.

``yield_2y_chg_1m`` and ``yield_2y_chg_3m`` carry 0.45 of MONETARY between
them, which is 0.135 of every composite. No source publishes a pre-differenced
yield change, so they are derived once, in the collector, from the ``yield_2y``
sessions every source already returns. Every number below is worked by hand
from the series it is computed from, so a test that passes is a window that
matches ADR 0004 Decision 1 rather than one that matches the code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fbe.config import DataConfig
from fbe.datasources import collect as collect_module
from fbe.datasources import registry
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.datasources.yield_changes import derive_yield_changes, months_back
from fbe.types import Frequency, Observation

CHG_1M = "yield_2y_chg_1m"
CHG_3M = "yield_2y_chg_3m"
TOLERANCE = registry.YIELD_CHANGE_TOLERANCE_DAYS


def level(
    day: date,
    value: float,
    currency: str = "USD",
    released_at: datetime | None = None,
) -> Observation:
    """One session of the two-year yield, in percent."""
    return Observation(
        indicator="yield_2y",
        currency=currency,
        value=value,
        period=day,
        source="fred",
        series_id="DGS2",
        unit="percent",
        frequency=Frequency.DAILY,
        released_at=released_at,
    )


def by_key(
    derived: Iterable[Observation],
) -> Mapping[tuple[str, str, date], Observation]:
    return {(row.indicator, row.currency, row.period): row for row in derived}


# --- the calendar arithmetic -------------------------------------------------


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 9, 30), 1, date(2026, 8, 30)),
        (date(2026, 9, 30), 3, date(2026, 6, 30)),
        (date(2026, 5, 31), 3, date(2026, 2, 28)),  # no 31 February
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2024, 3, 30), 1, date(2024, 2, 29)),  # leap year
        (date(2026, 1, 15), 3, date(2025, 10, 15)),  # across a year end
    ],
)
def test_the_target_is_the_same_day_back_or_the_months_last_day(
    day: date, months: int, expected: date
) -> None:
    assert months_back(day, months) == expected


# --- the window ---------------------------------------------------------------


def test_a_hand_worked_three_month_change_in_basis_points() -> None:
    """3.50% today against 3.90% on the target day is -40 basis points."""
    derived = by_key(
        derive_yield_changes(
            [level(date(2026, 6, 30), 3.90), level(date(2026, 9, 30), 3.50)]
        )
    )

    row = derived[(CHG_3M, "USD", date(2026, 9, 30))]
    assert row.value == pytest.approx(-40.0)
    assert row.unit == "basis_points"


def test_the_earlier_endpoint_is_the_last_session_on_or_before_the_target() -> None:
    """30 August 2026 is a Sunday, so the earlier endpoint is Friday the 28th.

    The Thursday before it must not be used, and neither must the Monday after:
    on or before, and the latest such session.
    """
    derived = by_key(
        derive_yield_changes(
            [
                level(date(2026, 8, 27), 9.99),
                level(date(2026, 8, 28), 3.70),
                level(date(2026, 8, 31), 8.88),
                level(date(2026, 9, 30), 3.50),
            ]
        )
    )

    assert derived[(CHG_1M, "USD", date(2026, 9, 30))].value == pytest.approx(-20.0)


def test_an_earlier_endpoint_at_the_tolerance_is_used() -> None:
    target = date(2026, 6, 30)
    derived = by_key(
        derive_yield_changes(
            [
                level(target - timedelta(days=TOLERANCE), 4.00),
                level(date(2026, 9, 30), 3.50),
            ]
        )
    )

    assert (CHG_3M, "USD", date(2026, 9, 30)) in derived


def test_an_earlier_endpoint_past_the_tolerance_emits_nothing() -> None:
    """A window longer than the key names is a wrong number with the right label."""
    target = date(2026, 6, 30)
    derived = by_key(
        derive_yield_changes(
            [
                level(target - timedelta(days=TOLERANCE + 1), 4.00),
                level(date(2026, 9, 30), 3.50),
            ]
        )
    )

    assert (CHG_3M, "USD", date(2026, 9, 30)) not in derived


def test_no_session_before_the_target_emits_nothing() -> None:
    """A month of history cannot give a three-month change, and must not try."""
    derived = by_key(
        derive_yield_changes(
            [level(date(2026, 9, 1), 3.60), level(date(2026, 9, 30), 3.50)]
        )
    )

    assert (CHG_3M, "USD", date(2026, 9, 30)) not in derived
    assert (CHG_1M, "USD", date(2026, 9, 30)) not in derived


def test_the_change_is_stamped_and_released_with_its_later_session() -> None:
    """The period is what `BasePillar.staleness_days` ages, so it is the later
    session; the release is the later one too, because the change cannot be
    known before both ends of it are."""
    released = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
    derived = by_key(
        derive_yield_changes(
            [
                level(date(2026, 8, 28), 3.70),
                level(date(2026, 9, 30), 3.50, released_at=released),
            ]
        )
    )

    row = derived[(CHG_1M, "USD", date(2026, 9, 30))]
    assert row.period == date(2026, 9, 30)
    assert row.released_at == released
    assert row.source == "fred"
    assert row.series_id == "DGS2"
    assert row.frequency is Frequency.DAILY


def test_currencies_are_never_mixed() -> None:
    derived = by_key(
        derive_yield_changes(
            [
                level(date(2026, 8, 31), 3.70, currency="USD"),
                level(date(2026, 9, 30), 0.50, currency="JPY"),
            ]
        )
    )

    assert derived == {}


def test_only_the_two_year_level_is_differenced() -> None:
    other = Observation(
        indicator="yield_10y",
        currency="USD",
        value=4.0,
        period=date(2026, 8, 31),
        source="fred",
        series_id="DGS10",
        unit="percent",
        frequency=Frequency.DAILY,
    )

    assert derive_yield_changes([other, level(date(2026, 9, 30), 3.5)]) == ()


# --- through the collector ----------------------------------------------------


def _usd_source(observations: Sequence[Observation]) -> type[BaseDataSource]:
    """A stand-in carrying the name the registry routes USD ``yield_2y`` to."""
    routed = registry.series_for("yield_2y", "USD")
    assert routed is not None

    class _Built(BaseDataSource):
        rate_limit = RateLimit(requests=1000, per_seconds=60.0)
        retry = RetryPolicy(attempts=1)

        def available(self) -> bool:
            return True

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            return [row for row in observations if row.indicator in set(indicators)]

        def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
            return {("yield_2y", "USD"): routed}

    _Built.name = routed.source
    _Built.base_url = ""
    return _Built


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(
        cache_dir=tmp_path / "cache",
        manual_dir=tmp_path / "manual",
        reports_dir=tmp_path / "reports",
    )


def test_asking_for_a_change_alone_fetches_the_level_behind_it(
    data_config: DataConfig,
) -> None:
    """A caller asking only for the change still gets it, and only it."""
    source = _usd_source([level(date(2026, 8, 28), 3.7), level(date(2026, 9, 30), 3.5)])

    result = collect_module.collect(
        data_config,
        start=date(2026, 1, 1),
        end=date(2026, 9, 30),
        sources=(source,),
        indicators=[CHG_1M],
        currencies=["USD"],
    )

    assert {(row.indicator, row.period) for row in result.observations} == {
        (CHG_1M, date(2026, 9, 30))
    }
    assert CHG_1M not in result.gaps


def test_a_run_holding_the_level_reports_no_change_gap(
    data_config: DataConfig,
) -> None:
    """The gap list read both keys as missing for all eight before this."""
    source = _usd_source(
        [
            level(date(2026, 6, 30), 3.9),
            level(date(2026, 8, 28), 3.7),
            level(date(2026, 9, 30), 3.5),
        ]
    )

    result = collect_module.collect(
        data_config,
        start=date(2026, 1, 1),
        end=date(2026, 9, 30),
        sources=(source,),
        indicators=["yield_2y", CHG_1M, CHG_3M],
        currencies=["USD"],
    )

    assert CHG_1M not in result.gaps
    assert CHG_3M not in result.gaps
    held = {(row.indicator, row.period): row.value for row in result.observations}
    assert held[(CHG_3M, date(2026, 9, 30))] == pytest.approx(-40.0)
