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

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
TEMP_SUFFIX = ".partial"
"""Suffix for the temporary file a write lands in before it is renamed into
place. It is never a valid entry: `get` reads neither suffix, and a write that
raises removes its own temporary files. A process killed outright leaves one
behind, where it counts towards ``stats()`` bytes until the source is cleared."""

CREDENTIAL_PARAMS: frozenset[str] = frozenset({"api_key", "token", "app_token"})
"""Parameter names treated as credentials rather than as part of a request's
identity.

Excluded from the key, so rotating a credential does not invalidate the cache,
and stripped from the sidecar by `DiskCache.put`, so a credential cannot reach
a file under ``data/cache``. Both matter: the cache is documented as safe to
delete and is the first thing anyone attaches to a bug report, and a sidecar
keeps a rotated key readable for as long as the entry survives."""

SECONDS_PER_HOUR = 3600.0

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
        exclude: frozenset[str] = CREDENTIAL_PARAMS,
    ) -> str:
        """Derive the cache key for one request.

        Args:
            source: Source name.
            path: Endpoint path relative to the source's base URL.
            params: Query parameters.
            exclude: Parameter names to leave out of the digest. Credentials
                by default, so that rotating a key does not invalidate the
                cache and so that no key can leak into a filename. This
                argument governs the digest only. What reaches the sidecar is
                `put`'s business, and `put` strips `CREDENTIAL_PARAMS` itself
                rather than trusting the caller to have done it.

        Returns:
            A `KEY_LENGTH`-character hex key. Two callers that pass the same
            parameters in a different order get the same key, and changing any
            included parameter value changes it.

        """
        material = {
            name: str(value) for name, value in params.items() if name not in exclude
        }
        payload = json.dumps(
            {"source": source, "path": path, "params": material}, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:KEY_LENGTH]

    def _paths(self, source: str, key: str) -> tuple[Path, Path]:
        """Return the body and sidecar paths for one entry.

        Args:
            source: Source name, which is also the subdirectory.
            key: Key from `key_for`.

        Returns:
            The body path and the sidecar path. Neither is guaranteed to exist.

        """
        directory = self.root / source
        return directory / f"{key}{BODY_SUFFIX}", directory / f"{key}{META_SUFFIX}"

    def _body_paths(self, directory: Path) -> list[Path]:
        """Return the body files in one source directory, sidecars excluded.

        Both files end in ``.json``, so a plain glob counts each entry twice.
        Counting entries as bodies is what makes `clear` and `stats` report the
        number of responses held rather than the number of files.

        Args:
            directory: One source's subdirectory.

        Returns:
            Body paths, sorted. Empty when the directory does not exist.

        """
        if not directory.is_dir():
            return []
        return sorted(
            path
            for path in directory.glob(f"*{BODY_SUFFIX}")
            if not path.name.endswith(META_SUFFIX)
        )

    def _load(self, source: str, key: str) -> CacheEntry:
        """Read one entry from disk without applying the TTL.

        Args:
            source: Source name.
            key: Key from `key_for`.

        Returns:
            The entry as the sidecar describes it.

        Raises:
            CacheMiss: If either file is absent, if the sidecar cannot be
                parsed or is missing any field `put` writes, if it describes a
                different entry, if either timestamp has no timezone, if the
                fetch time is in the future, or if the body does not match the
                digest the sidecar records. Every one of those is an entry that
                cannot be trusted, and saying so lets an online caller refetch
                and heal it rather than score against it.

        """
        body_path, meta_path = self._paths(source, key)
        if not body_path.is_file() or not meta_path.is_file():
            raise CacheMiss(f"no cache entry for {source}/{key}")

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
            stamp = meta["source_last_updated"]
            source_last_updated = (
                datetime.fromisoformat(stamp) if stamp is not None else None
            )
            request = {str(name): str(value) for name, value in meta["request"].items()}
            recorded = (str(meta["source"]), str(meta["key"]))
            body_digest = str(meta["body_sha256"])
        except (OSError, AttributeError, ValueError, TypeError, KeyError) as error:
            raise CacheMiss(
                f"unreadable sidecar for {source}/{key}: {error}"
            ) from error

        if recorded != (source, key):
            raise CacheMiss(
                f"sidecar in {source}/{key} describes {recorded[0]}/{recorded[1]}, "
                "so the pair was moved or restored wrongly"
            )

        # A naive stamp is a time of unknown offset, and both of these feed a
        # staleness figure that reaches the report.
        for label, moment in (
            ("fetch time", fetched_at),
            ("source freshness stamp", source_last_updated),
        ):
            if moment is not None and moment.tzinfo is None:
                raise CacheMiss(
                    f"sidecar for {source}/{key} has a {label} with no timezone"
                )

        if fetched_at > datetime.now(UTC):
            raise CacheMiss(
                f"sidecar for {source}/{key} is stamped in the future, so the "
                "clock moved and the entry's age cannot be trusted"
            )

        # The two renames in `put` are not atomic together, and a backup or a
        # partial restore can pair a sidecar with a body it does not describe.
        # Unchecked, that entry is served with the wrong age attached, which is
        # a plausible number rather than a failure.
        if hashlib.sha256(body_path.read_bytes()).hexdigest() != body_digest:
            raise CacheMiss(
                f"body of {source}/{key} does not match the digest its sidecar "
                "records, so the two are from different fetches"
            )

        return CacheEntry(
            key=key,
            source=source,
            fetched_at=fetched_at,
            body_path=body_path,
            request=request,
            source_last_updated=source_last_updated,
        )

    def _write_temp(self, directory: Path, payload: bytes) -> Path:
        """Write ``payload`` to a temporary file in ``directory``.

        The temporary file is created in the destination directory so the
        rename that follows stays on one filesystem, which is what makes it
        atomic.

        Args:
            directory: The source subdirectory the entry belongs to.
            payload: Bytes to write.

        Returns:
            Path to the written temporary file. The caller owns it and must
            either rename it into place or remove it.

        """
        handle, name = tempfile.mkstemp(dir=directory, suffix=TEMP_SUFFIX)
        temp_path = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    def get(self, source: str, key: str) -> CacheEntry:
        """Return a cached entry, honouring the TTL unless offline.

        Args:
            source: Source name.
            key: Key from `key_for`.

        Returns:
            The entry. Offline, an expired entry is returned rather than
            refused, because a stale number with an honest age beats a hole.

        Raises:
            CacheMiss: If no entry exists, if its sidecar cannot be read, or if
                it has expired and the run is online, in which case the caller
                should refetch. An empty body is not a miss: a zero-byte
                response is data, and the caller decides what it means.

        """
        entry = self._load(source, key)
        if not self.offline and self.is_expired(entry):
            raise CacheMiss(
                f"cache entry for {source}/{key} is "
                f"{self.age_hours(entry):.1f} hours old, past the "
                f"{self.ttl_hours} hour TTL"
            )
        return entry

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
            request: The request that produced it. Every name in
                `CREDENTIAL_PARAMS` is stripped here rather than trusted to
                have been stripped by the caller, since a source builds one
                parameter dict and passes it to both `key_for` and this. A
                credential under any other name is still the caller's to
                remove.
            source_last_updated: The source's own freshness stamp, where it
                publishes one, as a timezone-aware datetime. ``None`` where the
                source publishes none, which is a different fact from a source
                that publishes an old one.

        Returns:
            The written entry, with ``request`` as it was stored, credentials
            already removed.

        Raises:
            RuntimeError: If called while ``offline`` is set. An offline run
                that writes to the cache is no longer reproducible, which
                defeats the only reason the flag exists.
            ValueError: If ``source_last_updated`` has no timezone. It feeds a
                staleness figure, and a time of unknown offset cannot.
            OSError: If the write fails. Nothing readable is left behind, which
                includes the entry this call was replacing: a failed refresh of
                an existing key clears it rather than leaving a body and a
                sidecar that describe different fetches.

        """
        if self.offline:
            raise RuntimeError(
                f"refusing to cache {source}/{key} while offline: a run that "
                "writes its own inputs is no longer reproducible"
            )

        if source_last_updated is not None and source_last_updated.tzinfo is None:
            raise ValueError(
                f"source_last_updated for {source}/{key} has no timezone, so "
                "its offset is unknown and it cannot age anything"
            )

        stored_request = {
            str(name): str(value)
            for name, value in request.items()
            if name not in CREDENTIAL_PARAMS
        }

        body_path, meta_path = self._paths(source, key)
        directory = body_path.parent
        directory.mkdir(parents=True, exist_ok=True)

        fetched_at = datetime.now(UTC)
        sidecar = json.dumps(
            {
                "key": key,
                "source": source,
                "fetched_at": fetched_at.isoformat(),
                "request": stored_request,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "source_last_updated": (
                    source_last_updated.isoformat()
                    if source_last_updated is not None
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

        # Both temporary files are written before either is renamed, so a
        # failure while serialising or writing leaves the entry untouched. The
        # body is renamed first and the sidecar second, which makes the sidecar
        # the marker of validity: an entry without one is never read.
        body_temp: Path | None = None
        meta_temp: Path | None = None
        body_moved = False
        try:
            body_temp = self._write_temp(directory, body)
            meta_temp = self._write_temp(directory, sidecar)
            os.replace(body_temp, body_path)
            body_moved = True
            os.replace(meta_temp, meta_path)
        except BaseException:
            for leftover in (body_temp, meta_temp):
                if leftover is not None:
                    leftover.unlink(missing_ok=True)
            if body_moved:
                # The new body is already in place and its sidecar never
                # landed, so what remains is either a lone body or the previous
                # entry's sidecar describing bytes that are no longer there.
                # Both are unreadable, and the digest check in `_load` would
                # refuse the second anyway. Removing both leaves the key
                # genuinely empty rather than half-written, which is the
                # honest state: the refresh did not happen.
                body_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            raise

        return CacheEntry(
            key=key,
            source=source,
            fetched_at=fetched_at,
            body_path=body_path,
            request=stored_request,
            source_last_updated=source_last_updated,
        )

    def age_hours(self, entry: CacheEntry) -> float:
        """Return an entry's age in hours since it was fetched.

        Args:
            entry: The entry to age.

        Returns:
            Hours since ``fetched_at``, from the sidecar rather than file
            mtime, which a backup or a checkout can rewrite. Positive for an
            entry fetched in the past. Fractional, not rounded: this figure
            reaches the report as a staleness number and rounding it would
            make a twelve hour old print read as fresh. Negative only for an
            entry stamped in the future, which `get` refuses to return.

        """
        elapsed = datetime.now(UTC) - entry.fetched_at
        return elapsed.total_seconds() / SECONDS_PER_HOUR

    def is_expired(self, entry: CacheEntry, ttl_hours: int | None = None) -> bool:
        """Say whether an entry has passed its TTL.

        Args:
            entry: The entry to test.
            ttl_hours: Override for the default TTL, so a source can apply its
                own figure from `SUGGESTED_TTL_HOURS`.

        Returns:
            True when the entry is older than the TTL. Always False offline,
            where expiry is meaningless because nothing can be refetched.
            A TTL of zero, which is what ``manual`` wants, expires everything
            online, so a local file is re-read rather than served from a copy.

        """
        if self.offline:
            return False
        ttl = self.ttl_hours if ttl_hours is None else ttl_hours
        return self.age_hours(entry) > ttl

    def clear(self, source: str | None = None) -> int:
        """Delete cached entries.

        Args:
            source: Clear only this source's subdirectory. ``None`` clears
                everything.

        Returns:
            The number of entries removed, counting one per cached response
            rather than one per file. Zero for a source that was never
            fetched, which is not an error: clearing what is not there is the
            state the caller asked for.

        """
        if source is not None:
            return self._clear_source(source)
        if not self.root.is_dir():
            return 0
        return sum(
            self._clear_source(directory.name)
            for directory in sorted(self.root.iterdir())
            if directory.is_dir()
        )

    def _clear_source(self, source: str) -> int:
        """Remove one source's subdirectory and report what it held.

        Args:
            source: Source name. It must be a single path segment, so that a
                name arriving from a command line cannot resolve back to the
                cache root or out of it.

        Returns:
            The number of entries removed. Zero when the directory is absent,
            which is not an error: clearing what is not there leaves the caller
            in the state it asked for.

        Raises:
            ValueError: If ``source`` is empty, a dot segment, or contains a
                path separator. ``clear("")`` would otherwise resolve to the
                root and delete every source while reporting nothing removed.

        """
        if source in {"", ".", ".."} or "/" in source or os.sep in source:
            raise ValueError(
                f"cache source must be a single path segment, got {source!r}"
            )
        directory = self.root / source
        if not directory.is_dir():
            return 0
        removed = len(self._body_paths(directory))
        shutil.rmtree(directory)
        return removed

    def stats(self) -> Mapping[str, Mapping[str, float]]:
        """Summarise what is on disk, per source.

        Entry counts, total bytes and the age of the oldest and newest entry.
        Worth putting on the report: a cache whose newest FRED entry is four
        days old explains a run that looks inexplicably stale.

        Returns:
            Source name to its statistics. Each holds ``entries``, the number
            of readable responses; ``bytes``, the size on disk of everything in
            the subdirectory, bodies and sidecars together; ``unreadable``, the
            number of broken entries, counting both a body whose sidecar is
            absent or will not parse and a sidecar left with no body; and
            ``oldest_hours`` and ``newest_hours``, ages from the sidecars in
            hours. The two ages are **omitted** when nothing readable is left,
            rather than reported as zero, which would read as just fetched.
            A source directory holding no files at all does not appear, since
            an empty directory is what a failed first write leaves behind and
            reporting it as a source with zero entries would make an absent
            source and a broken one look the same.

        """
        if not self.root.is_dir():
            return {}

        summary: dict[str, dict[str, float]] = {}
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue

            files = [path for path in directory.iterdir() if path.is_file()]
            if not files:
                continue

            ages: list[float] = []
            unreadable = 0
            bodies = self._body_paths(directory)
            for body_path in bodies:
                key = body_path.name[: -len(BODY_SUFFIX)]
                try:
                    entry = self._load(directory.name, key)
                except CacheMiss:
                    unreadable += 1
                    continue
                ages.append(self.age_hours(entry))

            # A sidecar whose body is gone is counted from the other side, so
            # that a half-removed entry appears somewhere rather than nowhere.
            keys = {path.name[: -len(BODY_SUFFIX)] for path in bodies}
            unreadable += sum(
                1
                for path in directory.glob(f"*{META_SUFFIX}")
                if path.name[: -len(META_SUFFIX)] not in keys
            )

            counts: dict[str, float] = {
                "entries": float(len(ages)),
                "bytes": float(sum(path.stat().st_size for path in files)),
                "unreadable": float(unreadable),
            }
            if ages:
                counts["oldest_hours"] = max(ages)
                counts["newest_hours"] = min(ages)
            summary[directory.name] = counts

        return summary
