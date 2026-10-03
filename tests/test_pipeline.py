"""Tests for `fbe.pipeline.build_run`, the run every front end shares.

`fbe report` and the web API both build their report here (ADR 0018). The
failures that matter would not raise on their own: a run that read the calendar
before finding the cache empty makes a network request for nothing, a run that
skipped the filters strips every blocker from the page, and a run that wrote an
outage as a report turns it into tomorrow's baseline. Each test below pins one.

Collection, scoring and the bias layer are replaced, so no source is built and
no socket is opened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date

import pytest

from fbe import pipeline
from fbe.calendar_guard import DailyCalendar
from fbe.config import Config, RiskConfig
from fbe.datasources.collect import CollectionResult
from fbe.types import Conviction, CurrencyScore, Observation, PairBias
from tests.test_cli_report import ASOF, OBSERVATION, SCORES, bias, score


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    observations: Sequence[Observation] = (OBSERVATION,),
    scores: Sequence[CurrencyScore] = SCORES,
    biases: Sequence[PairBias] = (bias(),),
) -> dict[str, object]:
    """Replace the engine calls `build_run` sequences, recording what reached them."""
    seen: dict[str, object] = {}

    def fake_collect(config_in: object, **kwargs: object) -> CollectionResult:
        seen["offline"] = getattr(config_in, "offline", None)
        seen["end"] = kwargs["end"]
        return CollectionResult(observations=tuple(observations), outcomes=(), gaps={})

    def fake_apply_filters(
        bias_in: PairBias,
        scores_in: Mapping[str, CurrencyScore],
        config_in: object,
        asof_in: date,
        **hooks: object,
    ) -> PairBias:
        filtered = seen.setdefault("filtered", [])
        assert isinstance(filtered, list)
        filtered.append(bias_in.pair)
        return replace(bias_in, blockers=(*bias_in.blockers, "filtered"))

    monkeypatch.setattr("fbe.pipeline.collect", fake_collect)
    monkeypatch.setattr(
        "fbe.pipeline.score_currencies", lambda *args, **kwargs: tuple(scores)
    )
    monkeypatch.setattr(
        "fbe.pipeline.build_pair_biases", lambda *args, **kwargs: tuple(biases)
    )
    monkeypatch.setattr("fbe.pipeline.apply_filters", fake_apply_filters)
    return seen


def _no_calendar(calls: list[date]) -> pipeline.CalendarReader:
    def read(config: Config, run_date: date) -> DailyCalendar | None:
        calls.append(run_date)
        return None

    return read


def test_the_run_reads_the_cache_only_and_stops_at_its_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refetch mid-session would make two runs at one digest disagree."""
    seen = _stub(monkeypatch)

    pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar([]))

    assert seen["offline"] is True
    assert seen["end"] == ASOF


def test_an_empty_cache_is_unusable_and_reads_no_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, observations=())
    calls: list[date] = []

    with pytest.raises(pipeline.UnusableRunError, match="fbe refresh"):
        pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar(calls))

    assert calls == []


def test_a_run_that_scored_nothing_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28 neutral pairs from an outage must not become tomorrow's baseline."""
    empty = tuple(replace(row, coverage=0.0) for row in SCORES)
    _stub(monkeypatch, scores=empty)

    with pytest.raises(pipeline.UnusableRunError, match="no usable data"):
        pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar([]))


def test_every_pair_passes_through_the_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the filters would strip every blocker from the page."""
    seen = _stub(monkeypatch, biases=(bias("EURUSD"), bias("GBPUSD")))

    run = pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar([]))

    assert seen["filtered"] == ["EURUSD", "GBPUSD"]
    assert all("filtered" in row.blockers for row in run.pairs)


def test_an_unread_calendar_is_named_in_the_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch)
    calls: list[date] = []

    run = pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar(calls))

    assert calls == [ASOF]
    assert run.events == ()
    assert pipeline.bias_notes(consulted=False)[0] in run.warnings


def test_the_shortlist_is_capped_by_the_risk_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is read from config, so changing the config changes the answer."""
    pairs = tuple(
        bias(pair, conviction=Conviction.HIGH)
        for pair in ("EURUSD", "GBPJPY", "AUDNZD")
    )
    _stub(monkeypatch, biases=pairs)

    def shortlisted(cap: int) -> int:
        config = Config(risk=RiskConfig(max_concurrent_positions=cap))
        run = pipeline.build_run(config, ASOF, read_calendar=_no_calendar([]))
        return len(run.shortlist)

    assert shortlisted(3) == 3
    assert shortlisted(1) == 1


def test_the_run_carries_its_scores_and_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scores = (score("USD", 1.2, 1), score("EUR", -0.2, 2), score("JPY", 0.1, 3))
    _stub(monkeypatch, scores=scores)

    run = pipeline.build_run(Config(), ASOF, read_calendar=_no_calendar([]))

    assert run.asof == ASOF
    assert run.currencies == scores
