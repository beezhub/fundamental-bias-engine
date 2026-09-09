"""On-disk response cache, and the mechanism that makes runs reproducible.

Two jobs, and the second is the important one.

The obvious job is not re-fetching. A daily run touches around 150 FRED series,
8 CFTC contracts and one calendar feed. Most of those numbers change monthly or
quarterly, so refetching them every run wastes rate-limit budget on data that
has not moved.

The job that matters is reproducibility. `DataConfig.offline` makes every source
read the cache and never the network. With it set, a run produces byte-identical
inputs no matter when it is run, which is what makes it possible to re-derive
yesterday's report and see why it said what it said, to test the pillars against
fixed data, and to debug a bad call without the data shifting underfoot. Without
it, every run is against a moving target and no result is ever reproduced, only
approximated.

Layout
------
Under `DataConfig.cache_dir`, which defaults to ``data/cache``::

    data/cache/
      fred/
        a3f21c94e08b1d77.json
        a3f21c94e08b1d77.meta.json
      cftc/
      stooq/
      forexfactory/

One subdirectory per source, so a single source can be cleared without touching
the others: a bad FRED response should not cost the week's calendar. Each entry
is two files, the raw body and a sidecar holding the fetch time, the request
that produced it and the source's own freshness stamp.

Storing the raw body rather than parsed observations is deliberate. When a
parser turns out to be wrong, and one will, the cache still holds what the
server actually sent, so the fix is a re-parse rather than a refetch of history
that may no longer be available.

Key derivation
--------------
The key is a SHA-256 of the source name, the endpoint path and every request
parameter, sorted, truncated to 16 hex characters. Three rules give it its
properties:

* The API key is excluded. It is a credential, not part of the request's
  identity, and rotating a key should not invalidate a cache.
* Parameters are sorted before hashing, so two callers that build the same
  request in a different order share one entry.
* The source name is included, so two sources cannot collide on a shared path.

Date ranges are part of the key, which means a widened window is a cache miss.
That is correct but wasteful for daily series, where callers should request a
stable window, say five years back to today, rather than a window that shifts
by a day each run and misses every time.

TTL
---
`DataConfig.cache_ttl_hours`, default 12. One TTL for every source is a
simplification with a real cost: a COT file is valid for a week and a VIX close
for a few hours, and 12 hours serves neither well. `SUGGESTED_TTL_HOURS` below
records what each source actually wants, so the simplification is at least
documented rather than accidental.

TTL is measured from the fetch time in the sidecar, not from file mtime, which a
backup or a checkout can change.

Expiry and offline interact in the one way that matters. Online, an expired
entry is refetched. Offline, an expired entry is still served, with its age
attached, because a stale cached number and a clear staleness figure on the
report is strictly better than a hole. What offline must never do is reach the
network, and what it must always do is tell the truth about how old the data is.
A missing entry offline is an error, not an empty result: silently returning
nothing would show up as a coverage gap and hide a misconfiguration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fbe.config import DataConfig

__all__ = [
    "CacheEntry",
    "CacheMiss",
    "DiskCache",
    "KEY_LENGTH",
    "SUGGESTED_TTL_HOURS",
]


KEY_LENGTH = 16
"""Hex characters kept from the SHA-256 digest. Sixteen is 64 bits, which for a
few thousand entries makes a collision a non-event, and keeps filenames short
enough to read in a directory listing."""

BODY_SUFFIX = ".json"
META_SUFFIX = ".meta.json"

SUGGESTED_TTL_HOURS: Mapping[str, int] = {
    "fred": 12,
    "cftc": 72,
    "stooq": 6,
    "forexfactory": 24,
    "manual": 0,
}
"""What each source's cadence actually justifies, against the single
`DataConfig.cache_ttl_hours` the config currently applies to all of them.

``cftc`` gets 72 hours because the report only changes on Friday afternoons, so
a Tuesday refetch can only return what is already on disk. ``stooq`` gets 6
because closes change daily and the source is unreliable enough that a fresh
attempt is worth making. ``forexfactory`` gets 24 because the publisher asks
callers to download once a week, and a day is already generous against that.
``manual`` gets 0 because it reads local files: caching a file read adds a way
for an operator's edit not to take effect, which is the last thing hand-entered
data needs."""


class CacheMiss(KeyError):
    """No usable entry exists for a key.

    Raised rather than returned so that an offline run cannot mistake a missing
    entry for an empty result. The two differ: one is a broken setup, the other
    is data.
    """


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached response and what is known about it.

    Attributes:
        key: The derived cache key.
        source: Source name, which is also the subdirectory.
        fetched_at: When the body was written. TTL is measured from here, not
            from file mtime, which a backup or a checkout can rewrite.
        body_path: Path to the raw response body.
        request: The request that produced it, credentials excluded. Kept so a
            cache entry can be traced back to a URL during debugging.
        source_last_updated: The source's own freshness stamp where it publishes
            one, such as FRED's ``last_updated``. Distinct from ``fetched_at``:
            a series fetched this morning may not have been updated since 2024,
            and that difference is exactly what the registry's discontinued
            series make dangerous.

    """

    key: str
    source: str
    fetched_at: datetime
    body_path: Path
    request: Mapping[str, str] = field(default_factory=dict)
    source_last_updated: datetime | None = None


class DiskCache:
    """File-backed cache shared by every network source.

    Attributes:
        root: Cache root, from `DataConfig.cache_dir`.
        ttl_hours: Default TTL, from `DataConfig.cache_ttl_hours`.
        offline: When true, never write and never expire; serve what is on disk
            and raise `CacheMiss` for anything absent.

    """

    def __init__(self, config: DataConfig) -> None:
        """Store the cache configuration.

        Args:
            config: Effective `DataConfig`.

        """
        self.root = config.cache_dir
        self.ttl_hours = config.cache_ttl_hours
        self.offline = config.offline

    def key_for(
        self,
        source: str,
        path: str,
        params: Mapping[str, str | int | float],
        exclude: frozenset[str] = frozenset({"api_key", "token", "app_token"}),
    ) -> str:
        """Derive the cache key for one request.

        Args:
            source: Source name.
            path: Endpoint path relative to the source's base URL.
            params: Query parameters.
            exclude: Parameter names to leave out of the digest. Credentials
                by default, so that rotating a key does not invalidate the
                cache and so that no key can leak into a filename.

        Returns:
            A `KEY_LENGTH`-character hex key.

        """
        raise NotImplementedError

    def get(self, source: str, key: str) -> CacheEntry:
        """Return a cached entry, honouring the TTL unless offline.

        Args:
            source: Source name.
            key: Key from `key_for`.

        Returns:
            The entry. Offline, an expired entry is returned rather than
            refused, because a stale number with an honest age beats a hole.

        Raises:
            CacheMiss: If no entry exists, or if it has expired and the run is
                online, in which case the caller should refetch.

        """
        raise NotImplementedError

    def put(
        self,
        source: str,
        key: str,
        body: bytes,
        request: Mapping[str, str],
        source_last_updated: datetime | None = None,
    ) -> CacheEntry:
        """Write a response body and its sidecar.

        The body is written to a temporary file and renamed into place, so an
        interrupted run leaves no half-written entry for the next one to read
        as valid.

        Args:
            source: Source name.
            key: Key from `key_for`.
            body: Raw response bytes, unparsed.
            request: The request that produced it, credentials excluded.
            source_last_updated: The source's own freshness stamp, where it
                publishes one.

        Returns:
            The written entry.

        Raises:
            RuntimeError: If called while ``offline`` is set. An offline run
                that writes to the cache is no longer reproducible, which
                defeats the only reason the flag exists.

        """
        raise NotImplementedError

    def age_hours(self, entry: CacheEntry) -> float:
        """Return an entry's age in hours since it was fetched.

        Args:
            entry: The entry to age.

        Returns:
            Hours since ``fetched_at``.

        """
        raise NotImplementedError

    def is_expired(self, entry: CacheEntry, ttl_hours: int | None = None) -> bool:
        """Say whether an entry has passed its TTL.

        Args:
            entry: The entry to test.
            ttl_hours: Override for the default TTL, so a source can apply its
                own figure from `SUGGESTED_TTL_HOURS`.

        Returns:
            True when the entry is older than the TTL. Always False offline,
            where expiry is meaningless because nothing can be refetched.

        """
        raise NotImplementedError

    def clear(self, source: str | None = None) -> int:
        """Delete cached entries.

        Args:
            source: Clear only this source's subdirectory. ``None`` clears
                everything.

        Returns:
            The number of entries removed.

        """
        raise NotImplementedError

    def stats(self) -> Mapping[str, Mapping[str, float]]:
        """Summarise what is on disk, per source.

        Entry counts, total bytes and the age of the oldest and newest entry.
        Worth putting on the report: a cache whose newest FRED entry is four
        days old explains a run that looks inexplicably stale.

        Returns:
            Source name to its statistics.

        """
        raise NotImplementedError
