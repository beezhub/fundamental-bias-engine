"""Tests for the collector and ``fbe refresh``.

The collector is the one place that fans a request out across every source and
reconciles what comes back, so the properties worth testing are the ones whose
failure would be invisible on the output: a pair fetched from a source the
registry never named, an operator's correction silently losing to the vendor
print it was typed to replace, a second run quietly going back to the network,
an offline run reporting an empty success on a cold cache, and one dead source
taking the other six down with it.

No test reaches the network. Every request is mocked with ``respx`` and the
offline cases assert the route was never called at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from fbe.cli import EXIT_OK, EXIT_UNUSABLE, app
from fbe.config import DataConfig
from fbe.datasources import collect as collect_module
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy, SourceError
from fbe.datasources.cache import BODY_SUFFIX, META_SUFFIX
from fbe.datasources.curves import RBA_F2_URL, RbaSource
from fbe.datasources.fred import FredSource
from fbe.datasources.oecd import BASE_URL as OECD_BASE_URL
from fbe.datasources.oecd import OecdSource
from fbe.datasources.prices import PricesSource
from fbe.datasources.registry import SOURCE_MANUAL, SeriesRef
from fbe.types import Frequency, Observation

runner = CliRunner()

START = date(2021, 1, 1)
END = date(2026, 6, 30)
PERIOD = date(2026, 6, 1)
RELEASED_AT = datetime(2026, 6, 15, 13, 30, tzinfo=UTC)

FORBIDDEN = (
    "edge",
    "win rate",
    "hit rate",
    "backtest",
    "proven",
    "profitable",
    "expectancy",
)
"""Words a refresh must never print. It fills a cache; it measures nothing."""


@dataclass(frozen=True, slots=True)
class _Ask:
    """One call a fake source recorded, so routing can be asserted directly."""

    indicators: tuple[str, ...]
    currencies: tuple[str, ...]
    start: date
    end: date


def _ref(source: str) -> SeriesRef:
    """A minimal ref. Only ``source`` is read by the routing under test."""
    return SeriesRef(
        source=source,
        series_id="X",
        unit="percent",
        frequency=Frequency.MONTHLY,
        last_observed=date(2026, 6, 1),
    )


def _observation(
    indicator: str,
    currency: str,
    value: float,
    *,
    source: str = "alpha",
    period: date = PERIOD,
    released_at: datetime | None = None,
) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source=source,
        series_id="X",
        unit="percent",
        frequency=Frequency.MONTHLY,
        released_at=released_at,
    )


def _source(
    name: str,
    *,
    refs: Iterable[tuple[str, str]] = (),
    observations: Sequence[Observation] = (),
    error: Exception | None = None,
    available: bool | Exception = True,
    url: str | None = None,
) -> tuple[type[BaseDataSource], list[_Ask]]:
    """Build a source class and the list recording what was asked of it.

    Args:
        name: The source key, matching ``SeriesRef.source``.
        refs: The ``(indicator, currency)`` pairs this source owns.
        observations: What ``fetch`` returns when it succeeds.
        error: Raised by ``fetch`` instead of returning, when given.
        available: What ``available()`` returns, or an exception it raises.
        url: When given, ``fetch`` goes through the shared request path against
            this URL, so the cache and the offline short circuit are exercised
            rather than bypassed.

    Returns:
        The class and a list that gains one `_Ask` per ``fetch`` call.
    """
    asked: list[_Ask] = []
    ref_map = {pair: _ref(name) for pair in refs}

    class _Built(BaseDataSource):
        """A source under test."""

        rate_limit = RateLimit(requests=1000, per_seconds=60.0)
        retry = RetryPolicy(attempts=1)

        def available(self) -> bool:
            if isinstance(available, Exception):
                raise available
            return available

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            asked.append(
                _Ask(
                    tuple(sorted(indicators)),
                    tuple(sorted(currencies)),
                    start,
                    end,
                )
            )
            if error is not None:
                raise error
            if url is not None:
                self._request("", {})
            return observations

        def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
            return ref_map

    _Built.name = name
    _Built.base_url = url or ""
    return _Built, asked


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    """A config whose directories are all inside the test's own tmp_path."""
    return DataConfig(
        cache_dir=tmp_path / "cache",
        manual_dir=tmp_path / "manual",
        reports_dir=tmp_path / "reports",
    )


def _config_file(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n" + body
    )
    return path


def _run(path: Path, *args: str, offline: bool = False) -> Result:
    prefix = ["--config", str(path)]
    if offline:
        prefix.append("--offline")
    return runner.invoke(app, [*prefix, "refresh", *args])


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer machine with FRED_API_KEY set must not change the answers."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)


# --- criterion 1: a pair is requested from the source its ref names ---------


def test_each_source_is_asked_only_for_the_pairs_it_owns(
    data_config: DataConfig,
) -> None:
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP")])
    beta, beta_asked = _source("beta", refs=[("yield_2y", "AUD")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert alpha_asked == [_Ask(("cpi_yoy",), ("GBP",), START, END)]
    assert beta_asked == [_Ask(("yield_2y",), ("AUD",), START, END)]


def test_a_source_owning_nothing_in_the_request_is_never_fetched(
    data_config: DataConfig,
) -> None:
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP")])
    beta, beta_asked = _source("beta", refs=[("yield_2y", "AUD")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_asked
    assert beta_asked == []


@respx.mock
def test_only_the_owning_source_is_requested_on_the_wire(
    data_config: DataConfig,
) -> None:
    alpha_route = respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    beta_route = respx.get("https://beta.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], url="https://beta.test/")

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_route.call_count == 1
    assert beta_route.call_count == 0


def test_a_currency_outside_the_request_is_not_asked_for(
    data_config: DataConfig,
) -> None:
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP"), ("cpi_yoy", "JPY")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_asked == [_Ask(("cpi_yoy",), ("GBP",), START, END)]


@respx.mock
def test_the_cross_product_does_not_widen_what_reaches_the_wire(
    data_config: DataConfig,
) -> None:
    """``fetch`` takes two iterables, not a set of pairs, so the collector hands
    each source the union of its routed indicators and its routed currencies.
    That union spans pairs the registry routes elsewhere: ``yield_2y`` for EUR
    belongs to the ECB, not to FRED. Each source re-intersects against its own
    ``refs()``, so the requests that reach the wire are the routed pairs and
    nothing more. Asserted through a real source rather than a fake, because
    the fakes in this file cannot show it.
    """
    route = respx.get("https://api.stlouisfed.org/fred/series/observations").mock(
        return_value=httpx.Response(200, json={"observations": []})
    )

    collect_module.collect(
        replace(data_config, fred_api_key="k" * 32),
        start=START,
        end=END,
        sources=(FredSource,),
        indicators=["policy_rate", "yield_2y"],
        currencies=["USD", "EUR"],
    )

    requested = {call.request.url.params["series_id"] for call in route.calls}
    assert requested == {"DFEDTARU", "ECBDFR", "DGS2"}


# --- criterion 2: a manual entry beats a fetched value ----------------------


def test_a_manual_entry_wins_for_the_same_indicator_currency_and_period(
    data_config: DataConfig,
) -> None:
    fetched = _observation("cpi_yoy", "GBP", 2.4, source="alpha")
    typed = _observation("cpi_yoy", "GBP", 3.1, source=SOURCE_MANUAL)
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], observations=[fetched])
    manual, _ = _source(SOURCE_MANUAL, observations=[typed])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, manual),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert [(o.source, o.value) for o in result.observations] == [(SOURCE_MANUAL, 3.1)]


def test_a_manual_entry_for_another_period_does_not_replace_the_fetched_one(
    data_config: DataConfig,
) -> None:
    fetched = _observation("cpi_yoy", "GBP", 2.4, source="alpha", period=PERIOD)
    typed = _observation(
        "cpi_yoy", "GBP", 3.1, source=SOURCE_MANUAL, period=date(2026, 5, 1)
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], observations=[fetched])
    manual, _ = _source(SOURCE_MANUAL, observations=[typed])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, manual),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert sorted(o.value for o in result.observations) == [2.4, 3.1]


def test_the_override_source_is_asked_for_pairs_the_registry_routes_elsewhere(
    data_config: DataConfig,
) -> None:
    """The override cannot work if manual is only asked for its own refs.

    ``cpi_yoy/GBP`` routes to ``alpha`` here and to no manual ref at all, which
    is the ordinary case: the operator is correcting a vendor print, not filling
    a gap. If routing by ``refs()`` applied to manual as well, it would never be
    asked, and the correction would never be seen.
    """
    manual, manual_asked = _source(SOURCE_MANUAL)
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, manual),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert manual_asked == [_Ask(("cpi_yoy",), ("GBP",), START, END)]


def test_two_currencies_of_one_indicator_and_period_both_survive(
    data_config: DataConfig,
) -> None:
    """The merge key is a triple. Dropping a leg of it silently loses a leg."""
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP"), ("cpi_yoy", "AUD")],
        observations=[
            _observation("cpi_yoy", "GBP", 2.4),
            _observation("cpi_yoy", "AUD", 1.1),
        ],
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP", "AUD"],
    )

    assert {(o.currency, o.value) for o in result.observations} == {
        ("GBP", 2.4),
        ("AUD", 1.1),
    }


def test_two_indicators_of_one_currency_and_period_both_survive(
    data_config: DataConfig,
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP"), ("yield_2y", "GBP")],
        observations=[
            _observation("cpi_yoy", "GBP", 2.4),
            _observation("yield_2y", "GBP", 3.9),
        ],
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP"],
    )

    assert {(o.indicator, o.value) for o in result.observations} == {
        ("cpi_yoy", 2.4),
        ("yield_2y", 3.9),
    }


def test_a_fetched_value_survives_when_no_manual_entry_covers_it(
    data_config: DataConfig,
) -> None:
    fetched = _observation("cpi_yoy", "GBP", 2.4, source="alpha")
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], observations=[fetched])
    manual, _ = _source(SOURCE_MANUAL)

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, manual),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert [(o.source, o.value) for o in result.observations] == [("alpha", 2.4)]


# --- criterion 3: the TTL, and --force ---------------------------------------


@respx.mock
def test_a_second_collection_inside_the_ttl_makes_no_request(
    data_config: DataConfig,
) -> None:
    route = respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")

    for _ in range(2):
        collect_module.collect(
            data_config,
            start=START,
            end=END,
            sources=(alpha,),
            indicators=["cpi_yoy"],
            currencies=["GBP"],
        )

    assert route.call_count == 1


@respx.mock
def test_force_refetches_inside_the_ttl(data_config: DataConfig) -> None:
    route = respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")

    for force in (False, True):
        collect_module.collect(
            data_config,
            start=START,
            end=END,
            sources=(alpha,),
            indicators=["cpi_yoy"],
            currencies=["GBP"],
            force=force,
        )

    assert route.call_count == 2


@respx.mock
def test_force_clears_only_the_sources_in_the_run(data_config: DataConfig) -> None:
    """A forced refresh of one source must not throw away another's cache."""
    respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    beta_route = respx.get("https://beta.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], url="https://beta.test/")
    both = (alpha, beta)
    request = {"indicators": ["cpi_yoy", "yield_2y"], "currencies": ["GBP", "AUD"]}

    collect_module.collect(data_config, start=START, end=END, sources=both, **request)
    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=both,
        selected=["alpha"],
        force=True,
        **request,
    )
    collect_module.collect(data_config, start=START, end=END, sources=both, **request)

    assert beta_route.call_count == 1


# --- criterion 4: offline ----------------------------------------------------


@respx.mock
def test_an_offline_collection_makes_no_request_at_all(
    data_config: DataConfig,
) -> None:
    route = respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")

    collect_module.collect(
        replace(data_config, offline=True),
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert route.call_count == 0


@respx.mock
def test_an_offline_cold_cache_is_a_failure_not_an_empty_success(
    data_config: DataConfig,
) -> None:
    respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")

    result = collect_module.collect(
        replace(data_config, offline=True),
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert not result.usable
    assert result.observations == ()
    detail = " ".join(outcome.detail for outcome in result.failed)
    assert "offline" in detail
    assert "cache" in detail


def test_an_offline_cold_cache_exits_one_and_names_what_was_missing(
    tmp_path: Path,
) -> None:
    """The exit code alone cannot tell a refusal from an unhandled exception."""
    result = _run(_config_file(tmp_path), offline=True)

    assert result.exit_code == EXIT_UNUSABLE
    assert "offline" in result.stdout
    assert "cache holds no entry" in result.stdout
    assert result.stdout.rstrip().endswith("Cache: empty.")


# --- criterion 5: a source that raises is a named gap ------------------------


def test_one_source_failing_does_not_stop_the_others(
    data_config: DataConfig,
) -> None:
    good = _observation("yield_2y", "AUD", 3.4, source="beta")
    alpha, _ = _source(
        "alpha", refs=[("cpi_yoy", "GBP")], error=SourceError("alpha is down")
    )
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], observations=[good])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert result.observations == (good,)
    assert [o.source for o in result.failed] == ["alpha"]


def test_the_failure_names_the_source_and_says_what_happened(
    data_config: DataConfig,
) -> None:
    alpha, _ = _source(
        "alpha", refs=[("cpi_yoy", "GBP")], error=SourceError("alpha is down")
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert [o.source for o in result.failed] == ["alpha"]
    assert "alpha is down" in result.failed[0].detail


def test_a_scaffolded_source_is_skipped_rather_than_failing(
    data_config: DataConfig,
) -> None:
    """No shipped source is scaffolded here any more, since #208, but the
    collector's answer to one must stay a skip: a source that has not landed
    is not a source that failed."""
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        available=NotImplementedError(
            "fbe.datasources.alpha.Alpha.available is scaffolded; "
            "see docs/roadmap.md Phase 1"
        ),
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_asked == []
    assert result.failed == ()
    assert [o.status for o in result.outcomes] == [collect_module.SourceStatus.SKIPPED]
    assert result.outcomes[0].detail == "scaffolded, not yet built"


def test_an_unavailable_source_is_skipped_and_says_so(
    data_config: DataConfig,
) -> None:
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP")], available=False)

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_asked == []
    assert "not configured" in result.outcomes[0].detail


def test_a_source_whose_refs_are_scaffolded_is_skipped(
    data_config: DataConfig,
) -> None:
    class _NoRefs(BaseDataSource):
        name = "norefs"
        rate_limit = RateLimit(requests=1000, per_seconds=60.0)

        def available(self) -> bool:
            return True

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            raise AssertionError("fetch must not be reached")

        def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
            raise NotImplementedError(
                "fbe.datasources.norefs.NoRefs.refs is scaffolded; "
                "see docs/roadmap.md Phase 1"
            )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(_NoRefs,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert result.failed == ()
    assert [o.status for o in result.outcomes] == [collect_module.SourceStatus.SKIPPED]
    assert result.outcomes[0].detail == "scaffolded, not yet built"


def test_a_source_raising_an_unexpected_error_is_still_only_a_gap(
    data_config: DataConfig,
) -> None:
    """A parser bug must cost one source, not the whole morning's refresh."""
    good = _observation("yield_2y", "AUD", 3.4, source="beta")
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], error=ValueError("bad row"))
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], observations=[good])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert result.observations == (good,)
    assert "bad row" in result.failed[0].detail


def test_a_source_whose_refs_raise_for_another_reason_is_only_a_gap(
    data_config: DataConfig,
) -> None:
    """A routing table that raises must cost one source, not the whole run."""

    class _BadRefs(BaseDataSource):
        name = "badrefs"
        rate_limit = RateLimit(requests=1000, per_seconds=60.0)

        def available(self) -> bool:
            return True

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            raise AssertionError("fetch must not be reached")

        def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
            raise KeyError("no such indicator")

    good = _observation("yield_2y", "AUD", 3.4, source="beta")
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], observations=[good])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(_BadRefs, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert result.observations == (good,)
    assert [o.source for o in result.failed] == ["badrefs"]
    assert "no such indicator" in result.failed[0].detail


def test_a_subclass_answers_to_the_key_it_inherits(
    data_config: DataConfig,
) -> None:
    """The class-side name and ``self.name`` must be the same string.

    If they are not, a source is routed under one key and its output line is
    labelled with another, and ``--source`` cannot select it by either.
    """
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )

    class _Inheriting(alpha):  # type: ignore[valid-type, misc]
        """A source that does not restate its parent's key."""

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(_Inheriting,),
        selected=["alpha"],
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert alpha_asked
    assert [o.source for o in result.outcomes] == ["alpha"]


def test_an_empty_source_option_still_refreshes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--source`` never passed must not read as "select no source at all"."""
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path))

    assert alpha_asked
    assert result.exit_code == EXIT_OK


def test_a_negative_lookback_is_refused_rather_than_traced_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path, "scoring:\n  lookback_years: -1\n"))

    assert result.exit_code == 2
    assert "lookback_years" in result.stdout + str(result.stderr)


# --- criterion 6: exit codes -------------------------------------------------


def test_a_refresh_that_collected_something_is_usable(
    data_config: DataConfig,
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert result.usable


def test_every_source_failing_is_not_usable(data_config: DataConfig) -> None:
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], error=SourceError("down"))
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], error=SourceError("down"))

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert not result.usable


def test_a_run_that_collected_nothing_is_not_usable(
    data_config: DataConfig,
) -> None:
    """Every source answering with nothing is a cache that gained nothing."""
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert not result.usable


def test_one_source_failing_beside_one_that_worked_is_still_usable(
    data_config: DataConfig,
) -> None:
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], error=SourceError("down"))
    beta, _ = _source(
        "beta",
        refs=[("yield_2y", "AUD")],
        observations=[_observation("yield_2y", "AUD", 3.4, source="beta")],
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert result.usable


def test_an_unknown_source_name_is_refused_and_names_what_exists(
    data_config: DataConfig,
) -> None:
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")])

    with pytest.raises(ValueError, match="alpha") as caught:
        collect_module.collect(
            data_config,
            start=START,
            end=END,
            sources=(alpha,),
            selected=["nosuch"],
            indicators=["cpi_yoy"],
            currencies=["GBP"],
        )

    assert "nosuch" in str(caught.value)


def test_a_window_that_ends_before_it_starts_is_refused(
    data_config: DataConfig,
) -> None:
    """Every source would answer with nothing, and the run would look fine."""
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP")])

    with pytest.raises(ValueError, match="after it ends"):
        collect_module.collect(
            data_config,
            start=END,
            end=START,
            sources=(alpha,),
            indicators=["cpi_yoy"],
            currencies=["GBP"],
        )

    assert alpha_asked == []


def test_a_since_in_the_future_is_a_usage_error(tmp_path: Path) -> None:
    ahead = date.today().replace(year=date.today().year + 1)

    result = _run(_config_file(tmp_path), "--since", ahead.isoformat())

    assert result.exit_code == 2


def test_selecting_no_source_at_all_is_refused(data_config: DataConfig) -> None:
    """Fanning out over nothing has no meaning, and it looks like success.

    An empty selection could be read as everything or as nothing, and the two
    are too far apart to guess between, so it is refused rather than guessed.
    """
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )

    with pytest.raises(ValueError, match="no sources"):
        collect_module.collect(
            data_config,
            start=START,
            end=END,
            sources=(alpha,),
            selected=[],
            indicators=["cpi_yoy"],
            currencies=["GBP"],
        )

    assert alpha_asked == []


def test_an_unknown_source_name_is_a_usage_error_from_the_command(
    tmp_path: Path,
) -> None:
    result = _run(_config_file(tmp_path), "--source", "nosuch")

    assert result.exit_code == 2


# --- criterion 7: the output shape ------------------------------------------


def _cli_sources(
    monkeypatch: pytest.MonkeyPatch, *sources: type[BaseDataSource]
) -> None:
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", tuple(sources))


def test_each_completed_source_prints_series_observations_and_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP"), ("cpi_yoy", "JPY")],
        observations=[
            _observation("cpi_yoy", "GBP", 2.4),
            _observation("cpi_yoy", "JPY", 0.9),
            _observation("cpi_yoy", "JPY", 1.1, period=date(2026, 5, 1)),
        ],
    )
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path))

    assert "2 series" in result.stdout
    assert "3 observations" in result.stdout
    line = next(row for row in result.stdout.splitlines() if row.startswith("alpha"))
    assert re.search(r"\d+\.\d+s$", line), line


def test_a_source_not_selected_prints_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")])
    _cli_sources(monkeypatch, alpha, beta)

    result = _run(_config_file(tmp_path), "--source", "alpha")

    assert "beta" in result.stdout
    assert "skipped" in result.stdout
    assert "not selected" in result.stdout


def test_the_closing_line_summarises_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path))

    assert result.stdout.rstrip().splitlines()[-1].startswith("Cache:")


def test_a_successful_refresh_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path))

    assert result.exit_code == EXIT_OK


def test_every_source_failing_exits_one_from_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], error=SourceError("down"))
    _cli_sources(monkeypatch, alpha)

    result = _run(_config_file(tmp_path))

    assert result.exit_code == EXIT_UNUSABLE
    assert "down" in result.stdout


def test_the_output_claims_nothing_about_performance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    printed = _run(_config_file(tmp_path)).stdout.lower()

    # Absence alone would hold for output that was never printed at all.
    assert "alpha" in printed
    assert "1 observations" in printed
    assert [word for word in FORBIDDEN if word in printed] == []


# --- criterion 8: the coverage gaps -----------------------------------------


def test_every_indicator_with_a_gap_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gaps = {f"indicator_{n}": ("JPY", "NZD") for n in range(12)}
    monkeypatch.setattr(collect_module, "stale_refs", lambda asof=None: gaps)
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    printed = _run(_config_file(tmp_path)).stdout

    missing = [key for key in gaps if key not in printed]
    assert missing == []


def test_the_gap_list_names_the_currencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        collect_module, "stale_refs", lambda asof=None: {"cpi_yoy": ("JPY", "NZD")}
    )
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    printed = _run(_config_file(tmp_path)).stdout

    assert "JPY" in printed
    assert "NZD" in printed


def test_a_registry_with_no_gaps_says_so_rather_than_printing_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collect_module, "stale_refs", lambda asof=None: {})
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    printed = _run(_config_file(tmp_path)).stdout.lower()

    assert "coverage" in printed


def test_the_gaps_are_aged_against_the_run_date_not_the_registry_default(
    data_config: DataConfig,
) -> None:
    """Defaulting to ``VERIFIED_ON`` would report a year-old registry as healthy."""
    seen: list[date | None] = []

    def _record(asof: date | None = None) -> Mapping[str, tuple[str, ...]]:
        seen.append(asof)
        return {}

    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
        stale=_record,
    )

    assert seen == [END]


# --- criterion 9: --source and --since ---------------------------------------


def test_source_restricts_the_run_to_the_named_sources(
    data_config: DataConfig,
) -> None:
    alpha, alpha_asked = _source("alpha", refs=[("cpi_yoy", "GBP")])
    beta, beta_asked = _source("beta", refs=[("yield_2y", "AUD")])

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        selected=["beta"],
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    assert alpha_asked == []
    assert beta_asked


def test_since_sets_the_earliest_period_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    _run(_config_file(tmp_path), "--since", "2024-02-29")

    assert alpha_asked[0].start == date(2024, 2, 29)


def test_since_defaults_to_the_scoring_lookback_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, alpha_asked = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP")],
        observations=[_observation("cpi_yoy", "GBP", 2.4)],
    )
    _cli_sources(monkeypatch, alpha)

    _run(_config_file(tmp_path, "scoring:\n  lookback_years: 3\n"))

    today = date.today()
    assert alpha_asked[0].start == collect_module.lookback_start(today, 3)
    assert alpha_asked[0].end == today


@pytest.mark.parametrize(
    ("today", "years", "expected"),
    [
        (date(2026, 9, 15), 5, date(2021, 9, 15)),
        (date(2026, 1, 1), 1, date(2025, 1, 1)),
        (date(2024, 2, 29), 1, date(2023, 2, 28)),
        (date(2024, 2, 29), 4, date(2020, 2, 29)),
    ],
)
def test_the_lookback_start_lands_the_same_day_n_years_earlier(
    today: date, years: int, expected: date
) -> None:
    assert collect_module.lookback_start(today, years) == expected


def test_force_is_refused_offline_rather_than_clearing_the_only_copy(
    tmp_path: Path,
) -> None:
    """Offline plus force would delete the cache and be unable to refill it."""
    result = _run(_config_file(tmp_path), "--force", offline=True)

    assert result.exit_code == 2


# --- criterion 10: released_at survives untouched ----------------------------


def test_released_at_survives_collection_unchanged(
    data_config: DataConfig,
) -> None:
    stamped = _observation("cpi_yoy", "GBP", 2.4, released_at=RELEASED_AT)
    unstamped = _observation("yield_2y", "AUD", 3.4, source="beta")
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], observations=[stamped])
    beta, _ = _source("beta", refs=[("yield_2y", "AUD")], observations=[unstamped])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha, beta),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["GBP", "AUD"],
    )

    stamps = {o.indicator: o.released_at for o in result.observations}
    assert stamps == {"cpi_yoy": RELEASED_AT, "yield_2y": None}


def test_the_collector_never_fills_released_at_from_the_period(
    data_config: DataConfig,
) -> None:
    unstamped = _observation("cpi_yoy", "GBP", 2.4)
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], observations=[unstamped])

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert result.observations[0].released_at is None
    assert result.observations[0] == unstamped


# --- the request the collector builds by default -----------------------------


def test_the_default_request_covers_the_whole_registry_and_universe(
    data_config: DataConfig,
) -> None:
    """A refresh with no narrowing asks for everything the registry knows."""
    from fbe.datasources.registry import GLOBAL, INDICATORS
    from fbe.universe import G10

    pairs = [(key, currency) for key in INDICATORS for currency in (*G10, GLOBAL)]
    alpha, alpha_asked = _source("alpha", refs=pairs)

    collect_module.collect(data_config, start=START, end=END, sources=(alpha,))

    assert set(alpha_asked[0].indicators) == set(INDICATORS)
    assert set(alpha_asked[0].currencies) == {*G10, GLOBAL}


def test_observations_come_back_in_a_stable_order(data_config: DataConfig) -> None:
    served = [
        _observation("cpi_yoy", "AUD", 1.0, period=date(2026, 5, 1)),
        _observation("cpi_yoy", "GBP", 2.4),
        _observation("cpi_yoy", "AUD", 1.1),
    ]
    alpha, _ = _source(
        "alpha",
        refs=[("cpi_yoy", "GBP"), ("cpi_yoy", "AUD")],
        observations=served,
    )

    result = collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP", "AUD"],
    )

    keys = [(o.indicator, o.currency, o.period) for o in result.observations]
    assert keys == sorted(keys)
    # The source served them in an order that is neither sorted nor sorted when
    # reversed, so this cannot pass by the input happening to arrive ordered.
    assert keys != [(o.indicator, o.currency, o.period) for o in served]
    assert keys != list(reversed([(o.indicator, o.currency, o.period) for o in served]))


def test_every_source_gets_closed_even_when_one_fails(
    data_config: DataConfig,
) -> None:
    """An unclosed client leaks a socket per source per run."""
    closed: list[str] = []

    class _Closing(BaseDataSource):
        name = "closing"
        rate_limit = RateLimit(requests=1000, per_seconds=60.0)

        def available(self) -> bool:
            return True

        def close(self) -> None:
            closed.append(self.name)
            super().close()

        def fetch(
            self,
            indicators: Iterable[str],
            currencies: Iterable[str],
            start: date,
            end: date,
        ) -> Sequence[Observation]:
            raise SourceError("down")

        def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
            return {("cpi_yoy", "GBP"): _ref("closing")}

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(_Closing,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    assert closed == ["closing"]


@respx.mock
def test_the_cache_is_written_where_the_config_points(
    data_config: DataConfig,
) -> None:
    """A refresh that writes outside ``cache_dir`` leaves a test's cache on disk."""
    respx.get("https://alpha.test/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alpha, _ = _source("alpha", refs=[("cpi_yoy", "GBP")], url="https://alpha.test/")

    collect_module.collect(
        data_config,
        start=START,
        end=END,
        sources=(alpha,),
        indicators=["cpi_yoy"],
        currencies=["GBP"],
    )

    directory = data_config.cache_dir / "alpha"
    bodies = [
        path
        for path in directory.glob(f"*{BODY_SUFFIX}")
        if not path.name.endswith(META_SUFFIX)
    ]
    assert len(bodies) == 1
    assert json.loads(bodies[0].read_bytes()) == {"ok": True}


@respx.mock
def test_the_three_sources_landed_in_208_are_asked_rather_than_skipped(
    data_config: DataConfig,
) -> None:
    """Before #208 ``fbe refresh`` printed these three as "scaffolded, not yet
    built", and MONETARY and INFLATION scored n/a for every currency because
    the two-year yields and the OECD prices never reached the cache. One real
    series per source is enough to show the collector now asks them."""
    fixtures = Path(__file__).parent / "fixtures"
    respx.get(url__startswith=OECD_BASE_URL).mock(
        return_value=httpx.Response(
            200, text=(fixtures / "oecd_aus_cpi_quarterly.csv").read_text()
        )
    )
    respx.get(RBA_F2_URL).mock(
        return_value=httpx.Response(
            200, text=(fixtures / "rba_f2_2y.csv").read_text(encoding="utf-8-sig")
        )
    )

    result = collect_module.collect(
        data_config,
        start=date(2026, 5, 1),
        end=date(2026, 9, 30),
        sources=(OecdSource, RbaSource, PricesSource),
        indicators=["cpi_yoy", "yield_2y"],
        currencies=["AUD"],
    )

    by_name = {o.source: o for o in result.outcomes}
    assert by_name["oecd"].status is collect_module.SourceStatus.COMPLETED
    assert by_name["rba"].status is collect_module.SourceStatus.COMPLETED
    assert {(o.indicator, o.currency) for o in result.observations} == {
        ("cpi_yoy", "AUD"),
        ("yield_2y", "AUD"),
    }
    # Stooq is asked and answers honestly that the registry routes nothing to
    # it today, which is a different line from "not yet built".
    assert by_name["stooq"].status is collect_module.SourceStatus.SKIPPED
    assert by_name["stooq"].detail == "no series routed to it"
