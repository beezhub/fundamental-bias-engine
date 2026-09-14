"""Tests for the shared request path every source inherits.

`BaseDataSource` exists so the six concrete sources cannot drift apart on
policy, which means the properties worth testing are the ones a source could
quietly get wrong on its own: reaching the network when the run is offline,
refetching something already cached, retrying a request that cannot succeed,
leaking a credential into a filename or an error message, and re-typing a
series' unit instead of copying it from the registry.

No test here reaches the network. Every HTTP route is mocked with `respx`,
every cache lives under `tmp_path`, and the sleeps are monkeypatched so a
backoff test is instant.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from fbe.config import DataConfig
from fbe.datasources import ALL_SOURCES
from fbe.datasources.base import (
    REQUEST_TIMEOUT_SECONDS,
    BaseDataSource,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.cache import DiskCache
from fbe.datasources.fred import FredSource
from fbe.datasources.manual import ManualSource
from fbe.datasources.registry import SeriesRef
from fbe.types import Frequency, Observation

BASE_URL = "https://example.test/api/"
PATH = "series/observations"
PARAMS: Mapping[str, str | int | float] = {"series_id": "X1", "start": "2021-06-30"}
PAYLOAD = {"observations": [{"date": "2026-06-30", "value": "2.9"}]}
CREDENTIAL = "abcdef0123456789abcdef0123456789"

REF = SeriesRef(
    source="testsrc",
    series_id="X1",
    unit="percent",
    frequency=Frequency.DAILY,
)


class _Source(BaseDataSource):
    """A source with no credential, used for everything but the key tests."""

    name = "testsrc"
    base_url = BASE_URL
    rate_limit = RateLimit(requests=1000, per_seconds=60.0)
    retry = RetryPolicy(attempts=3, backoff_seconds=1.0, max_backoff_seconds=30.0)

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        return ()

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        return {("cpi_yoy", "USD"): REF}


class _KeyedSource(_Source):
    """A source that carries a credential, to test that it never escapes."""

    name = "keyedsrc"
    api_key_param = "api_key"

    def _api_key(self) -> str | None:
        return self.config.fred_api_key


class _OddlyKeyedSource(_KeyedSource):
    """A source whose credential is not named one of the cache's known three.

    `DiskCache` strips ``api_key``, ``token`` and ``app_token`` on its own, so a
    source using one of those names is protected by the cache whatever the base
    does. This one is not, which is what makes it the case that actually tests
    the base's own exclusion. Real sources do this: the OECD and Socrata
    endpoints both name their credential something else.
    """

    name = "oddsrc"
    api_key_param = "subscription_key"


@pytest.fixture
def data_config(tmp_path: Path) -> DataConfig:
    return DataConfig(cache_dir=tmp_path / "cache")


@pytest.fixture
def source(data_config: DataConfig) -> _Source:
    return _Source(data_config)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sleep instead of performing it."""
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("fbe.datasources.base.time.sleep", fake_sleep)
    return slept


# --- the offline short circuit ----------------------------------------------


@respx.mock
def test_offline_with_a_cached_entry_returns_it_and_makes_no_call(
    data_config: DataConfig,
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    _Source(data_config)._request(PATH, PARAMS)
    assert route.call_count == 1

    offline = _Source(replace(data_config, offline=True))
    assert offline._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 1


@respx.mock
def test_offline_with_a_cold_cache_raises_rather_than_returning_nothing(
    data_config: DataConfig,
) -> None:
    """An empty result would show up downstream as a coverage gap and hide the
    misconfiguration that actually caused it."""
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    offline = _Source(replace(data_config, offline=True))

    with pytest.raises(SourceError) as raised:
        offline._request(PATH, PARAMS)

    message = str(raised.value)
    assert "testsrc" in message
    assert PATH in message
    assert "offline" in message.lower()
    assert route.call_count == 0


# --- the cache lookup -------------------------------------------------------


@respx.mock
def test_a_fresh_cached_entry_is_served_without_a_network_call(
    data_config: DataConfig,
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    source = _Source(data_config)

    assert source._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 1

    assert source._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 1


@respx.mock
def test_an_expired_entry_is_refetched(data_config: DataConfig) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    source = _Source(replace(data_config, cache_ttl_hours=0))

    source._request(PATH, PARAMS)
    source._request(PATH, PARAMS)

    assert route.call_count == 2


@respx.mock
def test_a_successful_response_is_written_to_the_cache_once(
    data_config: DataConfig,
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _Source(data_config)

    source._request(PATH, PARAMS)

    directory = data_config.cache_dir / "testsrc"
    assert len(list(directory.glob("*.json"))) == 2

    cache = DiskCache(data_config)
    entry = cache.get("testsrc", source._cache_key(PATH, PARAMS))
    assert entry.body_path.read_bytes()


@respx.mock
def test_different_parameters_do_not_share_an_entry(data_config: DataConfig) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    source = _Source(data_config)

    source._request(PATH, PARAMS)
    source._request(PATH, {**PARAMS, "series_id": "X2"})

    assert route.call_count == 2


# --- retry ------------------------------------------------------------------


@respx.mock
def test_a_retryable_status_is_retried_up_to_attempts_then_raises(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    source = _Source(data_config)

    with pytest.raises(SourceError):
        source._request(PATH, PARAMS)

    assert route.call_count == source.retry.attempts == 3


@respx.mock
def test_a_retryable_status_that_then_succeeds_returns_the_body(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=PAYLOAD),
        ]
    )
    source = _Source(data_config)

    assert source._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_non_retryable_client_error_raises_on_the_first_response(
    data_config: DataConfig, no_sleep: list[float], status: int
) -> None:
    """Repeating a malformed request cannot help, so it is not repeated."""
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(status, text="nope")
    )
    source = _Source(data_config)

    with pytest.raises(SourceError):
        source._request(PATH, PARAMS)

    assert route.call_count == 1
    assert no_sleep == []


@respx.mock
def test_a_transport_error_is_retried(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """A dropped connection is exactly the failure a retry is for."""
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        side_effect=[
            httpx.ConnectError("dropped"),
            httpx.Response(200, json=PAYLOAD),
        ]
    )
    source = _Source(data_config)

    assert source._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 2


@respx.mock
def test_nothing_is_cached_when_every_attempt_fails(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(503))
    source = _Source(data_config)

    with pytest.raises(SourceError):
        source._request(PATH, PARAMS)

    assert not (data_config.cache_dir / "testsrc").exists()


# --- backoff ----------------------------------------------------------------


@respx.mock
def test_backoff_doubles_from_the_configured_start(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(503))

    class _Slow(_Source):
        retry = RetryPolicy(
            attempts=5,
            backoff_seconds=2.0,
            max_backoff_seconds=30.0,
            respect_retry_after=False,
        )

    with pytest.raises(SourceError):
        _Slow(data_config)._request(PATH, PARAMS)

    # Four waits between five attempts, doubling from the configured 2.0.
    assert no_sleep == [2.0, 4.0, 8.0, 16.0]


@respx.mock
def test_backoff_is_capped(data_config: DataConfig, no_sleep: list[float]) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(503))

    class _Capped(_Source):
        retry = RetryPolicy(
            attempts=5,
            backoff_seconds=2.0,
            max_backoff_seconds=5.0,
            respect_retry_after=False,
        )

    with pytest.raises(SourceError):
        _Capped(data_config)._request(PATH, PARAMS)

    assert no_sleep == [2.0, 4.0, 5.0, 5.0]


@respx.mock
def test_retry_after_wins_over_the_computed_backoff(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """The server's own figure beats ours: it knows when it will serve us."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )

    class _Polite(_Source):
        retry = RetryPolicy(
            attempts=3,
            backoff_seconds=1.0,
            max_backoff_seconds=30.0,
            respect_retry_after=True,
        )

    with pytest.raises(SourceError):
        _Polite(data_config)._request(PATH, PARAMS)

    assert no_sleep == [7.0, 7.0]


@respx.mock
def test_retry_after_is_ignored_when_the_policy_says_so(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )

    class _Ignoring(_Source):
        retry = RetryPolicy(
            attempts=3,
            backoff_seconds=1.0,
            max_backoff_seconds=30.0,
            respect_retry_after=False,
        )

    with pytest.raises(SourceError):
        _Ignoring(data_config)._request(PATH, PARAMS)

    assert no_sleep == [1.0, 2.0]


@respx.mock
def test_an_unparseable_retry_after_falls_back_to_the_backoff(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """A Retry-After as an HTTP date is legal and not handled here. Falling
    back to the computed backoff is right; treating it as zero is not."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
    )

    class _Two(_Source):
        retry = RetryPolicy(attempts=2, backoff_seconds=3.0, respect_retry_after=True)

    with pytest.raises(SourceError):
        _Two(data_config)._request(PATH, PARAMS)

    assert no_sleep == [3.0]


# --- throttling -------------------------------------------------------------


@respx.mock
def test_a_minimum_interval_is_waited_between_requests(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """The wait is the interval minus however long has passed since the last
    request, asserted against a bracket the test computes rather than a band.

    The previous assertion was ``0.5 * 0.75 < wait <= 0.5``, which holds only if
    the first request completes within 125ms. That is a statement about the
    runner, not about the throttle, and it failed on every one of eight runs on
    one machine and intermittently on CI.

    `_throttle` reads ``time.monotonic()`` once, at a moment inside the second
    ``_request`` call. Bracketing that call gives the earliest and latest gap it
    could have seen, and so the narrowest and widest residual it could have
    slept. The bracket is milliseconds wide, so it is tighter than the band it
    replaces: halving the quantity, or reading it from the wrong field, still
    falls outside.
    """
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    class _Spaced(_Source):
        rate_limit = RateLimit(
            requests=1000, per_seconds=60.0, min_interval_seconds=0.5
        )

    source = _Spaced(replace(data_config, cache_ttl_hours=0))
    source._request(PATH, PARAMS)

    recorded = source._request_times[-1]
    before = time.monotonic()
    source._request(PATH, PARAMS)
    after = time.monotonic()

    interval = _Spaced.rate_limit.min_interval_seconds
    assert len(no_sleep) == 1
    assert interval - (after - recorded) <= no_sleep[0]
    assert no_sleep[0] <= interval - (before - recorded)
    assert 0.0 < no_sleep[0] <= interval


@respx.mock
def test_the_request_budget_is_waited_out_when_exhausted(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    class _Tight(_Source):
        rate_limit = RateLimit(requests=2, per_seconds=60.0)

    source = _Tight(replace(data_config, cache_ttl_hours=0))
    for _ in range(3):
        source._request(PATH, PARAMS)

    assert len(no_sleep) == 1
    assert 60.0 * 0.75 < no_sleep[0] <= 60.0


@respx.mock
def test_a_cached_request_does_not_spend_the_budget(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """The throttle protects the remote endpoint. A cache read never touches
    it, so waiting before one is pure lost time on a morning deadline."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    class _Spaced(_Source):
        rate_limit = RateLimit(
            requests=1000, per_seconds=60.0, min_interval_seconds=5.0
        )

    source = _Spaced(data_config)
    source._request(PATH, PARAMS)
    source._request(PATH, PARAMS)

    assert no_sleep == []


# --- the credential ---------------------------------------------------------


@respx.mock
def test_the_api_key_is_added_to_the_request_by_the_base(
    data_config: DataConfig,
) -> None:
    """No subclass has to remember it, so no subclass can forget it."""
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    source = _KeyedSource(replace(data_config, fred_api_key=CREDENTIAL))

    source._request(PATH, PARAMS)

    assert route.calls[0].request.url.params["api_key"] == CREDENTIAL


@respx.mock
def test_the_api_key_reaches_neither_the_key_nor_the_cache_nor_an_error(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    config = replace(data_config, fred_api_key=CREDENTIAL)
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _KeyedSource(config)

    assert CREDENTIAL not in source._cache_key(PATH, PARAMS)
    source._request(PATH, PARAMS)

    for path in config.cache_dir.rglob("*"):
        if path.is_file():
            assert CREDENTIAL not in path.name
            assert CREDENTIAL.encode() not in path.read_bytes()

    respx.get(f"{BASE_URL}fails").mock(return_value=httpx.Response(500))
    with pytest.raises(SourceError) as raised:
        source._request("fails", PARAMS)
    assert CREDENTIAL not in str(raised.value)
    assert CREDENTIAL not in repr(raised.value)


@respx.mock
def test_rotating_the_api_key_does_not_invalidate_the_cache(
    data_config: DataConfig,
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )

    _KeyedSource(replace(data_config, fred_api_key=CREDENTIAL))._request(PATH, PARAMS)
    assert route.call_count == 1

    _KeyedSource(replace(data_config, fred_api_key="0" * 32))._request(PATH, PARAMS)
    assert route.call_count == 1


@respx.mock
def test_a_credential_under_an_unknown_name_still_never_reaches_the_cache(
    data_config: DataConfig,
) -> None:
    """The cache strips the three names it knows. A source free to name its
    credential anything is only protected if the base strips it too."""
    config = replace(data_config, fred_api_key=CREDENTIAL)
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )
    source = _OddlyKeyedSource(config)

    source._request(PATH, PARAMS)

    assert route.calls[0].request.url.params["subscription_key"] == CREDENTIAL
    written = [path for path in config.cache_dir.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert CREDENTIAL not in path.name
        assert CREDENTIAL.encode() not in path.read_bytes()


@respx.mock
def test_a_credential_passed_in_the_parameters_is_kept_out_of_the_cache(
    data_config: DataConfig,
) -> None:
    """The base adds the credential itself, but nothing stops a subclass from
    also putting it in the parameters it passes down. When it does, the
    credential still must not reach the sidecar, and the cache cannot catch
    this one because the name is not among the three it knows.
    """
    config = replace(data_config, fred_api_key=CREDENTIAL)
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    _OddlyKeyedSource(config)._request(PATH, {**PARAMS, "subscription_key": CREDENTIAL})

    written = [path for path in config.cache_dir.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert CREDENTIAL.encode() not in path.read_bytes()


def test_the_cache_key_ignores_a_credential_passed_in_the_parameters(
    data_config: DataConfig,
) -> None:
    """Otherwise rotating the key orphans every entry the source holds, and the
    cache's own exclusion does not cover a name it has never heard of."""
    source = _OddlyKeyedSource(replace(data_config, fred_api_key=CREDENTIAL))

    assert source._cache_key(PATH, {**PARAMS, "subscription_key": CREDENTIAL}) == (
        source._cache_key(PATH, PARAMS)
    )
    assert source._cache_key(PATH, {**PARAMS, "subscription_key": "0" * 32}) == (
        source._cache_key(PATH, PARAMS)
    )


@respx.mock
def test_rotating_a_credential_under_an_unknown_name_keeps_the_cache(
    data_config: DataConfig,
) -> None:
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=PAYLOAD)
    )

    _OddlyKeyedSource(replace(data_config, fred_api_key=CREDENTIAL))._request(
        PATH, PARAMS
    )
    assert route.call_count == 1

    _OddlyKeyedSource(replace(data_config, fred_api_key="0" * 32))._request(
        PATH, PARAMS
    )
    assert route.call_count == 1


# --- the cache key ----------------------------------------------------------


def test_cache_key_agrees_with_the_cache_module(source: _Source) -> None:
    """Two derivations that must never disagree, because one writes the entry
    and the other looks for it."""
    expected = DiskCache(source.config).key_for("testsrc", PATH, PARAMS)

    assert source._cache_key(PATH, PARAMS) == expected


def test_cache_key_agrees_even_when_a_credential_is_configured(
    data_config: DataConfig,
) -> None:
    source = _KeyedSource(replace(data_config, fred_api_key=CREDENTIAL))
    expected = DiskCache(source.config).key_for("keyedsrc", PATH, PARAMS)

    assert source._cache_key(PATH, PARAMS) == expected


# --- building an observation ------------------------------------------------


def test_observation_copies_its_provenance_from_the_ref(source: _Source) -> None:
    """Re-typing a unit per source is how a percentage becomes an index."""
    observation = source._observation(
        indicator="cpi_yoy",
        currency="USD",
        ref=REF,
        period=date(2026, 6, 30),
        value=2.9,
    )

    assert observation.source == REF.source
    assert observation.series_id == REF.series_id
    assert observation.unit == REF.unit
    assert observation.frequency == REF.frequency
    assert observation.indicator == "cpi_yoy"
    assert observation.currency == "USD"
    assert observation.value == 2.9
    assert observation.period == date(2026, 6, 30)


def test_observation_leaves_released_at_none_by_default(source: _Source) -> None:
    """Absent is its own answer. Phase 6 needs to know a release date was never
    published, not to receive a guess."""
    observation = source._observation(
        indicator="cpi_yoy",
        currency="USD",
        ref=REF,
        period=date(2026, 6, 30),
        value=2.9,
    )

    assert observation.released_at is None


def test_observation_never_derives_released_at_from_period(source: _Source) -> None:
    """Conflating the two is look-ahead bias: it dates a figure to the day it
    describes rather than the day it was published, which for a quarterly
    print is weeks early."""
    period = date(2026, 6, 30)
    observation = source._observation(
        indicator="cpi_yoy", currency="USD", ref=REF, period=period, value=2.9
    )

    assert observation.released_at is None
    assert observation.period == period


def test_observation_carries_a_supplied_release_timestamp(source: _Source) -> None:
    released = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
    observation = source._observation(
        indicator="cpi_yoy",
        currency="USD",
        ref=REF,
        period=date(2026, 6, 30),
        value=2.9,
        released_at=released,
    )

    assert observation.released_at == released
    assert observation.period == date(2026, 6, 30)


def test_observation_uses_the_ref_source_not_the_source_name(
    data_config: DataConfig,
) -> None:
    """The ref is the registry's answer to who published this, and the registry
    is what the coverage report is computed against."""
    other = SeriesRef(
        source="fred", series_id="CPIAUCSL", unit="index", frequency=Frequency.MONTHLY
    )
    observation = _Source(data_config)._observation(
        indicator="cpi_yoy",
        currency="USD",
        ref=other,
        period=date(2026, 6, 30),
        value=310.2,
    )

    assert observation.source == "fred"
    assert observation.series_id == "CPIAUCSL"
    assert observation.unit == "index"
    assert observation.frequency == Frequency.MONTHLY


# --- availability -----------------------------------------------------------

SCAFFOLDED_AVAILABLE = set(ALL_SOURCES) - {FredSource, ManualSource}
"""Sources whose ``available()`` is still a stub.

Remove a class from this set in the change that implements its ``available()``,
which is what makes the test below start demanding a real answer instead of a
raise. `FredSource` came out in #56 and `ManualSource` in #60."""


def test_available_is_true_when_the_base_has_no_prerequisite(source: _Source) -> None:
    assert source.available() is True


@respx.mock
@pytest.mark.parametrize(
    "source_class", ALL_SOURCES, ids=lambda cls: getattr(cls, "name", cls.__name__)
)
def test_available_never_reaches_the_network(
    source_class: type[BaseDataSource], data_config: DataConfig
) -> None:
    """Availability is a question about configuration, not connectivity.

    A source still scaffolded is listed in ``SCAFFOLDED_AVAILABLE`` and
    asserted to raise. That set is a debt counter rather than a blanket
    tolerance: landing a source means removing it from the set in the same
    change, at which point this test starts requiring a real answer. A bare
    ``except NotImplementedError`` here would assert nothing and would never
    force that decision.
    """
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    source = source_class(data_config)

    if source_class in SCAFFOLDED_AVAILABLE:
        with pytest.raises(NotImplementedError):
            source.available()
    else:
        assert isinstance(source.available(), bool)

    assert route.call_count == 0


# --- the client -------------------------------------------------------------


@respx.mock
def test_the_client_is_reused_across_requests(data_config: DataConfig) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _Source(replace(data_config, cache_ttl_hours=0))

    source._request(PATH, PARAMS)
    first = source._client
    source._request(PATH, PARAMS)

    assert source._client is first


@respx.mock
def test_close_releases_the_client(data_config: DataConfig) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _Source(data_config)
    source._request(PATH, PARAMS)

    source.close()

    assert source._client is None
    assert source.close() is None


def test_close_without_a_request_is_not_an_error(source: _Source) -> None:
    assert source.close() is None


# --- decoding ---------------------------------------------------------------


@respx.mock
def test_a_body_that_is_not_json_raises_rather_than_returning_text(
    data_config: DataConfig,
) -> None:
    """A source that silently returned the raw text here would push the parse
    failure into a pillar, where it would look like missing data."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(200, text="<html>down for maintenance</html>")
    )

    with pytest.raises(SourceError):
        _Source(data_config)._request(PATH, PARAMS)


@respx.mock
def test_a_subclass_can_override_decoding(data_config: DataConfig) -> None:
    """The curve sources publish CSV and a workbook, not JSON, so reading the
    response is the subclass's job and the request path stays shared."""

    class _Csv(_Source):
        name = "csvsrc"

        def _decode(self, body: bytes) -> object:
            return body.decode("utf-8").strip().split(",")

    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, text="a,b,c"))

    assert _Csv(data_config)._request(PATH, PARAMS) == ["a", "b", "c"]


@respx.mock
def test_the_cache_stores_the_raw_body_not_the_decoded_value(
    data_config: DataConfig,
) -> None:
    """Storing raw bytes is what makes a wrong parser a re-parse rather than a
    refetch of history the source may no longer serve."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _Source(data_config)
    source._request(PATH, PARAMS)

    entry = DiskCache(data_config).get("testsrc", source._cache_key(PATH, PARAMS))

    assert b'"observations"' in entry.body_path.read_bytes()


# --- gaps the reviews found -------------------------------------------------


@respx.mock
def test_a_transport_error_message_carries_no_credential(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """httpx renders the failing URL into its own exception text, query string
    and all, and this source's credential travels as a query parameter."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        side_effect=httpx.ConnectError(
            f"connection refused to {BASE_URL}{PATH}?api_key={CREDENTIAL}"
        )
    )
    source = _KeyedSource(replace(data_config, fred_api_key=CREDENTIAL))

    with pytest.raises(SourceError) as raised:
        source._request(PATH, PARAMS)

    assert CREDENTIAL not in str(raised.value)
    assert CREDENTIAL not in repr(raised.value)
    assert "[redacted]" in str(raised.value)


@respx.mock
def test_the_credential_does_not_reach_a_log_line(
    data_config: DataConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """The CLI documents -vv as showing every request, and httpx logs the full
    URL at INFO. Left alone that prints the key to a terminal."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _KeyedSource(replace(data_config, fred_api_key=CREDENTIAL))

    with caplog.at_level(logging.DEBUG):
        source._request(PATH, PARAMS)

    assert CREDENTIAL not in caplog.text


@respx.mock
def test_retry_on_status_is_read_from_the_policy(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """The status list is configuration, and nothing else in this file proves
    it is read rather than hardcoded to the default tuple."""

    class _Odd(_Source):
        retry = RetryPolicy(attempts=2, backoff_seconds=1.0, retry_on_status=(404,))

    retried = respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(404))
    with pytest.raises(SourceError):
        _Odd(data_config)._request(PATH, PARAMS)
    assert retried.call_count == 2

    once = respx.get(f"{BASE_URL}other").mock(return_value=httpx.Response(503))
    with pytest.raises(SourceError):
        _Odd(data_config)._request("other", PARAMS)
    assert once.call_count == 1


@respx.mock
def test_retry_after_is_honoured_downward_too(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """In preference to the computed backoff means in preference, not the
    larger of the two: the server knows when it will serve us again."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0.5"})
    )

    class _Slow(_Source):
        retry = RetryPolicy(attempts=2, backoff_seconds=10.0, respect_retry_after=True)

    with pytest.raises(SourceError):
        _Slow(data_config)._request(PATH, PARAMS)

    assert no_sleep == [0.5]


@respx.mock
def test_retry_after_is_capped_by_the_policy_ceiling(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """A daily-quota endpoint answers with a full day. max_backoff_seconds
    exists so a run fails fast and reports a gap rather than hanging."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "86400"})
    )

    class _Capped(_Source):
        retry = RetryPolicy(attempts=2, backoff_seconds=1.0, max_backoff_seconds=5.0)

    with pytest.raises(SourceError):
        _Capped(data_config)._request(PATH, PARAMS)

    assert no_sleep == [5.0]


@respx.mock
@pytest.mark.parametrize("header", ["-5", "inf", "nan", "-0.1"])
def test_a_nonsensical_retry_after_never_reaches_sleep(
    data_config: DataConfig, no_sleep: list[float], header: str
) -> None:
    """A negative or non-finite figure would raise out of time.sleep as
    something other than SourceError, which a collector recording coverage
    gaps would not catch."""
    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": header})
    )

    class _Two(_Source):
        retry = RetryPolicy(attempts=2, backoff_seconds=3.0, max_backoff_seconds=30.0)

    with pytest.raises(SourceError):
        _Two(data_config)._request(PATH, PARAMS)

    assert len(no_sleep) == 1
    assert no_sleep[0] >= 0.0
    assert math.isfinite(no_sleep[0])


@respx.mock
def test_a_redirect_is_not_read_as_data(data_config: DataConfig) -> None:
    """Redirects are not followed, so the body is the redirect stub. A CSV
    subclass would parse it into zero rows and report a coverage gap for a
    source that had merely moved."""

    class _Csv(_Source):
        name = "csvsrc"

        def _decode(self, body: bytes) -> object:
            return body.decode("utf-8").strip().split(",")

    respx.get(f"{BASE_URL}{PATH}").mock(
        return_value=httpx.Response(302, headers={"Location": f"{BASE_URL}moved"})
    )

    with pytest.raises(SourceError) as raised:
        _Csv(data_config)._request(PATH, PARAMS)

    assert "moved" in str(raised.value)


@respx.mock
def test_an_undecodable_body_is_not_cached(data_config: DataConfig) -> None:
    """Caching before decoding serves the same failure back for the whole TTL,
    and offline never expires anything, so one maintenance page would poison
    the key until someone deleted the file."""
    route = respx.get(f"{BASE_URL}{PATH}").mock(
        side_effect=[
            httpx.Response(200, text="<html>down for maintenance</html>"),
            httpx.Response(200, json=PAYLOAD),
        ]
    )
    source = _Source(data_config)

    with pytest.raises(SourceError):
        source._request(PATH, PARAMS)

    assert source._request(PATH, PARAMS) == PAYLOAD
    assert route.call_count == 2


@respx.mock
def test_a_bare_json_null_is_refused(data_config: DataConfig) -> None:
    """Returning None hands a subclass something to subscript, which surfaces
    as a TypeError rather than a recorded coverage gap."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, text="null"))

    with pytest.raises(SourceError):
        _Source(data_config)._request(PATH, PARAMS)


@respx.mock
def test_the_response_is_cached_exactly_once_per_request(
    data_config: DataConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[str] = []
    real_put = DiskCache.put

    def spy(
        self: DiskCache, source: str, key: str, *args: object, **kw: object
    ) -> object:
        writes.append(key)
        return real_put(self, source, key, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(DiskCache, "put", spy)
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    _Source(data_config)._request(PATH, PARAMS)

    assert len(writes) == 1


@respx.mock
def test_the_client_carries_the_configured_timeout(data_config: DataConfig) -> None:
    """A client with no timeout hangs, which is the failure the retry policy's
    own ceiling argues against."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))
    source = _Source(data_config)
    source._request(PATH, PARAMS)

    assert source._client is not None
    assert source._client.timeout.read == REQUEST_TIMEOUT_SECONDS


@respx.mock
def test_the_offline_error_names_the_source_itself(data_config: DataConfig) -> None:
    """Not merely by quoting the cache's own miss text, which would let the
    source name drop out of the message unnoticed."""
    offline = _Source(replace(data_config, offline=True))

    with pytest.raises(SourceError) as raised:
        offline._request(PATH, PARAMS)

    message = str(raised.value)
    assert message.index("testsrc") < message.index("no cache entry")


@respx.mock
def test_an_exhausted_retry_names_what_failed(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(503))

    with pytest.raises(SourceError) as raised:
        _Source(data_config)._request(PATH, PARAMS)

    message = str(raised.value)
    assert "testsrc" in message
    assert PATH in message
    assert "503" in message


@respx.mock
def test_a_refused_request_names_what_failed(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(404))

    with pytest.raises(SourceError) as raised:
        _Source(data_config)._request(PATH, PARAMS)

    message = str(raised.value)
    assert "testsrc" in message
    assert PATH in message
    assert "404" in message


@respx.mock
def test_the_throttle_applies_between_retries_not_just_between_requests(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """Being retried is exactly when a source is most likely to be over its
    budget, so a throttle hoisted out of the retry loop would spend the
    attempts as fast as the server could refuse them."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(503))

    class _Spaced(_Source):
        rate_limit = RateLimit(
            requests=1000, per_seconds=60.0, min_interval_seconds=0.25
        )
        retry = RetryPolicy(attempts=2, backoff_seconds=1.0, respect_retry_after=False)

    with pytest.raises(SourceError):
        _Spaced(data_config)._request(PATH, PARAMS)

    # One backoff between the two attempts, plus one throttle wait before the
    # second. A throttle outside the loop would leave only the backoff.
    assert len(no_sleep) == 2
    assert no_sleep[0] == 1.0
    assert 0.25 * 0.75 < no_sleep[1] <= 0.25


@respx.mock
def test_the_window_length_is_read_from_the_rate_limit(
    data_config: DataConfig, no_sleep: list[float]
) -> None:
    """Every other throttle test uses the default 60.0, so nothing proves the
    window is read rather than hardcoded."""
    respx.get(f"{BASE_URL}{PATH}").mock(return_value=httpx.Response(200, json=PAYLOAD))

    class _Wide(_Source):
        rate_limit = RateLimit(requests=1, per_seconds=120.0)

    source = _Wide(replace(data_config, cache_ttl_hours=0))
    source._request(PATH, PARAMS)
    source._request(PATH, PARAMS)

    assert len(no_sleep) == 1
    # A window hardcoded to the default 60.0 lands near 59.9, below the band.
    assert 120.0 * 0.75 < no_sleep[0] <= 120.0
