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

import json
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

import httpx

from fbe.config import DataConfig
from fbe.datasources.cache import CREDENTIAL_PARAMS, CacheMiss, DiskCache
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "BaseDataSource",
    "ProbeRequest",
    "RateLimit",
    "RetryPolicy",
    "SourceError",
    "UndatedRevisionError",
    "checked_vintage",
    "undated_revision",
]

REQUEST_TIMEOUT_SECONDS = 30.0
"""Per-request timeout. The engine runs from a morning routine against a
wall-clock deadline, so a source that has stopped answering must become a
reported coverage gap quickly rather than holding up the run."""


class SourceError(RuntimeError):
    """A source could not satisfy a request.

    Raised for exhausted retries, malformed responses, and missing credentials.
    Not raised for an empty result: a source that legitimately holds nothing for
    a request returns an empty sequence, because "no observations" is data and
    "the request failed" is not, and the coverage figures on a report depend on
    telling those two apart.
    """


class UndatedRevisionError(SourceError):
    """An observation claims a revision without saying when it was published.

    A revision and the original print it corrects describe the same period, so
    the assumed publication lag dates both to the same day and a run reads the
    correction from the moment the original would have been available. The lag
    estimates when *a print for this period* appeared, and there is no
    assumption available that dates a correction to it: only the source knows.
    ADR 0007 rules that a source which cannot supply `Observation.released_at`
    may not express a revision.

    Raised where observations are built rather than dropped where they are
    read. A pillar refusing the shape silently would, on a live run, score
    today's currency off a superseded figure and report nothing, because on a
    live run the newest vintage is the only one that matters. Refusing at the
    boundary is the one point where the fact is still recoverable: the response
    to a hand-typed correction with no date is to say so, which is what gets
    the date into the file.

    A `SourceError` because it is a statement about what a source supplied, so
    `fbe.datasources.collect` reports it as that source's failure rather than
    dropping it. Its own type so that a narrow ``except ValueError`` around a
    row parser cannot read an operator's mistake as a malformed number.
    """


def undated_revision(observation: Observation) -> bool:
    """Whether this observation claims a vintage nothing can place in time.

    Args:
        observation: Any observation, built or read back from a cache.

    Returns:
        True when ``revision`` is above zero and ``released_at`` is ``None``.
        That pair is the one shape no consumer can date: the publication lag
        estimates when a print for the period appeared, and a correction to it
        shares the period, so the estimate would admit the correction from the
        original's date.

        False for an unrevised observation with no stamp, which is most of the
        universe and is admitted on the assumed lag.

    One predicate for both halves of ADR 0007's rule: `checked_vintage` raises
    on it at the source boundary and `fbe.pillars.base.BasePillar._visible`
    refuses it a second time for anything that arrived another way, such as a
    cache written before this rule existed. Two copies of the condition is one
    edit away from the halves disagreeing about which shape is refused.

    """
    return observation.revision > 0 and observation.released_at is None


def checked_vintage(observation: Observation, where: str = "") -> Observation:
    """Return ``observation`` unless it claims a revision it cannot date.

    Args:
        observation: A freshly built observation, before anything reads it.
        where: Optional locator for the message, such as the file and row a
            hand-typed observation came from. Omitted by a fetching source,
            which has no row to name and whose source name is already on the
            observation.

    Returns:
        The same observation, unchanged. Every source's construction path calls
        this, so the rule lives in one place rather than once per source: ADR
        0007's requirement is a property of the contract, and six copies of it
        would be six chances for the seventh source to omit it.

    Raises:
        UndatedRevisionError: When ``revision`` is above zero and
            ``released_at`` is ``None``. A revision with no release date cannot
            be placed in time by anything downstream: the publication lag dates
            an original print and a correction to that print shares its period,
            so the assumption admits the correction from the original's date.

    An observation with no revision and no release date is fine and is most of
    the universe. It is admitted on the assumed lag and counted in
    ``PillarScore.diagnostics["assumed_lag_inputs"]``, which is how much of a
    run rests on the assumption rather than on fact.

    """
    if undated_revision(observation):
        locator = f"{where} " if where else ""
        raise UndatedRevisionError(
            f"{locator}claims {observation.indicator} / {observation.currency} "
            f"for period {observation.period} at revision "
            f"{observation.revision} with no released_at. A revision and the "
            "print it corrects describe the same period, so the assumed "
            "publication lag would admit the correction from the original's "
            "date and a historical run would read a number that did not exist "
            "yet. Give the row a released_at, or record it at revision 0 as "
            "the figure that was published. ADR 0007."
        )
    return observation


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


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """One request a source vouches for, and how to tell its answer is genuine.

    ``fbe doctor`` used to judge a source on the status code of a bare GET of
    ``base_url``. Stooq answers a blocked request with HTTP 200 and an HTML
    proof-of-work page, so that verdict printed a blocked source as healthy
    with a latency to prove it. The fix cannot be to run the bare GET through
    the source's own decoder either: a healthy Stooq does not serve CSV at its
    root, so that reports a working source as blocked, which is the same
    defect with the sign flipped. Hence this: the source names a request that
    a healthy instance answers with its own content, and the check that tells
    that content from anything else.

    Attributes:
        path: Path relative to the source's ``base_url``, the empty string for
            the root, exactly as `BaseDataSource._request` takes it.
        params: Query parameters to send. Never a credential: doctor prints
            what it asked for on failure, and the request does not go through
            the source's own client.
        verify: Called with the raw body of a 2xx response. Returns for a body
            that is this source's own content and raises `SourceError` naming
            what was expected for anything else. A body it refuses is reported
            as answered-but-not-served, which is a different line from a
            timeout, a refusal and an error status because it needs a
            different response from the operator.

    """

    path: str
    params: Mapping[str, str]
    verify: Callable[[bytes], object]


class BaseDataSource(ABC):
    """Abstract base for every source, implementing the `DataSource` protocol.

    Subclasses supply ``name``, a `RateLimit`, and the two abstract methods.
    Everything else, the client lifecycle, retry, throttling, cache lookup and
    the offline short circuit, is handled here so that the sources cannot drift
    apart on policy.

    Attributes:
        name: Short source key, matching ``SeriesRef.source``.
        base_url: Root that the paths passed to `_request` resolve against.
            Empty on the base itself, since only a concrete source has one.
        api_key_param: Query parameter the source's credential belongs in, or
            ``None`` for a source that needs none. Named here so `_request` can
            add the key and `_cache_key` can leave it out, rather than every
            source doing both and one of them forgetting.
        default_headers: Headers sent on every request this source makes. Empty
            by default. Declared here rather than built per source so that a
            source needing one, such as the OECD's
            ``Accept: application/vnd.sdmx.data+csv``, does not have to
            construct its own client and thereby drift from the timeout, the
            logger setting and the offline behaviour this class fixes for
            everyone. Never put a credential here: it would reach the cache
            sidecar and the logs, which is what ``api_key_param`` exists to
            prevent.
        follow_redirects: Whether this source's client follows a 3xx. False by
            default, which is what makes `_fetch_with_retries` treat a redirect
            as a moved endpoint rather than parsing its empty body as no rows.
            A source sets it True only where a provider's documented URL
            redirects by design, as the Bank of England's interactive database
            does on every request; there the redirect is the endpoint and
            refusing it means never reaching the data.
        rate_limit: This source's request budget.
        retry: This source's retry policy.

    """

    name: str = "base"
    base_url: str = ""
    api_key_param: str | None = None
    default_headers: Mapping[str, str] = MappingProxyType({})
    follow_redirects: bool = False
    rate_limit: RateLimit = RateLimit()
    retry: RetryPolicy = RetryPolicy()

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: The effective `DataConfig`, which carries the cache
                directory, the TTL, the offline flag and any API key.

        """
        self.config = config
        self.cache = DiskCache(config)
        self._client: httpx.Client | None = None
        self._request_times: deque[float] = deque()

    def _api_key(self) -> str | None:
        """Return this source's credential, where it needs one.

        Overridden by a source that takes a key, so `_request` can add it
        without knowing which field of `DataConfig` holds it.

        Returns:
            The credential, or ``None`` when the source needs none or none is
            configured. ``None`` means the parameter is not sent at all, which
            is correct for the anonymous endpoints several sources expose.

        """
        return None

    def probe_request(self) -> ProbeRequest | None:
        """Describe the request ``fbe doctor`` should make to check this source.

        Returns:
            A `ProbeRequest` for a source that can name one cheap request and
            recognise its own answer to it, or ``None``, which is the base's
            answer. ``None`` does not mean the source is blocked or healthy; it
            means doctor has nothing better than a bare GET of ``base_url``
            judged on its status code, which is the verdict every source had
            before this hook existed and which a source keeps until it
            overrides this. A source whose provider is known to answer a
            blocked request with a 2xx and the wrong body should override it,
            because for that provider the status-only verdict is the wrong
            answer on exactly the day the operator runs doctor.

        """
        return None

    def close(self) -> None:
        """Release the HTTP client, if one was opened.

        Safe to call more than once, and safe on a source that never made a
        request: an offline run opens no client at all.
        """
        if self._client is not None:
            self._client.close()
            self._client = None

    def _decode(self, body: bytes) -> object:
        """Turn a raw response body into the value `_request` returns.

        JSON by default. A source whose endpoint publishes something else, such
        as the CSV and the workbook the curve sources read, overrides this
        rather than reimplementing the request path around it.

        Args:
            body: Raw response bytes, exactly as cached.

        Returns:
            The decoded body.

        Raises:
            SourceError: If the body is not the format this source expects. A
                maintenance page served with a 200 is the common case, and
                returning its text would push the failure downstream where it
                is indistinguishable from missing data.

        """
        try:
            decoded = json.loads(body)
        except ValueError as error:
            raise SourceError(
                f"{self.name} returned a body that is not JSON: {error}"
            ) from error
        if decoded is None:
            # A bare ``null`` is not an empty result. No endpoint in
            # docs/data-sources.md publishes one on success, and returning it
            # would hand a subclass a None to subscript, which surfaces as a
            # TypeError rather than a recorded coverage gap.
            raise SourceError(f"{self.name} returned a bare JSON null")
        return decoded

    def available(self) -> bool:
        """Report whether this source can be used on this run.

        The check is about configuration, not connectivity: a missing API key
        or a missing manual directory makes a source unavailable, a network
        blip does not. An unavailable source is skipped and recorded as a
        coverage gap on the report rather than aborting the run, because a
        partial bias with an honest coverage number beats no bias at all.

        Returns:
            True when the source is usable. True on the base itself, which has
            no credential and no directory to require. A source with a
            prerequisite overrides this and answers for its own; the default is
            not a stand-in for an unknown answer, it is the correct answer for
            a source that needs nothing configured.

        """
        return True

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
            SourceError: On exhausted retries, on a status that is not worth
                retrying, on a body this source cannot decode, or when
                ``offline`` is set and the cache holds nothing for this
                request.

                An empty body raises, because the default `_decode` cannot read
                one. That is not a judgement that the source holds nothing: a
                source reporting an empty result does so by returning an empty
                sequence from `fetch`, which is data, and this is the request
                failing to produce a body at all.

                No message here carries a request parameter, so a credential
                cannot reach one.

        """
        key = self._cache_key(path, params)

        if self.config.offline:
            try:
                entry = self.cache.get(self.name, key)
            except CacheMiss as miss:
                raise SourceError(
                    f"{self.name} cannot serve {path} because the run is "
                    f"offline and the cache holds no entry for it: {miss}"
                ) from miss
            return self._decode(entry.body_path.read_bytes())

        try:
            entry = self.cache.get(self.name, key)
        except CacheMiss:
            pass
        else:
            return self._decode(entry.body_path.read_bytes())

        body = self._fetch_with_retries(path, params)
        # Decode before writing. A body this source cannot read is not worth
        # keeping: caching it first would serve the same failure back for the
        # whole TTL, and offline never expires anything, so one maintenance
        # page returned with a 200 would poison the key until a person deleted
        # the file by hand.
        value = self._decode(body)
        self.cache.put(self.name, key, body, self._cacheable_params(params))
        return value

    def _cacheable_params(
        self, params: Mapping[str, str | int | float]
    ) -> Mapping[str, str]:
        """Return the parameters that may be written to the cache sidecar.

        The credential is never among them. `DiskCache.put` also strips the
        names it knows, but a source is free to name its key something else, so
        the credential is kept out here rather than relied on being caught
        downstream.

        Args:
            params: The caller's query parameters.

        Returns:
            The same parameters as strings, with this source's credential
            parameter removed.

        """
        return {
            str(name): str(value)
            for name, value in params.items()
            if name != self.api_key_param
        }

    def _fetch_with_retries(
        self, path: str, params: Mapping[str, str | int | float]
    ) -> bytes:
        """Perform the request, retrying what is worth retrying.

        Args:
            path: Path relative to ``base_url``.
            params: Query parameters, without the credential.

        Returns:
            The raw response body.

        Raises:
            SourceError: When the attempts are exhausted, or on the first
                response carrying a status outside ``retry_on_status``.

        """
        # The caller's parameters go on the wire as given. Stripping a
        # credential the caller supplied would turn a configured request into a
        # 401 reported as a malformed one. Only the sidecar and the cache key
        # drop it, which is `_cacheable_params`.
        wire_params = {str(name): str(value) for name, value in params.items()}
        key = self._api_key()
        if self.api_key_param is not None and key is not None:
            wire_params[self.api_key_param] = key

        if self._client is None:
            # httpx logs every request at INFO with the full URL, query string
            # included, and a credential travels as a query parameter here. The
            # CLI documents -vv as showing every request, so leaving this at
            # INFO would print the key to a terminal and into any log a bug
            # report carries. This is the one place it can be fixed for every
            # source at once.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            self._client = httpx.Client(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers=dict(self.default_headers),
                follow_redirects=self.follow_redirects,
            )

        backoff = self.retry.backoff_seconds
        last = "no attempt was made"
        for attempt in range(1, self.retry.attempts + 1):
            self._throttle()
            failed: httpx.Response | None = None
            try:
                response = self._client.get(
                    f"{self.base_url}{path}", params=wire_params
                )
            except httpx.HTTPError as error:
                # A transport failure is the case a retry exists for, so it is
                # treated like a retryable status rather than raised at once.
                # httpx renders the failing URL into its message, query string
                # and all, so the credential is redacted out before the text
                # goes anywhere a person could read it.
                last = f"{type(error).__name__}: {self._redact(str(error), key)}"
            else:
                if response.is_success:
                    return response.content
                if response.is_redirect:
                    # Redirects are not followed, so the body here is empty. A
                    # subclass decoding it would read zero rows and report a
                    # coverage gap for a source that simply moved.
                    raise SourceError(
                        f"{self.name} redirected {path} to "
                        f"{response.headers.get('Location', 'an unnamed URL')}, "
                        "which is a moved endpoint rather than a failure to fix "
                        "by retrying"
                    )
                last = f"HTTP {response.status_code}"
                if response.status_code not in self.retry.retry_on_status:
                    raise SourceError(
                        f"{self.name} refused {path}: {last}. The request "
                        "itself is wrong, so it was not retried."
                    )
                failed = response

            if attempt < self.retry.attempts:
                self._wait_before_retry(failed, backoff)
                backoff = min(backoff * 2, self.retry.max_backoff_seconds)

        raise SourceError(
            f"{self.name} could not fetch {path} after "
            f"{self.retry.attempts} attempts, last failure {last}"
        )

    @staticmethod
    def _redact(text: str, secret: str | None) -> str:
        """Remove a credential from text that is about to be raised or logged.

        Args:
            text: The message to sanitise.
            secret: The credential to remove, or ``None`` when the source has
                none, in which case the text is returned unchanged.

        Returns:
            The text with every occurrence of the credential replaced. Applied
            to anything built from a library's own message, because those
            render the failing URL and this source's key travels in the query
            string.

        """
        if not secret:
            return text
        return text.replace(secret, "[redacted]")

    def _wait_before_retry(
        self, response: httpx.Response | None, backoff: float
    ) -> None:
        """Sleep for the server's figure where it gave one, else the backoff.

        Args:
            response: The response that failed, or ``None`` for a transport
                error, which carries no headers to read.
            backoff: The computed delay in seconds for this attempt.

        """
        wait = backoff
        if self.retry.respect_retry_after and response is not None:
            header = response.headers.get("Retry-After")
            if header is not None:
                try:
                    # Retry-After may also be an HTTP date, which is legal and
                    # not handled here. Falling back to the computed backoff is
                    # right; reading it as zero would hammer a source that has
                    # just asked to be left alone.
                    parsed = float(header)
                except ValueError:
                    parsed = backoff
                if not math.isfinite(parsed):
                    parsed = backoff
                # Honoured in preference to the computed backoff, downward as
                # well as upward, but still inside the policy's ceiling. A
                # daily-quota endpoint answering "Retry-After: 86400" would
                # otherwise hold a morning run for a day, which is the exact
                # hang max_backoff_seconds exists to prevent. A negative figure
                # from a skewed clock would otherwise reach time.sleep and
                # raise ValueError rather than SourceError.
                wait = min(max(parsed, 0.0), self.retry.max_backoff_seconds)
        time.sleep(wait)

    def _throttle(self) -> None:
        """Block until this source's rate limit allows another request.

        Two limits, and the longer wins: the floor between consecutive requests
        and the budget for the window. Only calls that actually reach the
        network are counted, because the throttle protects the remote endpoint
        and a cache read never touches it.

        The wait is computed once and slept once rather than polled in a loop,
        so a monkeypatched sleep in a test cannot spin.
        """
        window = self.rate_limit.per_seconds
        now = time.monotonic()

        while self._request_times and now - self._request_times[0] >= window:
            self._request_times.popleft()

        wait = 0.0
        if self._request_times:
            gap = now - self._request_times[-1]
            wait = max(wait, self.rate_limit.min_interval_seconds - gap)
        if len(self._request_times) >= self.rate_limit.requests:
            wait = max(wait, window - (now - self._request_times[0]))

        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()

        self._request_times.append(now)

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
            A filesystem-safe cache key, identical to what
            `DiskCache.key_for` derives for the same source, path and
            parameters. The two must agree: one writes the entry and the other
            looks for it.

        """
        excluded = frozenset(CREDENTIAL_PARAMS)
        if self.api_key_param is not None:
            excluded = excluded | {self.api_key_param}
        return self.cache.key_for(self.name, path, params, exclude=excluded)

    def _observation(
        self,
        indicator: str,
        currency: str,
        ref: SeriesRef,
        period: date,
        value: float,
        released_at: datetime | None = None,
        revision: int = 0,
    ) -> Observation:
        """Build a canonical `Observation` from a raw value and its ref.

        Every fetching source funnels through this so that ``source``,
        ``series_id``, ``unit`` and ``frequency`` are copied from the registry
        rather than re-typed per source, which is how those four fields stay
        consistent with what the documentation claims.

        `fbe.datasources.manual.ManualSource` is the one exception and builds
        its own. A hand-typed row must carry ``source`` ``"manual"`` rather
        than the ref's, so a report can separate typed numbers from fetched
        ones, and must not borrow the ref's ``series_id``, which would claim a
        vendor's provenance for a number somebody keyed in.

        Args:
            indicator: Canonical indicator key.
            currency: ISO 4217 code, or ``"GLOBAL"``.
            ref: The registry entry the value came from.
            period: The period the value describes, not the release date.
            value: The published number, in ``ref.unit``, after
                ``ref.transform``.
            released_at: When the number hit the tape, where the source
                publishes it. Left ``None`` otherwise, and never derived from
                ``period``: the period a figure describes and the day it was
                published are different facts, and conflating them is exactly
                the look-ahead bias Phase 6 has to avoid. A quarterly print
                dated to the quarter it covers is weeks early.
            revision: Which vintage of ``period`` this is, zero for the figure
                as first published. A source that republishes a corrected
                figure must supply ``released_at`` with it, because nothing
                downstream can date a correction: see `checked_vintage` and
                ADR 0007. Defaults to zero, which is what every source here
                currently publishes.

        Returns:
            A populated `Observation`. ``source``, ``series_id``, ``unit`` and
            ``frequency`` are copied from ``ref`` rather than from anything the
            caller re-typed, so those four cannot drift from the registry the
            coverage report is computed against.

        Raises:
            UndatedRevisionError: When ``revision`` is above zero and
                ``released_at`` is ``None``. Refused here rather than dropped
                where it is read, because this is the last point at which the
                missing date is still recoverable.

        """
        return checked_vintage(
            Observation(
                indicator=indicator,
                currency=currency,
                value=value,
                period=period,
                source=ref.source,
                series_id=ref.series_id,
                unit=ref.unit,
                frequency=ref.frequency,
                released_at=released_at,
                revision=revision,
            )
        )
