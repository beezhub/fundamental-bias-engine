"""Shared machinery every data source inherits.

The `DataSource` protocol in `fbe.types` says what a source must do. This
module says how they all do the common parts of it: one HTTP client, one retry
policy, one rate limiter, one cache. A source subclass is then only the two
things that are genuinely source-specific, namely how to build a request and
how to read a response.

The contract that matters most
------------------------------
A source returns canonical indicator keys, never its own vocabulary. FRED calls
it ``CPIAUCSL``, the CFTC calls it ``099741``, the operator's YAML calls it
whatever the operator typed. All three come back as `Observation`s whose
``indicator`` field is a key from `fbe.datasources.registry.INDICATORS`. The
source's own identifier survives on ``Observation.series_id`` for audit, but
nothing downstream is allowed to branch on it.

This inversion is the point. Pillars stay free of vendor knowledge, so swapping
a source means editing the registry and one adapter, not every pillar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from fbe.config import DataConfig
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = ["BaseDataSource", "RateLimit", "RetryPolicy", "SourceError"]


class SourceError(RuntimeError):
    """A source could not satisfy a request.

    Raised for exhausted retries, malformed responses, and missing credentials.
    Not raised for an empty result: a source that legitimately holds nothing for
    a request returns an empty sequence, because "no observations" is data and
    "the request failed" is not, and the coverage figures on a report depend on
    telling those two apart.
    """


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How a source behaves when a request fails.

    Attributes:
        attempts: Total tries including the first. Three is the default because
            the failure modes worth retrying here, a dropped connection or a
            momentary 5xx, almost always clear on the second try, and anything
            that survives three tries is an outage that a longer wait will not
            fix.
        backoff_seconds: Delay before the second attempt. Doubles each time.
        max_backoff_seconds: Ceiling on that doubling. The engine runs from a
            morning routine against a wall-clock deadline, so it is better to
            fail fast and report a gap than to hang.
        retry_on_status: HTTP statuses worth retrying. 429 is included because
            every source here rate-limits, and 5xx because those are the
            server's problem. 4xx other than 429 means the request itself is
            wrong and repeating it will not help.
        respect_retry_after: Honour a ``Retry-After`` header when present, in
            preference to the computed backoff.

    """

    attempts: int = 3
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504)
    respect_retry_after: bool = True


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A source's request budget.

    Attributes:
        requests: Requests allowed per window.
        per_seconds: Length of the window.
        min_interval_seconds: Floor on the gap between two requests, applied
            even when the budget is not exhausted. Bursting into a public
            endpoint at full speed is how a free key gets revoked.

    """

    requests: int = 60
    per_seconds: float = 60.0
    min_interval_seconds: float = 0.0


class BaseDataSource(ABC):
    """Abstract base for every source, implementing the `DataSource` protocol.

    Subclasses supply ``name``, a `RateLimit`, and the two abstract methods.
    Everything else, the client lifecycle, retry, throttling, cache lookup and
    the offline short circuit, is handled here so that the sources cannot drift
    apart on policy.

    Attributes:
        name: Short source key, matching ``SeriesRef.source``.
        rate_limit: This source's request budget.
        retry: This source's retry policy.

    """

    name: str = "base"
    rate_limit: RateLimit = RateLimit()
    retry: RetryPolicy = RetryPolicy()

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: The effective `DataConfig`, which carries the cache
                directory, the TTL, the offline flag and any API key.
        """
        self.config = config

    def available(self) -> bool:
        """Report whether this source can be used on this run.

        The check is about configuration, not connectivity: a missing API key
        or a missing manual directory makes a source unavailable, a network
        blip does not. An unavailable source is skipped and recorded as a
        coverage gap on the report rather than aborting the run, because a
        partial bias with an honest coverage number beats no bias at all.

        Returns:
            True when the source is usable.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Return every observation this source holds for the request.

        Args:
            indicators: Canonical indicator keys. Keys this source does not
                back are ignored silently, since the collector fans the same
                request out to every source.
            currencies: ISO 4217 codes, plus ``"GLOBAL"`` where relevant.
            start: Earliest period wanted, inclusive.
            end: Latest period wanted, inclusive.

        Returns:
            Observations carrying canonical indicator keys. Order is not
            guaranteed and callers must not depend on it.

        Raises:
            SourceError: If the source is configured but could not be reached
                or returned something unreadable.
        """
        raise NotImplementedError

    @abstractmethod
    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return the registry entries this source is responsible for.

        Keyed by ``(indicator, currency)``. The collector uses this to route a
        request to the right sources instead of asking every source about every
        indicator, and a test uses it to assert that the registry and the
        source agree about who owns what.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.
        """
        raise NotImplementedError

    def _request(
        self,
        path: str,
        params: Mapping[str, str | int | float],
    ) -> object:
        """Perform one throttled, retried, cached HTTP GET against this source.

        The single funnel through which all network access passes. It applies,
        in order: the offline short circuit, the cache lookup, the rate limit
        wait, the request itself, the retry loop, and the cache write.

        Args:
            path: Path relative to the source's base URL.
            params: Query parameters. The API key, where one is needed, is
                added here rather than by the caller, so no subclass has to
                remember to include it and no log line can leak it.

        Returns:
            The decoded response body.

        Raises:
            SourceError: On exhausted retries, or when ``offline`` is set and
                the cache holds nothing for this request.
        """
        raise NotImplementedError

    def _throttle(self) -> None:
        """Block until this source's rate limit allows another request."""
        raise NotImplementedError

    def _cache_key(
        self,
        path: str,
        params: Mapping[str, str | int | float],
    ) -> str:
        """Derive the cache key for one request.

        The key covers the source name, the path and every parameter except the
        API key, which is excluded deliberately: the key is a credential, not
        part of the request's identity, and rotating it should not invalidate
        a cache.

        Args:
            path: Path relative to the source's base URL.
            params: Query parameters.

        Returns:
            A filesystem-safe cache key.
        """
        raise NotImplementedError

    def _observation(
        self,
        indicator: str,
        currency: str,
        ref: SeriesRef,
        period: date,
        value: float,
    ) -> Observation:
        """Build a canonical `Observation` from a raw value and its ref.

        Every source funnels through this so that ``source``, ``series_id``,
        ``unit`` and ``frequency`` are copied from the registry rather than
        re-typed per source, which is how those four fields stay consistent
        with what the documentation claims.

        Args:
            indicator: Canonical indicator key.
            currency: ISO 4217 code, or ``"GLOBAL"``.
            ref: The registry entry the value came from.
            period: The period the value describes, not the release date.
            value: The published number, after ``ref.transform``.

        Returns:
            A populated `Observation`.
        """
        raise NotImplementedError
