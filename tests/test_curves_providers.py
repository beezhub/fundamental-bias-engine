"""One source per curve provider, so one failing costs its own currency only.

ADR 0013 and #218. Before this the seven central banks sat behind one
``curves`` source, and a single provider raising withheld every two-year yield
in the run. These tests go through `collect.collect` and the ``refresh``
command rather than the source alone, because the claim is about what the
collector records and what the operator reads, not about what a method
returns.

No test reaches the network. Every route is mocked with `respx`, every cache
lives under ``tmp_path``, and the bodies are the same live captures the other
curves tests use.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fbe.cli import app
from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES, collect
from fbe.datasources.base import SourceError
from fbe.datasources.curves import (
    BOC_BASE_URL,
    ECB_BASE_URL,
    PROVIDER_SOURCES,
    RBA_F2_URL,
    BocSource,
    CurvesSource,
    EcbSource,
    RbaSource,
)
from fbe.datasources.registry import CURVE_SOURCES, INDICATORS
from fbe.pillars.monetary import MonetaryPillar
from fbe.types import Observation

FIXTURES = Path(__file__).parent / "fixtures"
BOC_BODY = (FIXTURES / "boc_2y_yield.json").read_text()
ECB_BODY = (FIXTURES / "ecb_2y_spot.csv").read_text()
RBA_BODY = (FIXTURES / "rba_f2_2y.csv").read_text(encoding="utf-8-sig")

START = date(2026, 9, 1)
END = date(2026, 9, 10)
THREE = (EcbSource, BocSource, RbaSource)

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing provider retries with a backoff; the test does not wait for it."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache", manual_dir=tmp_path / "manual")


def _serve_boc_and_rba() -> None:
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text=BOC_BODY)
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))


# ---------------------------------------------------------------------------
# The shape: seven sources, one per registry provider
# ---------------------------------------------------------------------------


def test_every_registry_provider_has_a_source_and_no_source_lacks_one() -> None:
    """A provider added to the registry and not here is a currency never fetched."""
    assert {cls.name for cls in PROVIDER_SOURCES} == CURVE_SOURCES
    assert all(issubclass(cls, CurvesSource) for cls in PROVIDER_SOURCES)


def test_all_sources_lists_the_providers_and_not_the_fan_out() -> None:
    names = [cls.name for cls in ALL_SOURCES]
    for cls in PROVIDER_SOURCES:
        assert cls.name in names
    assert "curves" not in names
    assert CurvesSource not in ALL_SOURCES


@pytest.mark.parametrize("cls", PROVIDER_SOURCES, ids=lambda cls: cls.name)
def test_a_provider_source_claims_only_its_own_refs(
    cls: type[CurvesSource], data_config: DataConfig
) -> None:
    """The collector routes by ``refs()``, so this is what keeps the ECB from
    being asked about the Canadian curve."""
    source = cls(data_config)
    try:
        assert {ref.source for ref in source.refs().values()} <= {cls.name}
        assert source.base_url
        assert source.name == cls.name
    finally:
        source.close()


@pytest.mark.parametrize("cls", PROVIDER_SOURCES, ids=lambda cls: cls.name)
def test_the_providers_refs_partition_the_fan_outs(
    cls: type[CurvesSource], data_config: DataConfig
) -> None:
    """Every ref the fan-out served is served by exactly one provider source."""
    everything = CurvesSource(data_config)
    mine = cls(data_config)
    try:
        expected = {
            key: ref for key, ref in everything.refs().items() if ref.source == cls.name
        }
        assert dict(mine.refs()) == expected
    finally:
        everything.close()
        mine.close()


@respx.mock
def test_a_fetcher_on_the_wrong_provider_is_refused_not_misrouted(
    data_config: DataConfig,
) -> None:
    """``base_url`` plus a whole URL from another host is a request to nowhere.

    Raised by name rather than sent, because the provider might answer it with
    a 404 page at HTTP 200 and the slow answer would be "no rows".
    """
    route = respx.route().mock(return_value=httpx.Response(200, text=BOC_BODY))
    ecb = EcbSource(data_config)
    try:
        with pytest.raises(SourceError, match="not under its root"):
            ecb.fetch_boc("BD.CDN.2YR.DQ.YLD", START, END)
    finally:
        ecb.close()
    assert route.call_count == 0


@respx.mock
def test_a_provider_source_sends_the_same_request_the_fan_out_did(
    data_config: DataConfig,
) -> None:
    """Stripping the root and letting the base re-add it must round-trip."""
    route = respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(200, text=ECB_BODY)
    )
    ecb = EcbSource(data_config)
    try:
        emitted = ecb.fetch(["yield_2y"], ["EUR"], START, END)
    finally:
        ecb.close()
    assert emitted
    assert str(route.calls.last.request.url).startswith(
        f"{ECB_BASE_URL}YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y?"
    )


# ---------------------------------------------------------------------------
# Rule 1 and rule 2: the others are returned, the failure is a named status
# ---------------------------------------------------------------------------


@respx.mock
def test_one_provider_failing_costs_its_currency_and_no_other(
    data_config: DataConfig,
) -> None:
    """The ECB answers 503 after every retry; Canada and Australia still land.

    Through the collector, because that is where the outcome is recorded and
    where, before #218, the whole ``curves`` line read ``failed`` and the cache
    held no two-year yield for anyone.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(503, text="down")
    )
    _serve_boc_and_rba()

    result = collect.collect(
        data_config,
        start=START,
        end=END,
        sources=THREE,
        indicators=["yield_2y"],
        currencies=["EUR", "CAD", "AUD"],
    )

    by_name = {o.source: o for o in result.outcomes}
    assert by_name["ecb"].status is collect.SourceStatus.FAILED
    assert "ecb" in by_name["ecb"].detail
    assert "EUR" in by_name["ecb"].detail
    assert by_name["boc"].status is collect.SourceStatus.COMPLETED
    assert by_name["rba"].status is collect.SourceStatus.COMPLETED
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("yield_2y", "CAD"),
        ("yield_2y", "AUD"),
    }
    assert not any(o.currency == "EUR" for o in result.observations)


@respx.mock
def test_the_refresh_output_names_the_failed_provider_on_its_own_line(
    tmp_path: Path,
) -> None:
    """An operator must be able to tell ``ecb failed`` from ``served nothing``.

    Rule 2 of ADR 0013: the failure is a status on the line, not detail text
    on a completed one, and the line carries the provider's name and the
    currency it cost.
    """
    respx.get(url__startswith=ECB_BASE_URL).mock(
        return_value=httpx.Response(503, text="down")
    )
    _serve_boc_and_rba()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
    )

    result = runner.invoke(
        app,
        ["--config", str(config), "refresh", "-s", "ecb", "-s", "boc", "-s", "rba"],
    )

    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line}
    assert lines["ecb"].split()[1] == "failed"
    assert "EUR" in lines["ecb"]
    assert lines["boc"].split()[1] != "failed"
    assert lines["rba"].split()[1] != "failed"
    assert "curves" not in result.stdout


# ---------------------------------------------------------------------------
# Rule 3: silence from a single-currency provider is still a raised error
# ---------------------------------------------------------------------------


@respx.mock
def test_a_provider_answering_no_rows_is_a_failure_not_an_empty_success(
    data_config: DataConfig,
) -> None:
    """A 200 with no session inside the window is a lost pillar, per provider."""
    respx.get(url__startswith=BOC_BASE_URL).mock(
        return_value=httpx.Response(200, text='{"observations": []}')
    )
    respx.get(RBA_F2_URL).mock(return_value=httpx.Response(200, text=RBA_BODY))

    result = collect.collect(
        data_config,
        start=START,
        end=END,
        sources=(BocSource, RbaSource),
        indicators=["yield_2y"],
        currencies=["CAD", "AUD"],
    )

    by_name = {o.source: o for o in result.outcomes}
    assert by_name["boc"].status is collect.SourceStatus.FAILED
    assert "CAD" in by_name["boc"].detail
    assert by_name["rba"].status is collect.SourceStatus.COMPLETED
    assert {o.currency for o in result.observations} == {"AUD"}


# ---------------------------------------------------------------------------
# What the lost currency looks like downstream: an explicit absence
# ---------------------------------------------------------------------------


def _monetary_set(currency: str, *, with_two_year: bool = True) -> list[Observation]:
    """One visible observation per monetary input, in the registry's units.

    Values differ by currency so the cross-section has spread to normalise.
    """
    period = date(2026, 9, 10)
    stamp = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    offset = float(sum(ord(c) for c in currency) % 7) / 10.0

    def one(
        indicator: str, value: float, when: date, released: datetime
    ) -> Observation:
        spec = INDICATORS[indicator]
        return Observation(
            indicator=indicator,
            currency=currency,
            value=value,
            period=when,
            source="test",
            series_id=indicator,
            unit=spec.unit,
            frequency=spec.frequency,
            released_at=released,
        )

    built = [
        one("policy_rate", 4.0 + offset, period, stamp),
        one(
            "cpi_yoy",
            2.0 + offset,
            date(2026, 8, 1),
            datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        ),
    ]
    if with_two_year:
        built += [
            one("yield_2y", 3.5 + offset, period, stamp),
            one("yield_2y_chg_1m", 10.0 + offset, period, stamp),
            one("yield_2y_chg_3m", -5.0 + offset, period, stamp),
        ]
    return built


def test_a_currency_whose_provider_failed_scores_monetary_as_absent() -> None:
    """The lost two-year reaches the score as ``z is None``, not as neutral.

    This is what the split buys: with the ECB down, EUR's monetary pillar is
    an explicit absence, which ``fbe score --pillars`` prints as n/a (pinned in
    ``tests/test_cli_score.py``), and every other currency still scores.
    Before #218 all six would have been absent together.
    """
    observations: list[Observation] = []
    for currency in ("CAD", "AUD", "GBP", "JPY", "USD"):
        observations += _monetary_set(currency)
    observations += _monetary_set("EUR", with_two_year=False)

    scored = MonetaryPillar().compute(
        observations, ["EUR", "CAD", "AUD", "GBP", "JPY", "USD"], date(2026, 9, 15)
    )

    assert scored["EUR"].z is None
    assert scored["EUR"].raw is None
    for currency in ("CAD", "AUD", "GBP", "JPY", "USD"):
        assert scored[currency].z is not None, currency
