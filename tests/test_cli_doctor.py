"""Tests for ``fbe doctor``.

``doctor`` is the command an operator runs when a report looks wrong, so the
properties worth testing are the ones that would send them chasing the model
instead of the plumbing: a check that stops the later checks running, a
credential printed in full, a stale cache reported as fresh, a scaffolded
source reported as working, and an exit code that says usable when it is not.

No test reaches the network. Source probes are mocked with ``respx`` and the
offline cases assert the route was never called at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from fbe.cli import (
    EXIT_OK,
    EXIT_UNUSABLE,
    GAP_SAMPLE_LIMIT,
    KEY_PREFIX_LENGTH,
    LABEL_WIDTH,
    CheckStatus,
    _check_sources,
    _probe,
    app,
)
from fbe.config import Config, DataConfig, load_config
from fbe.datasources.base import BaseDataSource, ProbeRequest, RateLimit, SourceError
from fbe.datasources.cache import DiskCache
from fbe.datasources.prices import STOOQ_CSV_URL, PricesSource
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHALLENGE_BODY = (FIXTURES / "stooq_challenge.html").read_text()
"""Stooq's anti-bot page, served with HTTP 200. Committed by PR #99."""
CSV_BODY = (FIXTURES / "stooq_spx_daily.csv").read_text()
"""A genuine Stooq session history, so the fix cannot be a blanket refusal."""

CREDENTIAL = "abcdef0123456789abcdef0123456789"
PROBE_URL = "https://example.test/api/"
SECOND_URL = "https://second.test/api/"

FORBIDDEN = (
    "edge",
    "win rate",
    "hit rate",
    "backtest",
    "proven",
    "profitable",
    "accuracy",
    "expectancy",
)
"""Words doctor must never print. It reports plumbing, and this is the command
an operator runs when they already doubt the output."""

runner = CliRunner()

BAD_WEIGHTS = (
    "scoring:\n"
    "  weights:\n"
    "    monetary: 0.2\n"
    "    inflation: 0.2\n"
    "    growth: 0.2\n"
    "    employment: 0.2\n"
    "    external: 0.2\n"
    "    positioning: 0.2\n"
    "    risk: 0.2\n"
    "risk:\n"
    "  risk_per_trade_max: 0.05\n"
)
"""Every pillar listed, as the loader demands, summing to 1.4 rather than 1.0.

The loader refuses a partial block outright, so a `ConfigError` would never
reach ``doctor``. This is the shape that loads and then fails
`Config.validate`, which is the case doctor exists to print.

Two problems rather than one on purpose: the criterion is that *every* problem
prints, and a single-problem config cannot tell that from printing the first."""


class _Reachable(BaseDataSource):
    """A source that is configured and answers, so a probe can succeed."""

    name = "reachable"
    base_url = PROBE_URL
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
        return ()

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        return {}


class _SecondReachable(_Reachable):
    """A second answering source, so one failure is not every failure."""

    name = "second"
    base_url = SECOND_URL


class _Unconfigured(_Reachable):
    """A source that reports itself unusable, which is not an error."""

    name = "unconfigured"

    def available(self) -> bool:
        return False


class _Scaffolded(_Reachable):
    """A source whose ``available()`` is still a stub, as all seven are today."""

    name = "scaffolded"

    def available(self) -> bool:
        raise NotImplementedError(
            "fbe.datasources.fake.Scaffolded.available is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )


def _config_file(tmp_path: Path, body: str = "", *, with_key: bool = False) -> Path:
    """Write a config pointing every directory at tmp_path."""
    path = tmp_path / "config.yaml"
    key = f"\n  fred_api_key: {CREDENTIAL}" if with_key else ""
    path.write_text(
        f"data:\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}{key}\n" + body
    )
    return path


def _run(path: Path, *args: str) -> Result:
    return runner.invoke(app, ["--config", str(path), "doctor", *args])


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer machine with FRED_API_KEY set must not change the answers."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)


@pytest.fixture
def only_scaffolded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Scaffolded,))


# --- the five checks, and that one failure does not hide the rest -----------


@respx.mock
def test_the_five_checks_print_in_the_documented_order(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path))

    positions = [
        result.stdout.index(label)
        for label in ("config", "credentials", "cache", "sources", "reports")
    ]
    assert positions == sorted(positions)


@respx.mock
def test_each_check_prints_its_own_labelled_line(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path))

    labels = [
        line.split()[0]
        for line in result.stdout.splitlines()
        if line and not line.startswith(" ")
    ]
    for label in ("config", "credentials", "cache", "sources", "reports"):
        assert label in labels


@respx.mock
def test_an_invalid_config_does_not_stop_the_other_four_checks(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """An operator running doctor wants the whole picture, not a bisect."""
    path = _config_file(
        tmp_path,
        BAD_WEIGHTS,
    )

    result = _run(path)

    assert "config" in result.stdout
    for label in ("credentials", "cache", "sources", "reports"):
        assert label in result.stdout
    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_every_config_problem_prints_on_its_own_line(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = _config_file(
        tmp_path,
        BAD_WEIGHTS,
    )
    problems = load_config(path).validate()
    assert len(problems) >= 2

    result = _run(path)

    for problem in problems:
        assert problem in result.stdout


@respx.mock
def test_a_config_that_will_not_load_prints_a_check_not_a_traceback(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """An unknown key is the commonest config mistake there is, and doctor is
    the one command that has to stay usable when nothing else is."""
    path = tmp_path / "config.yaml"
    path.write_text("data:\n  cach_dir: /tmp/typo\n")

    result = _run(path)

    assert "Traceback" not in result.stdout
    assert "cach_dir" in result.stdout
    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_a_config_that_will_not_load_says_the_others_cannot_run(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("data:\n  cach_dir: /tmp/typo\n")

    result = _run(path)

    assert "no other check can run" in result.stdout
    assert result.exit_code == EXIT_UNUSABLE


# --- credentials ------------------------------------------------------------


@respx.mock
def test_a_missing_credential_is_named_as_absent(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path))

    assert "FRED_API_KEY" in result.stdout
    assert "absent" in result.stdout


@respx.mock
def test_a_present_credential_is_named_without_printing_it(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path, with_key=True))

    assert "FRED_API_KEY" in result.stdout
    assert "present" in result.stdout
    assert CREDENTIAL not in result.stdout


@respx.mock
def test_show_keys_prints_only_the_first_four_characters(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Enough to tell which key is loaded, not enough to use."""
    result = _run(_config_file(tmp_path, with_key=True), "--show-keys")

    assert CREDENTIAL[:4] in result.stdout
    assert CREDENTIAL not in result.stdout
    assert CREDENTIAL[:8] not in result.stdout


# --- cache ------------------------------------------------------------------


def _fill_cache(tmp_path: Path, entries: int = 3, source: str = "fred") -> DiskCache:
    cache = DiskCache(DataConfig(cache_dir=tmp_path / "cache"))
    for index in range(entries):
        key = cache.key_for(source, "series", {"id": str(index)})
        cache.put(source, key, b'{"observations": []}', {"id": str(index)})
    return cache


def _age_source(tmp_path: Path, source: str, hours: float) -> None:
    """Age every entry belonging to one source."""
    stamp = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    for meta_path in (tmp_path / "cache" / source).glob("*.meta.json"):
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = stamp
        meta_path.write_text(json.dumps(meta))


def _age_every_entry(tmp_path: Path, hours: float) -> None:
    """Age every entry to the same figure."""
    _age_entries(tmp_path, [hours])


def _age_entries(tmp_path: Path, hours: Sequence[float]) -> None:
    """Age each entry in turn, cycling through ``hours``.

    Distinct ages matter: with every entry the same age, reporting the oldest
    as the newest is indistinguishable from reporting it correctly.
    """
    now = datetime.now(UTC)
    for index, meta_path in enumerate(
        sorted((tmp_path / "cache").rglob("*.meta.json"))
    ):
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = (
            now - timedelta(hours=hours[index % len(hours)])
        ).isoformat()
        meta_path.write_text(json.dumps(meta))


@respx.mock
def test_the_cache_check_reports_the_entry_count(
    tmp_path: Path, only_scaffolded: None
) -> None:
    _fill_cache(tmp_path, entries=3)

    result = _run(_config_file(tmp_path))

    assert "3 entries" in result.stdout


@respx.mock
def test_a_cache_older_than_the_ttl_is_a_warning(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """A stale cache reported as fresh is why a run looks inexplicably old.

    Mixed ages, so reporting the newest entry instead of the oldest would say
    2h and pass as healthy on a cache that is 19h stale.
    """
    _fill_cache(tmp_path)
    _age_entries(tmp_path, [18.5, 2, 2])

    result = _run(_config_file(tmp_path))

    assert "19h" in result.stdout
    assert "ttl 12h" in result.stdout
    assert "warn" in result.stdout


@respx.mock
def test_a_cache_inside_the_ttl_is_not_a_warning(
    tmp_path: Path, only_scaffolded: None
) -> None:
    _fill_cache(tmp_path)
    _age_every_entry(tmp_path, hours=2)

    result = _run(_config_file(tmp_path))

    cache_line = next(
        line for line in result.stdout.splitlines() if line.startswith("cache")
    )
    assert "ok" in cache_line


@respx.mock
def test_the_ttl_is_read_from_the_config_not_a_literal(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Override the TTL and the same cache must change verdict."""
    _fill_cache(tmp_path)
    _age_every_entry(tmp_path, hours=19)

    generous = _run(_config_file(tmp_path, "  cache_ttl_hours: 48\n"))

    cache_line = next(
        line for line in generous.stdout.splitlines() if line.startswith("cache")
    )
    assert "ttl 48h" in cache_line
    assert "ok" in cache_line


@respx.mock
def test_an_empty_cache_is_reported_rather_than_omitted(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path))

    cache_line = next(
        line for line in result.stdout.splitlines() if line.startswith("cache")
    )
    assert "0 entries" in cache_line


@respx.mock
def test_the_oldest_entry_across_every_source_is_the_one_reported(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """`DiskCache.stats` reports per source, so the figure doctor prints has to
    be the oldest across all of them. Reporting the newest would call a cache
    healthy on the strength of whichever source was refreshed most recently.
    """
    _fill_cache(tmp_path, entries=2, source="fred")
    _fill_cache(tmp_path, entries=2, source="cftc")
    _age_source(tmp_path, "fred", hours=18.5)
    _age_source(tmp_path, "cftc", hours=2)

    result = _run(_config_file(tmp_path))

    cache_line = next(
        line for line in result.stdout.splitlines() if line.startswith("cache")
    )
    assert "4 entries" in cache_line
    assert "oldest 19h" in cache_line
    assert "warn" in cache_line


# --- sources ----------------------------------------------------------------


@respx.mock
def test_every_source_in_all_sources_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _Unconfigured, _Scaffolded))

    result = _run(_config_file(tmp_path))

    for name in ("reachable", "unconfigured", "scaffolded"):
        assert name in result.stdout


@respx.mock
def test_a_scaffolded_source_reports_unavailable_not_a_traceback(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """The honest answer for a source that does not exist yet, and the reason
    doctor can land before the sources do."""
    result = _run(_config_file(tmp_path))

    assert result.exception is None
    assert "Traceback" not in result.stdout
    assert "scaffolded" in result.stdout


@respx.mock
def test_an_available_source_is_probed_and_timed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))

    result = _run(_config_file(tmp_path))

    assert route.call_count == 1
    assert "reachable" in result.stdout
    assert "ms" in result.stdout


@respx.mock
def test_the_probe_is_the_sources_own_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe carries the source's headers and follows its redirects.

    The Bank of England refuses the client default User-Agent with a 403 and
    answers its database URL with a 302 on every request. A bare GET therefore
    printed a healthy provider as "refused the request: check the credential",
    which is the wrong diagnosis on the day the operator runs doctor (#218).
    Both settings are read off the source, so the probe cannot drift from the
    fetch.
    """

    class _Particular(_Reachable):
        name = "particular"
        default_headers = MappingProxyType({"User-Agent": "fbe-test/1"})
        follow_redirects = True

    moved = respx.get(PROBE_URL).mock(
        return_value=httpx.Response(302, headers={"Location": SECOND_URL})
    )
    landed = respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Particular,))

    result = _run(_config_file(tmp_path))

    assert moved.calls.last.request.headers["User-Agent"] == "fbe-test/1"
    assert landed.call_count == 1
    assert "particular" in result.stdout
    assert "ms" in result.stdout
    assert "302" not in result.stdout


@respx.mock
def test_an_unavailable_source_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Availability is about configuration. Probing a source with no key would
    report a credential problem as a network one."""
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Unconfigured,))

    _run(_config_file(tmp_path))

    assert route.call_count == 0


@respx.mock
def test_a_failed_probe_is_a_warning_naming_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dead source among working ones is a recorded coverage gap, not a
    reason to refuse the whole run."""
    route = respx.get(PROBE_URL).mock(side_effect=httpx.ConnectError("refused"))
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    result = _run(_config_file(tmp_path, with_key=True))

    assert route.call_count == 1
    assert "Traceback" not in result.stdout
    assert "reachable" in result.stdout
    assert "warn" in result.stdout
    assert result.exit_code == EXIT_OK


@respx.mock
def test_every_probed_source_failing_is_a_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/interfaces.md counts "every source failed" among the conditions for
    exit 1, because nothing can be fetched and the next command in a chain
    should not run."""
    first = respx.get(PROBE_URL).mock(side_effect=httpx.ConnectError("refused"))
    second = respx.get(SECOND_URL).mock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    result = _run(_config_file(tmp_path, with_key=True))

    assert first.call_count == 1
    assert second.call_count == 1
    assert "all 2 probed sources failed" in result.stdout
    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_every_source_scaffolded_is_not_a_hard_failure(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Nothing failed. A phase has not landed, which is why doctor can be the
    thing that tells you so."""
    result = _run(_config_file(tmp_path, with_key=True))

    assert "scaffolded" in result.stdout
    assert "probed sources failed" not in result.stdout
    assert result.exit_code == EXIT_OK


@respx.mock
def test_a_source_answering_an_error_status_is_not_reported_as_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503 arrives as fast as a 200. Timing it and calling it ok is how an
    operator ends up blaming the model for an outage."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(503))
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    result = _run(_config_file(tmp_path, with_key=True))

    assert "503" in result.stdout
    assert "warn" in result.stdout


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_credential_is_named_as_a_credential_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """The key being present is not the key working, and an operator told the
    network is fine will go looking in the wrong place."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(status))
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    result = _run(_config_file(tmp_path, with_key=True))

    # Asserted on the sources block, not the whole output: the word
    # "credential" is a substring of the "credentials" label above it, so
    # searching the whole of stdout would pass whatever the sources line said.
    source_lines = [
        line
        for line in result.stdout.splitlines()
        if "reachable" in line and "second" not in line
    ]
    assert source_lines
    assert str(status) in source_lines[0]
    assert "check the credential" in source_lines[0]


@respx.mock
def test_sources_that_answer_share_one_comma_joined_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layout published in docs/interfaces.md."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    result = _run(_config_file(tmp_path, with_key=True))

    sources_line = next(
        line for line in result.stdout.splitlines() if line.startswith("sources")
    )
    assert "reachable" in sources_line
    assert "second" in sources_line
    assert "," in sources_line


@respx.mock
def test_a_timed_out_probe_is_a_warning_naming_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(PROBE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))

    result = _run(_config_file(tmp_path), "--timeout", "0.5")

    assert "reachable" in result.stdout
    assert "0.5" in result.stdout
    assert "warn" in result.stdout


@respx.mock
def test_the_timeout_option_bounds_each_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The figure must reach httpx, not merely be printed."""
    seen: list[object] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    respx.get(PROBE_URL).mock(side_effect=capture)
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))

    _run(_config_file(tmp_path), "--timeout", "1.5")

    assert seen
    assert seen[0] == {
        "connect": 1.5,
        "read": 1.5,
        "write": 1.5,
        "pool": 1.5,
    }


@respx.mock
def test_a_failed_probe_does_not_stop_the_reports_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(PROBE_URL).mock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))

    result = _run(_config_file(tmp_path))

    assert route.call_count == 1
    assert "reports" in result.stdout


# --- offline ----------------------------------------------------------------


@respx.mock
def test_an_offline_run_performs_no_probe_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))

    result = runner.invoke(
        app, ["--config", str(_config_file(tmp_path)), "--offline", "doctor"]
    )

    assert route.call_count == 0
    assert "offline" in result.stdout
    assert "reachable" in result.stdout


@respx.mock
def test_an_offline_run_still_reports_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline removes the probe, not the configuration question."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _Unconfigured))

    result = runner.invoke(
        app, ["--config", str(_config_file(tmp_path)), "--offline", "doctor"]
    )

    assert "reachable" in result.stdout
    assert "unconfigured" in result.stdout


# --- reports ----------------------------------------------------------------


@respx.mock
def test_an_empty_reports_directory_says_there_is_nothing_to_diff(
    tmp_path: Path, only_scaffolded: None
) -> None:
    result = _run(_config_file(tmp_path))

    reports_line = next(
        line for line in result.stdout.splitlines() if line.startswith("reports")
    )
    assert "no previous report" in reports_line


@respx.mock
def test_a_previous_report_with_a_matching_digest_is_reported(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = _config_file(tmp_path)
    digest = load_config(path).digest()
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text(
        json.dumps({"asof": "2026-09-08", "config_digest": digest})
    )

    result = _run(path)

    assert "2026-09-08" in result.stdout
    assert "digest matches" in result.stdout


@respx.mock
def test_a_previous_report_with_a_different_digest_is_a_warning(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """A digest change means the weights moved, so the two runs are not
    comparable and a diff against them would mislead."""
    path = _config_file(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text(
        json.dumps({"asof": "2026-09-08", "config_digest": "000000000000"})
    )

    result = _run(path)

    reports_line = next(
        line for line in result.stdout.splitlines() if line.startswith("reports")
    )
    assert "2026-09-08" in reports_line
    assert "warn" in reports_line
    assert "not comparable" in reports_line


@respx.mock
def test_the_newest_report_is_the_one_reported(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = _config_file(tmp_path)
    digest = load_config(path).digest()
    reports = tmp_path / "reports"
    reports.mkdir()
    for day in ("2026-09-01", "2026-09-08", "2026-09-04"):
        (reports / f"bias-{day}.json").write_text(
            json.dumps({"asof": day, "config_digest": digest})
        )

    result = _run(path)

    assert "2026-09-08" in result.stdout
    assert "2026-09-01" not in result.stdout


@respx.mock
def test_a_reports_path_that_is_not_a_directory_is_a_failure(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Reporting "nothing to diff against" would read as a fresh checkout,
    while every `fbe report` will in fact fail to write."""
    path = _config_file(tmp_path)
    (tmp_path / "reports").write_text("this is a file, not a directory")

    result = _run(path)

    reports_line = next(
        line for line in result.stdout.splitlines() if line.startswith("reports")
    )
    assert "fail" in reports_line
    assert "not a directory" in reports_line
    assert result.exit_code == EXIT_UNUSABLE


# --- gaps in the forward record (issue #284) --------------------------------
#
# `_check_reports` read only the newest sidecar, so a month with eleven missing
# mornings printed exactly like a month with none. The cost of finding out late
# is asymmetric: a bias cannot be recorded after the outcome is known, so a gap
# noticed in month six is a hole in the record for good. These tests pin the
# clock, because the window the check measures ends at today and every
# expectation below would otherwise move one day per day.

GAP_TODAY = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
"""A Friday, pinned. Chosen as a weekday so that "today is not reported as
missing" is a real assertion rather than one the weekend satisfies."""


@pytest.fixture
def today_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the clock doctor reads, through the one seam every command uses."""
    monkeypatch.setattr("fbe.cli._now", lambda: GAP_TODAY)


def _record(path: Path, tmp_path: Path, *days: str) -> Path:
    """Write one readable sidecar per named day, carrying the live digest.

    The digest matches deliberately, so the first reports line stays ``ok`` and
    a test reading the gap line is not also reading a digest complaint.
    """
    digest = load_config(path).digest()
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    for day in days:
        (reports / f"bias-{day}.json").write_text(
            json.dumps({"asof": day, "config_digest": digest})
        )
    return reports


@respx.mock
def test_a_missing_weekday_is_named_and_counted(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 1, on a fixture directory holding one deliberate gap."""
    path = _config_file(tmp_path)
    _record(path, tmp_path, "2026-09-21", "2026-09-22", "2026-09-24")

    block = _block(_run(path), "reports")

    gap = block[-1]
    # "1 weekday missing", not "1 weekdays missing": the substring without the
    # next word is satisfied by the plural, so it pins nothing.
    assert "1 weekday missing" in gap
    assert "2026-09-23" in gap


@respx.mock
def test_a_weekend_is_not_reported_as_a_gap(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 2, first half. 19 and 20 September 2026 are a Saturday and a
    Sunday, and the engine is not run on either."""
    path = _config_file(tmp_path)
    _record(
        path,
        tmp_path,
        "2026-09-17",
        "2026-09-18",
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
        "2026-09-24",
    )

    result = _run(path)

    assert "2026-09-19" not in result.stdout
    assert "2026-09-20" not in result.stdout
    assert "unbroken" in _block(result, "reports")[-1]


@respx.mock
def test_a_weekday_that_has_a_report_is_not_named(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 2, second half. The days on either side of the gap are on
    disk, and naming them would send the owner looking for a report that is
    already there."""
    path = _config_file(tmp_path)
    _record(
        path,
        tmp_path,
        "2026-09-14",
        "2026-09-18",
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
        "2026-09-24",
    )

    gap = _block(_run(path), "reports")[-1]

    assert "3 weekdays" in gap
    for missing in ("2026-09-15", "2026-09-16", "2026-09-17"):
        assert missing in gap
    for present in ("2026-09-14", "2026-09-18", "2026-09-21", "2026-09-24"):
        assert present not in gap


@respx.mock
def test_an_unbroken_record_says_so(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 1's other half. Silence would be indistinguishable from a
    check that did not run, which is the state this issue is about."""
    path = _config_file(tmp_path)
    _record(path, tmp_path, "2026-09-22", "2026-09-23", "2026-09-24")

    block = _block(_run(path), "reports")

    assert "unbroken" in block[-1]
    assert "2026-09-22" in block[-1]
    assert "ok" in block[-1]


@respx.mock
def test_a_long_gap_prints_a_count_and_a_bounded_sample(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 3. Eighty-two missing weekdays is the case the criterion
    names, and a check that answers it with eighty-two lines is a check the
    owner scrolls past."""
    path = _config_file(tmp_path)
    _record(path, tmp_path, "2026-06-01", "2026-09-24")

    block = _block(_run(path), "reports")

    gap = block[-1]
    # 84 weekdays from 1 June to 24 September inclusive, two of which have a
    # report. Counted here by hand rather than from the implementation.
    assert "82 weekdays" in gap
    assert len(block) == 2
    assert gap.count("2026-") == GAP_SAMPLE_LIMIT
    assert f"{82 - GAP_SAMPLE_LIMIT} more" in gap


@respx.mock
def test_an_empty_reports_directory_checks_no_record(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 4. Every routine host clones fresh and sees this, so a check
    that reported every weekday since the epoch would be muted within a week,
    taking the real gaps with it."""
    result = _run(_config_file(tmp_path))

    block = _block(result, "reports")

    assert len(block) == 1
    assert "2026-" not in block[0]
    assert "missing" not in block[0]


def _clean_but_for_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *days: str
) -> Path:
    """A run where the forward record is the only thing that can warn.

    Sources answer, the credential is present, the cache is fresh and the
    broker profile is confirmed. The three exit-code tests below differ only in
    which days are on disk, so an exit code that moves between them moves on
    the record check and on nothing else. Without this the broker profile is
    unconfirmed by default, its warning alone turns ``--strict`` to exit 1, and
    the criterion-5 assertions would pass with no record check at all.
    """
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    path = _config_file(tmp_path, "broker:\n  confirmed: true\n", with_key=True)
    _record(path, tmp_path, *days)
    _fill_cache(tmp_path)
    return path


@respx.mock
def test_a_gap_warns_and_doctor_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today_is_fixed: None
) -> None:
    """Criterion 5. A hole in the record is worth acting on and is not a reason
    to call the install unusable."""
    path = _clean_but_for_the_record(tmp_path, monkeypatch, "2026-09-21", "2026-09-24")

    result = _run(path)

    assert "warn" in _block(result, "reports")[-1]
    assert result.exit_code == EXIT_OK


@respx.mock
def test_a_gap_is_a_failure_under_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today_is_fixed: None
) -> None:
    """Criterion 5's other half, and the behaviour every other warning here
    already has."""
    path = _clean_but_for_the_record(tmp_path, monkeypatch, "2026-09-21", "2026-09-24")

    result = _run(path, "--strict")

    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_an_unbroken_record_survives_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today_is_fixed: None
) -> None:
    """The control for the two above. Same install, same flags, no gap, and the
    run stands, so the exit code they assert is this check's and not a warning
    that would have been there anyway."""
    path = _clean_but_for_the_record(
        tmp_path, monkeypatch, "2026-09-22", "2026-09-23", "2026-09-24"
    )

    assert _run(path, "--strict").exit_code == EXIT_OK


@respx.mock
def test_presence_is_read_from_the_name_and_not_from_the_sidecar(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Criterion 6. A damaged report is a separate finding the digest line
    already makes, and decoding a file to establish that it exists would file a
    corrupt sidecar as a morning nobody ran."""
    path = _config_file(tmp_path)
    reports = _record(path, tmp_path, "2026-09-22", "2026-09-24")
    (reports / "bias-2026-09-23.json").write_text("{ truncated")

    block = _block(_run(path), "reports")

    assert "unbroken" in block[-1]
    assert "2026-09-23" not in block[-1]


@respx.mock
def test_today_is_not_reported_as_missing(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """doctor runs before the morning report as often as after it, and a check
    that cries wolf every morning is one nobody reads by Wednesday."""
    path = _config_file(tmp_path)
    _record(path, tmp_path, "2026-09-23", "2026-09-24")

    result = _run(path)

    assert "2026-09-25" not in result.stdout
    assert "unbroken" in _block(result, "reports")[-1]


@respx.mock
def test_a_name_with_no_date_withholds_the_gap_list(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """A file the glob matches but the name convention cannot date could be any
    morning, so the days around it cannot be called missing. Naming the file
    and withholding the list says what is known; listing the gaps anyway would
    report a morning that may well be on disk."""
    path = _config_file(tmp_path)
    reports = _record(path, tmp_path, "2026-09-21", "2026-09-24")
    # Readable, and carrying the live digest, so the digest line stays ok and
    # the only thing wrong with this file is that its name says no day.
    (reports / "bias-backup.json").write_text(
        json.dumps({"asof": "2026-09-24", "config_digest": load_config(path).digest()})
    )

    block = _block(_run(path), "reports")

    named = block[-1]
    assert len(block) == 2
    assert "bias-backup.json" in named
    assert "as-of date" in named
    assert named[LABEL_WIDTH:].startswith("warn")
    assert not any("missing" in line for line in block)
    assert "2026-09-22" not in named


@respx.mock
def test_a_damaged_newest_sidecar_does_not_suppress_the_record_check(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """The two findings are independent, and this is the pairing that matters:
    one unreadable file would otherwise hide every missing morning behind it,
    which is the failure this check exists to end rather than a second copy of
    it."""
    path = _config_file(tmp_path)
    reports = _record(path, tmp_path, "2026-09-21")
    (reports / "bias-2026-09-24.json").write_text("{ truncated")

    block = _block(_run(path), "reports")

    assert "could not be read" in block[0]
    assert len(block) == 2
    assert "2 weekdays missing" in block[-1]
    assert "2026-09-22" in block[-1]
    assert "2026-09-23" in block[-1]


@respx.mock
def test_a_digest_that_no_longer_matches_does_not_suppress_the_record_check(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """Moving a weight changes the digest, so this is the ordinary state of a
    project still setting its priors rather than a corner case. A record check
    that went quiet on it would be quiet for weeks at a time."""
    path = _config_file(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    for day in ("2026-09-21", "2026-09-24"):
        (reports / f"bias-{day}.json").write_text(
            json.dumps({"asof": day, "config_digest": "000000000000"})
        )

    block = _block(_run(path), "reports")

    assert "not comparable" in block[0]
    assert len(block) == 2
    assert "2 weekdays missing" in block[-1]


@respx.mock
def test_the_digest_line_still_prints_beside_a_gap(
    tmp_path: Path, only_scaffolded: None, today_is_fixed: None
) -> None:
    """The gap lines are additions. Losing the digest line to gain them would
    trade one silent failure for another."""
    path = _config_file(tmp_path)
    _record(path, tmp_path, "2026-09-21", "2026-09-22", "2026-09-24")

    block = _block(_run(path), "reports")

    assert "digest matches" in block[0]
    assert "2026-09-24" in block[0]
    assert len(block) == 2


@respx.mock
def test_a_source_whose_constructor_raises_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A constructor that raises is a programming error, not an outage, and
    reporting it as "unavailable" would file it alongside a missing key."""

    class _Broken(_Reachable):
        name = "broken"

        def __init__(self, config: DataConfig) -> None:
            raise TypeError("wrong arity")

    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Broken,))

    result = _run(_config_file(tmp_path, with_key=True))

    assert "Traceback" not in result.stdout
    assert "broken" in result.stdout
    assert "wrong arity" in result.stdout
    assert "fail" in result.stdout
    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_an_unreadable_report_is_a_warning_not_a_traceback(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = _config_file(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text("{ truncated")

    result = _run(path)

    assert result.exception is None
    assert "Traceback" not in result.stdout
    assert "warn" in result.stdout


# --- exit codes -------------------------------------------------------------


@respx.mock
def test_exit_zero_when_every_check_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    path = _config_file(tmp_path, with_key=True)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text(
        json.dumps({"asof": "2026-09-08", "config_digest": load_config(path).digest()})
    )
    _fill_cache(tmp_path)

    result = _run(path)

    assert result.exit_code == EXIT_OK


@respx.mock
def test_exit_one_on_a_hard_failure(tmp_path: Path, only_scaffolded: None) -> None:
    path = _config_file(
        tmp_path,
        BAD_WEIGHTS,
    )

    assert _run(path).exit_code == EXIT_UNUSABLE


@respx.mock
def test_a_warning_alone_exits_zero_without_strict(
    tmp_path: Path, only_scaffolded: None
) -> None:
    _fill_cache(tmp_path)
    _age_every_entry(tmp_path, hours=19)

    result = _run(_config_file(tmp_path, with_key=True))

    assert "warn" in result.stdout
    assert result.exit_code == EXIT_OK


@respx.mock
def test_a_warning_exits_one_under_strict(
    tmp_path: Path, only_scaffolded: None
) -> None:
    _fill_cache(tmp_path)
    _age_every_entry(tmp_path, hours=19)

    result = _run(_config_file(tmp_path, with_key=True), "--strict")

    assert result.exit_code == EXIT_UNUSABLE


@respx.mock
def test_strict_does_not_turn_a_clean_run_into_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    # A confirmed broker, or the profile's own warning turns this clean run into
    # a strict failure, which is the broker check working rather than this test
    # breaking.
    path = _config_file(tmp_path, "broker:\n  confirmed: true\n", with_key=True)
    # Dated today, not in the past: the forward-record check measures from the
    # earliest report to today, so a fixture dated three weeks back is a record
    # with three weeks of holes in it, and this run would not be clean.
    _record(path, tmp_path, date.today().isoformat())
    _fill_cache(tmp_path)

    assert _run(path, "--strict").exit_code == EXIT_OK


def _otherwise_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> Path:
    """A run where the broker is the only thing that could warn.

    Sources answer, the credential is present, the cache is fresh and a matching
    report exists, so the only verdict that moves between confirmed and
    unconfirmed is the broker line. That is what lets the exit-code assertions
    below turn on the broker check and nothing else.
    """
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    path = _config_file(tmp_path, body, with_key=True)
    # Today's date, so the forward record is one morning long and unbroken. A
    # report dated in the past would add a gap warning here, and the exit-code
    # tests below would stop turning on the broker line alone.
    _record(path, tmp_path, date.today().isoformat())
    _fill_cache(tmp_path)
    return path


@respx.mock
def test_doctor_reports_an_unconfirmed_profile_and_prints_its_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfirmed profile is a warn line naming its three lot values, so a
    reader can check them against a contract without opening the config."""
    path = _otherwise_clean(tmp_path, monkeypatch, "")

    line = _line(_run(path), "broker")

    assert line[LABEL_WIDTH:].startswith("warn")
    assert "unconfirmed" in line
    assert "min_lot" in line and "lot_step" in line and "contract_size" in line


@respx.mock
def test_plain_doctor_exits_zero_on_an_unconfirmed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfirmed is usable, so without --strict the run stands."""
    path = _otherwise_clean(tmp_path, monkeypatch, "")

    assert _run(path).exit_code == EXIT_OK


@respx.mock
def test_doctor_strict_exits_one_on_an_unconfirmed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker warning is the only warning here, so --strict turning the run
    to exit 1 is the broker check doing its job rather than any other line."""
    path = _otherwise_clean(tmp_path, monkeypatch, "")

    assert _run(path, "--strict").exit_code == EXIT_UNUSABLE


@respx.mock
def test_a_confirmed_profile_is_ok_and_survives_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _otherwise_clean(tmp_path, monkeypatch, "broker:\n  confirmed: true\n")

    result = _run(path, "--strict")

    assert result.exit_code == EXIT_OK
    assert _line(result, "broker")[LABEL_WIDTH:].startswith("ok")


# --- what doctor must never say ---------------------------------------------


@respx.mock
def test_no_line_claims_anything_about_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor reports plumbing. An edge claim here would be a claim nobody has
    measured, on the one command an operator runs when they already doubt the
    output."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    _fill_cache(tmp_path)

    result = _run(_config_file(tmp_path, with_key=True))

    lowered = result.stdout.lower()
    for word in FORBIDDEN:
        assert word not in lowered


@respx.mock
def test_the_real_registry_reports_every_source_offline(tmp_path: Path) -> None:
    """Every other source test patches ALL_SOURCES, so nothing exercises the
    real tuple. Offline needs no mocking and no credential."""
    result = runner.invoke(
        app, ["--config", str(_config_file(tmp_path)), "--offline", "doctor"]
    )

    assert result.exit_code == EXIT_OK
    assert "Traceback" not in result.stdout
    for name in (
        "fred",
        "oecd",
        "ecb",
        "boc",
        "mof_jp",
        "boe",
        "rba",
        "snb",
        "rbnz",
        "cftc",
        "stooq",
        "forexfactory",
        "manual",
    ):
        assert name in result.stdout
    # The fan-out is the providers' base and not a source in a run, so its
    # name must not appear as a line of its own (ADR 0013, #218).
    assert "curves" not in result.stdout


# --- the worked example in docs/interfaces.md -------------------------------


def _published_block() -> list[str]:
    """Return the console block published under ``### fbe doctor``.

    The repository's rule is that a worked example in the docs is a fixture: an
    example that does not reproduce is worse than no example. This reads the
    doc rather than restating it, so the two cannot drift apart silently.
    """
    text = (Path(__file__).resolve().parents[1] / "docs" / "interfaces.md").read_text()
    start = text.index("### `fbe doctor`")
    block = text.index("```console\n$ fbe doctor\n", start)
    body = text[block + len("```console\n$ fbe doctor\n") :]
    return body[: body.index("```")].rstrip("\n").split("\n")


@respx.mock
def test_the_published_console_block_reproduces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuild the doc's scenario and compare the rendering line for line.

    This is what pins the column widths, the wording of every verdict and the
    summary. Each of those is otherwise only checked by a substring assertion
    that would survive the column moving or a label being reworded.
    """
    published = _published_block()

    class _Fred(_Reachable):
        name = "fred"
        base_url = "https://fred.test/"

    class _Stooq(_Reachable):
        name = "stooq"
        base_url = "https://stooq.test/"

    class _Cftc(_Reachable):
        name = "cftc"
        base_url = "https://cftc.test/"

    class _Ff(_Reachable):
        name = "forexfactory"
        base_url = "https://ff.test/"

    for source in (_Fred, _Stooq, _Cftc):
        respx.get(source.base_url).mock(return_value=httpx.Response(200))
    respx.get(_Ff.base_url).mock(side_effect=httpx.ReadTimeout("slow"))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Fred, _Stooq, _Cftc, _Ff))

    # The published block names the days missing from the forward record, and
    # those depend on what today is. Pinned here, and the doc's scenario is
    # that Tuesday: one report a week old, and the four weekdays since.
    monkeypatch.setattr("fbe.cli._now", lambda: datetime(2026, 9, 15, 7, 0, tzinfo=UTC))
    path = _config_file(tmp_path, with_key=True)
    _fill_cache(tmp_path, entries=37)
    _age_every_entry(tmp_path, hours=18.5)
    _record(path, tmp_path, "2026-09-08")

    produced = _run(path).stdout.rstrip("\n").split("\n")

    assert len(produced) == len(published)
    for got, want in zip(produced, published, strict=True):
        if "ms" in want:
            # The published timings are illustrative, so the figures are not
            # compared. Everything around them is.
            assert got.startswith("sources         ok        ")
            assert "fred " in got and "stooq " in got and "cftc " in got
            continue
        assert got == want


# --- verdicts, asserted on the line that carries them -----------------------


def _line(result: Result, label: str) -> str:
    """Return the line starting with ``label``, so a verdict printed by some
    other check cannot satisfy the assertion."""
    return next(line for line in result.stdout.splitlines() if line.startswith(label))


def _block(result: Result, label: str) -> list[str]:
    """Return a check's line and every continuation line under it."""
    lines = result.stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(label))
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith(" "):
            break
        block.append(line)
    return block


@respx.mock
def test_no_part_of_a_key_is_printed_without_show_keys(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Not just the whole key. The prefix is opt-in, so output can be pasted
    into an issue without a second thought."""
    result = _run(_config_file(tmp_path, with_key=True))

    assert CREDENTIAL[:KEY_PREFIX_LENGTH] not in result.stdout


@respx.mock
def test_the_key_prefix_is_exactly_four_characters(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Five would be a different disclosure decision, made by accident."""
    assert KEY_PREFIX_LENGTH == 4
    result = _run(_config_file(tmp_path, with_key=True), "--show-keys")

    assert CREDENTIAL[:KEY_PREFIX_LENGTH] in result.stdout
    assert CREDENTIAL[: KEY_PREFIX_LENGTH + 1] not in result.stdout


@respx.mock
def test_an_absent_credential_warns_on_its_own_line(
    tmp_path: Path, only_scaffolded: None
) -> None:
    assert "warn" in _line(_run(_config_file(tmp_path)), "credentials")


@respx.mock
def test_a_scaffolded_source_warns_on_its_own_line(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Today every real source takes this branch, so it decides what
    `doctor --strict` does on an actual machine."""
    assert "warn" in _line(_run(_config_file(tmp_path, with_key=True)), "sources")


@respx.mock
def test_an_unavailable_source_warns_on_its_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Unconfigured,))

    assert "warn" in _line(_run(_config_file(tmp_path, with_key=True)), "sources")


@respx.mock
def test_a_timed_out_probe_warns_on_its_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(PROBE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable, _SecondReachable))

    block = _block(
        _run(_config_file(tmp_path, with_key=True), "--timeout", "0.5"), "sources"
    )

    assert route.call_count == 1
    assert any("warn" in line and "reachable" in line for line in block)


@respx.mock
def test_an_empty_cache_is_not_a_warning(tmp_path: Path, only_scaffolded: None) -> None:
    """A fresh checkout has nothing cached, and doctor --strict must not refuse
    a machine for being new."""
    assert "ok" in _line(_run(_config_file(tmp_path, with_key=True)), "cache")


@respx.mock
def test_an_empty_reports_directory_is_not_a_warning(
    tmp_path: Path, only_scaffolded: None
) -> None:
    assert "ok" in _line(_run(_config_file(tmp_path, with_key=True)), "reports")


@respx.mock
def test_a_cache_directory_that_cannot_be_created_is_a_hard_failure(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """Nothing can be fetched into it, so no later command can succeed.

    The unwritable case is produced by putting a regular file where a parent
    directory should be, rather than by clearing the permission bits, because
    the tests run as root in CI and root ignores those bits.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data:\n"
        f"  cache_dir: {blocker / 'cache'}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  manual_dir: {tmp_path / 'manual'}\n"
        f"  fred_api_key: {CREDENTIAL}\n"
    )

    result = _run(path)

    cache_line = _line(result, "cache")
    assert "fail" in cache_line
    assert "not writable" in cache_line
    assert result.exit_code == EXIT_UNUSABLE
    assert "reports" in result.stdout


@respx.mock
def test_the_probe_silences_the_http_client_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx logs every request at INFO with the full URL, and a source is free
    to carry a credential in its base URL. `BaseDataSource` pins this for its
    own client; the probe does not go through that client."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Reachable,))
    logging.getLogger("httpx").setLevel(logging.INFO)

    _run(_config_file(tmp_path, with_key=True))

    assert logging.getLogger("httpx").level == logging.WARNING


@respx.mock
def test_an_unreadable_sidecar_warns_on_the_reports_line(
    tmp_path: Path, only_scaffolded: None
) -> None:
    path = _config_file(tmp_path, with_key=True)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text("{ truncated")

    assert "warn" in _line(_run(path), "reports")


@respx.mock
def test_the_reports_digest_is_compared_against_the_effective_config(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """A default digest would match a default config and hide every
    re-weighting, which is the one thing this check exists to catch."""
    default_digest = Config().digest()
    path = _config_file(tmp_path, "scoring:\n  min_spread_low: 0.42\n")
    assert load_config(path).digest() != default_digest

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "bias-2026-09-08.json").write_text(
        json.dumps({"asof": "2026-09-08", "config_digest": default_digest})
    )

    reports_line = _line(_run(path), "reports")
    assert "warn" in reports_line
    assert "not comparable" in reports_line


@respx.mock
def test_the_summary_counts_every_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three warnings printed and "1 warning." summarised is how the published
    example came to contradict itself."""
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Unconfigured, _Scaffolded))
    _fill_cache(tmp_path)
    _age_every_entry(tmp_path, hours=40)

    result = _run(_config_file(tmp_path))

    warnings = sum(
        1
        for line in result.stdout.splitlines()
        if line[LABEL_WIDTH:].startswith("warn")
    )
    # Five, not four: the default broker profile is unconfirmed, which is its
    # own warning. The count and the summary have to agree whatever the mix.
    assert warnings == 5
    assert result.stdout.rstrip().endswith("5 warnings.")


@respx.mock
def test_no_line_claims_anything_about_the_model_on_a_failing_run(
    tmp_path: Path, only_scaffolded: None
) -> None:
    """The clean-run summary is a fixed string. The failure and warning
    summaries are built, so they are the ones that could grow a claim."""
    path = _config_file(tmp_path, BAD_WEIGHTS)

    lowered = _run(path).stdout.lower()

    for word in FORBIDDEN:
        assert word not in lowered


# --- a 200 whose body is not the source's own content (issue #102) ----------
#
# `_probe` judged a source on its status code alone. Stooq answers a blocked
# request with HTTP 200 and an HTML proof-of-work page, so on the commonest
# real blocking mode doctor printed `stooq 118ms` and sent the operator to look
# somewhere else. The fix is a request each source vouches for, with its own
# check of the body, and not a bare GET of `base_url` run through a shared
# decoder: that would refuse a healthy source, which is the same defect with
# the sign flipped.

VOUCHED_MARKER = b"served-by-vouching"


class _Vouching(_Reachable):
    """A source that describes its own probe and recognises its own content."""

    name = "vouching"

    def probe_request(self) -> ProbeRequest:
        return ProbeRequest(path="probe", params={"q": "1"}, verify=self._verify)

    @staticmethod
    def _verify(body: bytes) -> None:
        if VOUCHED_MARKER not in body:
            raise SourceError("expected the marker and got something else")


class _BadDescription(_Reachable):
    """A source whose probe description is itself broken."""

    name = "baddesc"

    def probe_request(self) -> ProbeRequest:
        raise KeyError("XXX")


class _NoBaseUrl(_Reachable):
    """A source with nothing to probe, which is a warning and not a request."""

    name = "nobase"
    base_url = ""


@respx.mock
def test_a_challenge_page_at_200_is_not_reported_as_healthy(tmp_path: Path) -> None:
    """Criterion 2. The committed challenge page, at HTTP 200, is a warning.

    The message carries no latency: a timing is what made the healthy line
    and the blocked line indistinguishable.
    """
    respx.get(url__startswith=STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CHALLENGE_BODY)
    )

    status, detail = _probe(PricesSource(DataConfig(cache_dir=tmp_path)), 5.0)

    assert status is CheckStatus.WARN
    assert "ms" not in detail
    assert "did not serve its own content" in detail


@respx.mock
def test_a_genuine_session_history_at_200_is_healthy(tmp_path: Path) -> None:
    """Criterion 3. The same source with a real body is OK and timed."""
    respx.get(url__startswith=STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    status, detail = _probe(PricesSource(DataConfig(cache_dir=tmp_path)), 5.0)

    assert status is CheckStatus.OK
    assert detail.startswith("stooq ")
    assert detail.endswith("ms")


@respx.mock
def test_the_prices_probe_asks_for_a_session_history_not_the_bare_root(
    tmp_path: Path,
) -> None:
    """The trap the issue names. A bare GET of `base_url` never returns CSV,
    even from a healthy Stooq, so the probe has to ask for a symbol and a
    window the way `fetch_stooq` does."""
    route = respx.get(url__startswith=STOOQ_CSV_URL).mock(
        return_value=httpx.Response(200, text=CSV_BODY)
    )

    _probe(PricesSource(DataConfig(cache_dir=tmp_path)), 5.0)

    params = route.calls.last.request.url.params
    assert set(params.keys()) >= {"s", "d1", "d2", "i"}


@respx.mock
def test_the_blocked_message_is_distinct_from_every_other_warning() -> None:
    """Criterion 1. Four failures, four different sentences.

    A timeout, a refusal, an error status and a body that is not the source's
    content each need a different response from the operator, so the wording
    that tells them apart is what is pinned here.
    """
    url = PROBE_URL + "probe"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"<html>"))
    blocked = _probe(_Vouching(DataConfig()), 5.0)[1]
    respx.get(url).mock(side_effect=httpx.ReadTimeout("slow"))
    timed_out = _probe(_Vouching(DataConfig()), 5.0)[1]
    respx.get(url).mock(return_value=httpx.Response(403))
    refused = _probe(_Vouching(DataConfig()), 5.0)[1]
    respx.get(url).mock(return_value=httpx.Response(503))
    errored = _probe(_Vouching(DataConfig()), 5.0)[1]

    assert "did not serve its own content" in blocked
    assert "timeout" in timed_out and "did not serve" not in timed_out
    assert "credential" in refused and "did not serve" not in refused
    assert "HTTP 503" in errored and "did not serve" not in errored


@respx.mock
def test_a_vouched_probe_sends_the_path_and_params_the_source_named() -> None:
    route = respx.get(PROBE_URL + "probe").mock(
        return_value=httpx.Response(200, content=VOUCHED_MARKER)
    )

    status, _ = _probe(_Vouching(DataConfig()), 5.0)

    assert status is CheckStatus.OK
    assert route.calls.last.request.url.params["q"] == "1"


@respx.mock
def test_a_source_with_no_base_url_keeps_its_current_line() -> None:
    """Criterion 4, first half. Nothing to probe is a warning, never a request
    and never a report of being blocked."""
    route = respx.get(url__startswith="https://").mock(return_value=httpx.Response(200))

    status, detail = _probe(_NoBaseUrl(DataConfig()), 5.0)

    assert status is CheckStatus.WARN
    assert "names no base URL" in detail
    assert "did not serve" not in detail
    assert route.call_count == 0


@respx.mock
def test_a_source_that_describes_no_probe_is_judged_on_status_alone() -> None:
    """Criterion 4, second half. A source that has not said what its content
    looks like keeps the status-only verdict it had, whatever the body."""
    respx.get(PROBE_URL).mock(return_value=httpx.Response(200, content=b"<html>"))

    status, detail = _probe(_Reachable(DataConfig()), 5.0)

    assert status is CheckStatus.OK
    assert detail.endswith("ms")


@respx.mock
def test_an_offline_run_never_sends_a_vouched_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5. `_check_sources` short circuits before `_probe`, and the
    new request path must not have opened a way round it."""
    route = respx.get(url__startswith=PROBE_URL).mock(
        return_value=httpx.Response(200, content=VOUCHED_MARKER)
    )
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Vouching,))
    config = Config(data=DataConfig(cache_dir=tmp_path / "cache", offline=True))

    lines = _check_sources(config, 5.0)

    assert route.call_count == 0
    assert any("offline so no probe" in line.detail for line in lines)


@respx.mock
def test_a_blocked_source_prints_as_a_warning_line_under_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the command, so the verdict reaches the operator."""
    respx.get(PROBE_URL + "probe").mock(
        return_value=httpx.Response(200, content=b"<html>challenge</html>")
    )
    respx.get(SECOND_URL).mock(return_value=httpx.Response(200))
    monkeypatch.setattr("fbe.cli.ALL_SOURCES", (_Vouching, _SecondReachable))

    result = _run(_config_file(tmp_path))

    block = _block(result, "sources")
    assert any("did not serve its own content" in line for line in block)
    assert not any("vouching" in line and "ms" in line for line in block)


@respx.mock
def test_a_probe_description_that_raises_is_a_warning_not_a_traceback() -> None:
    """Doctor's own rule: nothing under the sources check raises."""
    route = respx.get(url__startswith=PROBE_URL).mock(return_value=httpx.Response(200))

    status, detail = _probe(_BadDescription(DataConfig()), 5.0)

    assert status is CheckStatus.WARN
    assert "could not describe a probe" in detail
    assert "KeyError" in detail
    assert route.call_count == 0


@respx.mock
def test_no_shipped_source_reports_as_scaffolded(tmp_path: Path) -> None:
    """Runs against the real ``ALL_SOURCES``, offline so no probe is made.

    Before #208 this printed three "scaffolded, not yet built" lines for the
    curves, OECD and Stooq sources, which was true, and which hid that the
    fetch paths behind them had landed months earlier. Offline, every source
    that needs nothing configured reports available, and the line says so.
    """
    route = respx.route().mock(return_value=httpx.Response(200))

    result = runner.invoke(
        app, ["--config", str(_config_file(tmp_path)), "--offline", "doctor"]
    )

    assert route.call_count == 0
    assert "scaffolded" not in result.stdout
    for name in ("ecb", "boc", "rba", "oecd", "stooq"):
        assert f"{name} available, offline so no probe" in result.stdout
