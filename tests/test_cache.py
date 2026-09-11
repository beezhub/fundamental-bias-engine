"""Tests for the on-disk response cache.

The cache is what makes a run reproducible, so the properties worth testing are
the ones a silent failure would take away: that a key is stable across callers
and never carries a credential, that a stale entry is refused online and served
offline with an honest age, and that an interrupted write leaves nothing the
next run would read as valid.

Every test works under ``tmp_path`` and none reaches the network. Nothing here
uses ``httpx``, so there is nothing to mock: the cache only touches the
filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fbe.config import DataConfig
from fbe.datasources.cache import (
    KEY_LENGTH,
    SUGGESTED_TTL_HOURS,
    CacheEntry,
    CacheMiss,
    DiskCache,
)

BODY = b'{"observations": [{"date": "2026-06-30", "value": "2.9"}]}'
REQUEST = {"series_id": "CPIAUCSL", "observation_start": "2021-06-30"}
CREDENTIAL = "abcdef0123456789abcdef0123456789"


@pytest.fixture
def cache_config(tmp_path: Path) -> DataConfig:
    """A DataConfig rooted at tmp_path, online, with the default 12 hour TTL."""
    return DataConfig(cache_dir=tmp_path / "cache")


@pytest.fixture
def cache(cache_config: DataConfig) -> DiskCache:
    return DiskCache(cache_config)


@pytest.fixture
def offline_cache(cache_config: DataConfig) -> DiskCache:
    return DiskCache(replace(cache_config, offline=True))


def _age_entry(cache: DiskCache, source: str, key: str, hours: float) -> None:
    """Rewrite an entry's sidecar so it reads as fetched ``hours`` ago.

    Edits the sidecar rather than the file mtime on purpose: the TTL is defined
    to follow the sidecar, and a helper that moved the mtime would pass against
    an implementation that read the wrong one.
    """
    meta_path = cache.root / source / f"{key}.meta.json"
    meta = json.loads(meta_path.read_text())
    fetched_at = datetime.now(UTC) - timedelta(hours=hours)
    meta["fetched_at"] = fetched_at.isoformat()
    meta_path.write_text(json.dumps(meta))


# --- key derivation ---------------------------------------------------------


def test_key_is_hex_of_the_documented_length(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)

    # Pinned against the 16 published in docs/data-sources.md rather than
    # against KEY_LENGTH alone, which would follow the constant anywhere it
    # moved and agree with it.
    assert KEY_LENGTH == 16
    assert len(key) == KEY_LENGTH
    assert all(character in "0123456789abcdef" for character in key)


def test_the_key_derivation_is_pinned(cache: DiskCache) -> None:
    """The serialisation is a wire format, not an implementation detail.

    Every entry on disk is named by it, so changing how the digest is built
    orphans the whole cache at once and every offline run silently misses.
    Recomputed here from the rule in docs/data-sources.md: SHA-256 over the
    source, the path and the parameters sorted, first KEY_LENGTH hex chars.
    """
    expected = hashlib.sha256(
        json.dumps(
            {
                "source": "fred",
                "path": "series/observations",
                "params": {
                    "observation_start": "2021-06-30",
                    "series_id": "CPIAUCSL",
                },
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:KEY_LENGTH]

    assert expected == "29662973e5acda3e"
    assert cache.key_for("fred", "series/observations", REQUEST) == expected


def test_suggested_ttl_hours_matches_the_published_table() -> None:
    """docs/data-sources.md publishes these figures, so they are a fixture."""
    assert dict(SUGGESTED_TTL_HOURS) == {
        "fred": 12,
        "cftc": 72,
        "stooq": 6,
        "forexfactory": 24,
        "manual": 0,
    }


def test_parameter_order_does_not_change_the_key(cache: DiskCache) -> None:
    forward = cache.key_for("fred", "series/observations", REQUEST)
    reversed_params = dict(reversed(list(REQUEST.items())))
    backward = cache.key_for("fred", "series/observations", reversed_params)

    assert forward == backward


def test_changing_a_parameter_value_changes_the_key(cache: DiskCache) -> None:
    base = cache.key_for("fred", "series/observations", REQUEST)
    moved = cache.key_for(
        "fred", "series/observations", {**REQUEST, "observation_start": "2020-06-30"}
    )

    assert base != moved


def test_changing_the_path_changes_the_key(cache: DiskCache) -> None:
    base = cache.key_for("fred", "series/observations", REQUEST)
    other = cache.key_for("fred", "series/search", REQUEST)

    assert base != other


def test_the_source_name_is_part_of_the_key(cache: DiskCache) -> None:
    """Two sources sharing a path must not collide on one entry."""
    fred = cache.key_for("fred", "series/observations", REQUEST)
    oecd = cache.key_for("oecd", "series/observations", REQUEST)

    assert fred != oecd


def test_a_credential_does_not_change_the_key(cache: DiskCache) -> None:
    """Rotating an API key must not invalidate every entry the source holds."""
    without = cache.key_for("fred", "series/observations", REQUEST)
    with_key = cache.key_for(
        "fred", "series/observations", {**REQUEST, "api_key": CREDENTIAL}
    )
    rotated = cache.key_for(
        "fred", "series/observations", {**REQUEST, "api_key": "0" * 32}
    )

    assert without == with_key == rotated


def test_every_excluded_parameter_name_is_honoured(cache: DiskCache) -> None:
    base = cache.key_for("fred", "series/observations", REQUEST)

    for name in ("api_key", "token", "app_token"):
        assert cache.key_for("fred", "series/observations", {**REQUEST, name: "x"}) == (
            base
        )


def test_exclude_is_overridable(cache: DiskCache) -> None:
    """A source with a differently named credential can say so."""
    base = cache.key_for("fred", "series/observations", REQUEST)
    excluded = cache.key_for(
        "fred",
        "series/observations",
        {**REQUEST, "subscription": CREDENTIAL},
        exclude=frozenset({"subscription"}),
    )

    assert base == excluded


def test_numeric_parameters_are_accepted(cache: DiskCache) -> None:
    key = cache.key_for("stooq", "d/l", {"limit": 500, "factor": 1.5})
    same = cache.key_for("stooq", "d/l", {"factor": 1.5, "limit": 500})

    assert key == same


# --- writing and reading ----------------------------------------------------


def test_put_then_get_round_trips_the_body_and_the_request(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    written = cache.put("fred", key, BODY, REQUEST)
    read = cache.get("fred", key)

    assert read.key == key
    assert read.source == "fred"
    assert read.body_path.read_bytes() == BODY
    assert dict(read.request) == REQUEST
    assert read.fetched_at == written.fetched_at


def test_entries_land_in_a_subdirectory_per_source(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    entry = cache.put("fred", key, BODY, REQUEST)

    assert entry.body_path == cache.root / "fred" / f"{key}.json"
    assert (cache.root / "fred" / f"{key}.meta.json").exists()


@pytest.mark.parametrize("credential", ["api_key", "token", "app_token"])
def test_no_credential_reaches_a_filename_or_a_sidecar(
    cache: DiskCache, credential: str
) -> None:
    """Pass the credential to put, which is what a source actually does.

    A source builds one parameter dict and hands it to both `key_for` and
    `put`. A test that strips the credential before calling `put` is testing
    its own tidiness, not the cache, and would pass against a cache that wrote
    the key into the sidecar in cleartext.
    """
    request = {**REQUEST, credential: CREDENTIAL}
    key = cache.key_for("fred", "series/observations", request)
    cache.put("fred", key, BODY, request)

    written = [path for path in cache.root.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert CREDENTIAL not in path.name
        assert CREDENTIAL.encode() not in path.read_bytes()

    assert credential not in cache.get("fred", key).request


def test_put_refuses_to_write_offline(offline_cache: DiskCache) -> None:
    """An offline run that writes is no longer reproducible."""
    key = offline_cache.key_for("fred", "series/observations", REQUEST)

    with pytest.raises(RuntimeError):
        offline_cache.put("fred", key, BODY, REQUEST)

    assert not (offline_cache.root / "fred").exists()


def test_get_raises_cache_miss_when_nothing_was_written(cache: DiskCache) -> None:
    with pytest.raises(CacheMiss):
        cache.get("fred", "0" * KEY_LENGTH)


def test_get_raises_cache_miss_when_the_sidecar_is_absent(cache: DiskCache) -> None:
    """A body with no sidecar is the shape an interrupted write would leave."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    (cache.root / "fred" / f"{key}.meta.json").unlink()

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_get_raises_cache_miss_when_the_sidecar_is_unreadable(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    (cache.root / "fred" / f"{key}.meta.json").write_text("{ truncated")

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_an_offline_miss_raises_rather_than_returning_nothing(
    cache: DiskCache, offline_cache: DiskCache
) -> None:
    """The distinction the module exists to preserve: a missing entry is a
    broken setup, an empty body is data."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, b"", REQUEST)

    assert offline_cache.get("fred", key).body_path.read_bytes() == b""

    with pytest.raises(CacheMiss):
        offline_cache.get("fred", "1" * KEY_LENGTH)


# --- expiry -----------------------------------------------------------------


def test_get_refuses_an_expired_entry_online(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=cache.ttl_hours + 1)

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_get_serves_an_expired_entry_offline(
    cache: DiskCache, offline_cache: DiskCache
) -> None:
    """Offline, a stale number with an honest age beats a hole."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=500)

    entry = offline_cache.get("fred", key)

    assert entry.body_path.read_bytes() == BODY
    assert offline_cache.age_hours(entry) == pytest.approx(500, abs=0.1)


def test_get_serves_a_fresh_entry_online(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=cache.ttl_hours - 1)

    assert cache.get("fred", key).body_path.read_bytes() == BODY


def test_age_hours_follows_the_sidecar_not_the_file_mtime(cache: DiskCache) -> None:
    """A backup or a checkout rewrites mtime. It must not age the cache."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    entry = cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=8)

    moved = (datetime.now(UTC) - timedelta(hours=1000)).timestamp()
    os.utime(entry.body_path, (moved, moved))

    # Eight hours is inside the 12 hour TTL and 1000 is far outside it, so an
    # implementation reading mtime would both misreport the age and refuse the
    # entry as expired.
    assert cache.age_hours(cache.get("fred", key)) == pytest.approx(8, abs=0.1)


def test_is_expired_is_always_false_offline(offline_cache: DiskCache) -> None:
    """Expiry is meaningless when nothing can be refetched."""
    entry = CacheEntry(
        key="0" * KEY_LENGTH,
        source="cftc",
        fetched_at=datetime.now(UTC) - timedelta(days=400),
        body_path=offline_cache.root / "cftc" / "x.json",
    )

    assert offline_cache.is_expired(entry) is False
    assert offline_cache.is_expired(entry, ttl_hours=1) is False


def test_is_expired_honours_the_ttl_override(cache: DiskCache) -> None:
    """A source applies its own SUGGESTED_TTL_HOURS against one shared default."""
    entry = CacheEntry(
        key="0" * KEY_LENGTH,
        source="cftc",
        fetched_at=datetime.now(UTC) - timedelta(hours=24),
        body_path=cache.root / "cftc" / "x.json",
    )

    assert cache.is_expired(entry) is True
    assert cache.is_expired(entry, ttl_hours=SUGGESTED_TTL_HOURS["cftc"]) is False


def test_a_ttl_override_of_zero_expires_everything(cache: DiskCache) -> None:
    """Zero is the figure `manual` carries, and it is falsy.

    An implementation writing ``ttl_hours or self.ttl_hours`` passes every
    other test in this file and silently serves a hand-edited CSV from a 12
    hour old copy, which is the one thing that TTL exists to prevent.
    """
    entry = CacheEntry(
        key="0" * KEY_LENGTH,
        source="manual",
        fetched_at=datetime.now(UTC) - timedelta(minutes=1),
        body_path=cache.root / "manual" / "x.json",
    )

    assert SUGGESTED_TTL_HOURS["manual"] == 0
    assert cache.is_expired(entry, ttl_hours=0) is True


def test_the_ttl_boundary_falls_between_these_two_seconds(cache: DiskCache) -> None:
    """Pin the boundary to seconds rather than to the nearest hour.

    Exact equality with the TTL is not constructible: time passes between
    building the entry and comparing it, so an entry stamped exactly ten hours
    ago is already a few microseconds past ten hours by the time it is tested.
    Two seconds either side is as close as this can honestly get, and it is
    tight enough to catch a comparison against the wrong quantity.
    """

    def entry(seconds: int) -> CacheEntry:
        return CacheEntry(
            key="0" * KEY_LENGTH,
            source="fred",
            fetched_at=datetime.now(UTC) - timedelta(hours=10, seconds=seconds),
            body_path=cache.root / "fred" / "x.json",
        )

    assert cache.is_expired(entry(-2), ttl_hours=10) is False
    assert cache.is_expired(entry(2), ttl_hours=10) is True


def test_age_hours_is_fractional_not_rounded(cache: DiskCache) -> None:
    """The report prints this as a staleness figure. Rounding a 7.5 hour old
    print to 7 or 8 changes what a reader concludes about it."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=7.5)

    assert cache.age_hours(cache.get("fred", key)) == pytest.approx(7.5, abs=0.01)


def test_is_expired_uses_the_configured_default_not_a_literal(
    cache_config: DataConfig,
) -> None:
    """Override the config and the answer must move with it."""
    entry = CacheEntry(
        key="0" * KEY_LENGTH,
        source="fred",
        fetched_at=datetime.now(UTC) - timedelta(hours=20),
        body_path=cache_config.cache_dir / "fred" / "x.json",
    )

    assert DiskCache(cache_config).is_expired(entry) is True
    assert DiskCache(replace(cache_config, cache_ttl_hours=36)).is_expired(entry) is (
        False
    )


# --- freshness stamp --------------------------------------------------------


def test_source_last_updated_round_trips_and_is_separate_from_fetched_at(
    cache: DiskCache,
) -> None:
    """A series fetched this morning may not have been updated for a year."""
    last_updated = datetime(2024, 3, 14, 9, 30, tzinfo=UTC)
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST, source_last_updated=last_updated)

    entry = cache.get("fred", key)

    assert entry.source_last_updated == last_updated
    assert entry.fetched_at != last_updated
    assert cache.age_hours(entry) < 1


def test_source_last_updated_is_none_when_the_source_publishes_none(
    cache: DiskCache,
) -> None:
    """Absent is its own answer, not a stand-in date."""
    key = cache.key_for("stooq", "d/l", {"s": "eurusd"})
    cache.put("stooq", key, BODY, {"s": "eurusd"})

    assert cache.get("stooq", key).source_last_updated is None


# --- sidecars that cannot be trusted ----------------------------------------


def _corrupt(
    cache: DiskCache, source: str, entry_key: str, fields: dict[str, object]
) -> None:
    """Overwrite named sidecar fields, leaving the rest intact."""
    meta_path = cache.root / source / f"{entry_key}.meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update(fields)
    meta_path.write_text(json.dumps(meta))


def test_a_naive_fetch_time_is_refused(cache: DiskCache) -> None:
    """A time with no offset cannot age anything."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _corrupt(
        cache, "fred", key, {"fetched_at": datetime(2026, 6, 30, 12, 0).isoformat()}
    )

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_a_naive_source_stamp_is_refused_on_write(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)

    with pytest.raises(ValueError, match="no timezone"):
        cache.put(
            "fred",
            key,
            BODY,
            REQUEST,
            source_last_updated=datetime(2024, 3, 14, 9, 30),
        )


def test_a_fetch_time_in_the_future_is_refused(cache: DiskCache) -> None:
    """A clock that was wrong during the fetch would otherwise make an entry
    that never expires and reports a negative age."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _age_entry(cache, "fred", key, hours=-720)

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_a_body_that_does_not_match_its_sidecar_is_refused(cache: DiskCache) -> None:
    """The failure this prevents is the dangerous one: old bytes served with a
    fresh age, which is a plausible number rather than an error."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    entry = cache.put("fred", key, BODY, REQUEST)
    entry.body_path.write_bytes(b'{"observations": [{"value": "9.9"}]}')

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_a_pair_restored_under_the_wrong_name_is_refused(cache: DiskCache) -> None:
    """A sidecar records which entry it describes, so a misplaced restore is
    caught rather than served as the entry it was filed under."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    _corrupt(cache, "fred", key, {"key": "0" * KEY_LENGTH})

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_a_sidecar_missing_a_field_is_refused_not_defaulted(cache: DiskCache) -> None:
    """Absent must not read as "this source publishes no freshness stamp"."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    meta_path = cache.root / "fred" / f"{key}.meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["source_last_updated"]
    meta_path.write_text(json.dumps(meta))

    with pytest.raises(CacheMiss):
        cache.get("fred", key)


# --- interrupted writes -----------------------------------------------------


def test_the_temporary_file_is_written_beside_its_destination(
    cache: DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename is only atomic within one filesystem. A temp file in the
    system temp directory passes every other test here, because tmp_path and
    /tmp are the same mount in CI, and breaks wherever data/ is its own."""
    seen: list[str | None] = []
    real_mkstemp = tempfile.mkstemp

    def spy(**kwargs: object) -> tuple[int, str]:
        seen.append(str(kwargs.get("dir")))
        return real_mkstemp(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tempfile, "mkstemp", spy)

    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)

    assert seen == [str(cache.root / "fred")] * 2


def test_an_interrupted_write_leaves_no_entry_behind(
    cache: DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the rename that moves the sidecar into place, which is the last
    step and so the worst case: the body is already there."""
    real_replace = os.replace
    calls: list[int] = []

    def failing_replace(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("interrupted")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("fbe.datasources.cache.os.replace", failing_replace)

    key = cache.key_for("fred", "series/observations", REQUEST)
    with pytest.raises(OSError, match="interrupted"):
        cache.put("fred", key, BODY, REQUEST)

    assert list((cache.root / "fred").iterdir()) == []
    with pytest.raises(CacheMiss):
        cache.get("fred", key)


def test_an_interrupted_body_write_leaves_no_entry_behind(
    cache: DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(src: object, dst: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr("fbe.datasources.cache.os.replace", failing_replace)

    key = cache.key_for("fred", "series/observations", REQUEST)
    with pytest.raises(OSError, match="interrupted"):
        cache.put("fred", key, BODY, REQUEST)

    assert list((cache.root / "fred").iterdir()) == []


def test_an_interrupted_refresh_leaves_no_mismatched_pair(
    cache: DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupt a write that is replacing an entry, not creating one.

    The dangerous leftover here is the previous sidecar beside the new body:
    old age, new bytes. Whatever else this costs, it must never be readable.
    """
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)

    real_replace = os.replace
    calls: list[int] = []

    def failing_replace(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("no space left on device")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="no space"):
        cache.put("fred", key, b'{"observations": [{"value": "9.9"}]}', REQUEST)

    monkeypatch.setattr(os, "replace", real_replace)
    with pytest.raises(CacheMiss):
        cache.get("fred", key)
    assert list((cache.root / "fred").iterdir()) == []


def test_a_rewrite_of_the_same_key_replaces_the_entry(cache: DiskCache) -> None:
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    cache.put("fred", key, b"replaced", REQUEST)

    assert cache.get("fred", key).body_path.read_bytes() == b"replaced"
    assert len(list((cache.root / "fred").glob("*.json"))) == 2


# --- clearing ---------------------------------------------------------------


def _populate(cache: DiskCache) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {"fred": [], "cftc": []}
    for index in range(3):
        key = cache.key_for("fred", "series/observations", {"series_id": f"S{index}"})
        cache.put("fred", key, BODY, {"series_id": f"S{index}"})
        keys["fred"].append(key)
    key = cache.key_for("cftc", "dea/all", {"year": 2026})
    cache.put("cftc", key, BODY, {"year": "2026"})
    keys["cftc"].append(key)
    return keys


def test_clear_removes_only_the_named_source(cache: DiskCache) -> None:
    """A bad FRED response must not cost the week's calendar."""
    keys = _populate(cache)

    removed = cache.clear("fred")

    assert removed == 3
    assert not (cache.root / "fred").exists()
    assert cache.get("cftc", keys["cftc"][0]).body_path.read_bytes() == BODY


def test_clear_without_a_source_removes_everything(cache: DiskCache) -> None:
    _populate(cache)

    assert cache.clear() == 4
    assert list(cache.root.iterdir()) == []


def test_clear_counts_entries_not_files(cache: DiskCache) -> None:
    """Each entry is two files. Reporting four for two entries would read as a
    cache twice the size it is."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)

    assert len(list((cache.root / "fred").iterdir())) == 2
    assert cache.clear("fred") == 1


def test_clear_on_an_absent_source_is_not_an_error(cache: DiskCache) -> None:
    assert cache.clear("never-fetched") == 0


@pytest.mark.parametrize("source", ["", ".", "..", "../manual", "fred/sub"])
def test_clear_refuses_a_source_that_is_not_one_path_segment(
    cache: DiskCache, source: str
) -> None:
    """`clear("")` resolves to the root. Left unguarded it deletes every source
    and reports nothing removed, which is the worst possible pair."""
    _populate(cache)

    with pytest.raises(ValueError, match="single path segment"):
        cache.clear(source)

    assert (cache.root / "fred").is_dir()


# --- stats ------------------------------------------------------------------


def test_stats_reports_count_bytes_and_ages_per_source(cache: DiskCache) -> None:
    keys = _populate(cache)
    _age_entry(cache, "fred", keys["fred"][0], hours=30)
    _age_entry(cache, "fred", keys["fred"][1], hours=2)

    stats = cache.stats()

    assert stats["fred"]["entries"] == 3
    assert stats["cftc"]["entries"] == 1
    assert stats["fred"]["oldest_hours"] == pytest.approx(30, abs=0.1)
    assert stats["fred"]["newest_hours"] == pytest.approx(0, abs=0.1)

    # Stated independently of the implementation's own expression: the three
    # bodies plus the three sidecars, summed by name.
    bodies = sum(
        (cache.root / "fred" / f"{key}.json").stat().st_size for key in keys["fred"]
    )
    sidecars = sum(
        (cache.root / "fred" / f"{key}.meta.json").stat().st_size
        for key in keys["fred"]
    )
    assert bodies == len(BODY) * 3
    assert stats["fred"]["bytes"] == bodies + sidecars


def test_stats_is_empty_for_an_empty_cache(cache: DiskCache) -> None:
    assert dict(cache.stats()) == {}


def test_stats_omits_a_source_directory_holding_nothing(cache: DiskCache) -> None:
    """A failed first write leaves the directory behind. Reporting it as a
    source with zero entries makes an absent source and a broken one look the
    same on the report."""
    _populate(cache)
    (cache.root / "stooq").mkdir()

    assert "stooq" not in cache.stats()
    assert "fred" in cache.stats()


def test_stats_counts_a_sidecar_left_without_a_body(cache: DiskCache) -> None:
    """Half an entry is a broken entry, and counting only the other half would
    leave its bytes in the total with nothing to explain them."""
    keys = _populate(cache)
    (cache.root / "fred" / f"{keys['fred'][0]}.json").unlink()

    stats = cache.stats()

    assert stats["fred"]["entries"] == 2
    assert stats["fred"]["unreadable"] == 1


def test_stats_reports_unreadable_entries_rather_than_dropping_them(
    cache: DiskCache,
) -> None:
    """A body whose sidecar will not parse is a broken entry, and a summary
    that silently omits it hides the breakage."""
    keys = _populate(cache)
    (cache.root / "fred" / f"{keys['fred'][0]}.meta.json").write_text("{ truncated")

    stats = cache.stats()

    assert stats["fred"]["entries"] == 2
    assert stats["fred"]["unreadable"] == 1


def test_stats_omits_ages_when_no_entry_can_be_read(cache: DiskCache) -> None:
    """Zero would read as just fetched, which is the opposite of the truth."""
    key = cache.key_for("fred", "series/observations", REQUEST)
    cache.put("fred", key, BODY, REQUEST)
    (cache.root / "fred" / f"{key}.meta.json").write_text("{ truncated")

    stats = cache.stats()

    assert stats["fred"]["entries"] == 0
    assert stats["fred"]["unreadable"] == 1
    assert "oldest_hours" not in stats["fred"]
    assert "newest_hours" not in stats["fred"]
