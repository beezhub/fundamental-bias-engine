"""What an unknown calendar is worth: conviction, the report, and the journal.

Issue #43 gave the engine a way to say "the calendar does not cover this
moment". It did not say what that is worth to a trade, and three places
answered it by accident:

- `fbe.bias.EventHorizonGuard` returned ``bool``, so a guard that could not see
  the next 24 hours returned ``False``, which is the same value a guard that
  looked and found a quiet day returns. An unseen calendar bought full
  conviction, which is more than a seen one buys.
- The Markdown report labelled every marker on a tradeable pair "Not checked",
  so a failed fetch and an offline run read identically to anyone holding the
  page.
- `fbe.journal.TradeRecord` carried ``blackout_checked: bool``, which collapses
  "the guard was consulted and the window was clear" together with "the guard
  was blind and the trade was taken anyway" and with "no guard ran". Proposal
  #2's own falsification is a count of the middle one, and a boolean cannot
  produce it.

The owner ruled option 3 on #24: fail closed for central bank rate decisions,
fail open with a visible marker for statistical releases. The fail-closed half
needs a rate-decision calendar and there is not one, so this file pins the
fail-open half and the vocabulary, which is the scope the architect narrowed
this issue to on 2026-09-14.

One rule sits under all of it and every test here is a case of it: **an unseen
calendar must never be worth more than a seen one.**

Every fixture is built in this file and no calendar is constructed. The guards
are injected callables and each test supplies its own.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbe.bias import (
    UNCHECKED_SUFFIX,
    UNKNOWN_SUFFIX,
    apply_filters,
    build_pair_biases,
    conviction_for,
)
from fbe.config import Config, ScoringConfig
from fbe.journal import BlackoutCheck, TradeRecord, append, load
from fbe.report import render_report, write_report
from fbe.types import (
    Conviction,
    CurrencyScore,
    Direction,
    PairBias,
    PillarName,
    PillarScore,
)
from fbe.universe import G10

ASOF = date(2026, 9, 14)
CONFIG = Config()
SCORING = ScoringConfig()

UNKNOWN_MARKER = "event" + UNKNOWN_SUFFIX
UNCHECKED_MARKER = "event" + UNCHECKED_SUFFIX

WIDE = 2.80
"""A spread above ``min_spread_high`` (2.50), so the base tier is HIGH.

Written relative to the threshold in the assertion rather than as a bare
number, so a test that passes because the tier was already LOW cannot hide.
"""


def score(code: str, composite: float, coverage: float = 1.0) -> CurrencyScore:
    """One currency, scored so that nothing except the horizon demotes it.

    All seven pillars carry the composite, so two legs scored in opposite
    directions agree completely and the agreement cap stays out of the way.
    Every other demotion is kept clear too: full coverage and low dispersion.
    A fixture that tripped one of those would pass this file's tests for a
    reason that has nothing to do with the calendar.
    """
    return CurrencyScore(
        currency=code,
        composite=composite,
        pillars={
            pillar: PillarScore(
                pillar=pillar,
                currency=code,
                raw=composite,
                z=composite,
                score=composite,
                weight=0.30,
                asof=ASOF,
                staleness_days=1,
                inputs=(),
                notes="",
                diagnostics={},
            )
            for pillar in PillarName
        },
        asof=ASOF,
        rank=1,
        dispersion=0.10,
        coverage=coverage,
    )


def universe(**composites: float) -> tuple[CurrencyScore, ...]:
    """All ten G10 scores, with the named currencies set and the rest at zero.

    `build_pair_biases` builds every entry in ``ALL_PAIRS`` and refuses a
    currency it has no score for, so a test cannot supply only the leg it cares
    about. The unnamed currencies sit at zero, which keeps every pair that is
    not the one under test at a spread of zero and therefore uninteresting.
    """
    return tuple(score(code, composites.get(code, 0.0)) for code in sorted(G10))


def bias(
    *,
    pair: str = "EURUSD",
    tradeable: bool = True,
    blockers: tuple[str, ...] = (),
) -> PairBias:
    return PairBias(
        pair=pair,
        base=pair[:3],
        quote=pair[3:],
        spread=1.60,
        direction=Direction.LONG,
        conviction=Conviction.MEDIUM,
        asof=ASOF,
        base_score=0.80,
        quote_score=-0.80,
        agreement=0.82,
        tradeable=tradeable,
        blockers=blockers,
    )


def record(**overrides: object) -> TradeRecord:
    """One journal record, with only the fields these tests read set."""
    fields: dict[str, object] = {
        "trade_id": "2026-09-14-eurusd-1",
        "pair": "EURUSD",
        "direction": Direction.LONG,
        "opened_at": datetime(2026, 9, 14, 7, 0, tzinfo=UTC),
        "entry": 1.0850,
        "stop": 1.0800,
        "units": 1000.0,
        "lots": 0.01,
        "risk_amount": 40.00,
        "risk_fraction": 0.02,
        "account_balance_at_entry": 2000.0,
        "conviction": Conviction.HIGH,
        "base_score": 1.2,
        "quote_score": -0.4,
        "spread_score": 1.6,
        "config_digest": "a1b2c3d4e5f6",
    }
    fields.update(overrides)
    return TradeRecord(**fields)  # type: ignore[arg-type]


# --- an unseen calendar is never worth more than a seen one -------------------


def test_an_unknown_horizon_caps_conviction_at_low() -> None:
    """The rule, stated at the one function that grades a call.

    ``None`` means the horizon guard was asked and could not tell. A position
    opened today may be held through a repricing the model has not seen, and
    that is true whether the release was found or merely could not be ruled
    out.
    """
    assert SCORING.min_spread_high <= WIDE

    tier = conviction_for(
        spread=WIDE,
        agreement_fraction=0.90,
        coverage_fraction=1.0,
        dispersion_value=0.10,
        event_within_24h=None,
        config=SCORING,
    )

    assert tier is Conviction.LOW


def test_an_unknown_horizon_is_worth_exactly_what_a_known_event_is_worth() -> None:
    """Not less, and above all not more.

    Less would mean an outage costing more than the release it could not see,
    which would make a failed fetch the most expensive thing that can happen to
    a run. More is the defect: it is what ``bool`` did, because a guard that
    could not answer had only ``False`` to return.
    """
    graded = {
        answer: conviction_for(
            spread=WIDE,
            agreement_fraction=0.90,
            coverage_fraction=1.0,
            dispersion_value=0.10,
            event_within_24h=answer,
            config=SCORING,
        )
        for answer in (False, True, None)
    }

    assert graded[None] is graded[True]
    assert graded[False] is Conviction.HIGH


def test_a_quiet_horizon_is_still_the_only_uncapped_answer() -> None:
    """``False`` has to keep meaning what it meant: looked, and found nothing."""
    tier = conviction_for(
        spread=WIDE,
        agreement_fraction=0.90,
        coverage_fraction=1.0,
        dispersion_value=0.10,
        event_within_24h=False,
        config=SCORING,
    )

    assert tier is Conviction.HIGH


def test_the_cap_survives_the_route_from_the_guard_to_the_pair() -> None:
    """The unit above proves the rule; this proves the wiring carries it.

    `build_pair_biases` asks the guard once per currency and hands the answer
    to `conviction_for`. A pipeline that coerced the guard's answer to ``bool``
    on the way would pass the unit test above and still award HIGH here.
    """
    scores = universe(EUR=1.40, USD=-1.40)

    def blind_on_the_euro(currency: str, asof: date) -> bool | None:
        return None if currency == "EUR" else False

    def quiet(currency: str, asof: date) -> bool | None:
        return False

    blind = {
        row.pair: row
        for row in build_pair_biases(scores, CONFIG, ASOF, blind_on_the_euro)
    }
    seen = {row.pair: row for row in build_pair_biases(scores, CONFIG, ASOF, quiet)}

    assert seen["EURUSD"].spread >= SCORING.min_spread_high
    assert seen["EURUSD"].conviction is Conviction.HIGH
    assert blind["EURUSD"].conviction is Conviction.LOW


def test_a_seen_leg_is_not_capped_by_an_unseen_one_elsewhere() -> None:
    """The cap is per leg, not per run.

    A guard blind on one currency must not demote every pair in the universe:
    that converts one failed lookup into a run-wide downgrade, which is the
    banner-blindness cost ADR 0002 names in its own consequences section.
    """
    scores = universe(EUR=1.40, USD=-1.40, JPY=-1.40)

    def blind_on_the_euro(currency: str, asof: date) -> bool | None:
        return None if currency == "EUR" else False

    rows = {
        row.pair: row
        for row in build_pair_biases(scores, CONFIG, ASOF, blind_on_the_euro)
    }

    assert rows["EURUSD"].conviction is Conviction.LOW
    assert rows["USDJPY"].conviction is not Conviction.LOW


# --- the fail-open policy, which is the owner's ruling ------------------------


def test_unknown_coverage_leaves_a_statistical_release_tradeable() -> None:
    """Option 3's fail-open half, pinned so a later change is deliberate.

    The trader reviews the calendar themselves in the daily routine, and
    refusing all 28 pairs on one failed fetch would cost a full trading day
    over an outage that may clear on the next run.
    """

    def blind(currency: str, asof: date) -> tuple[tuple[str, ...], str | None]:
        return (), "cached through 2026-09-11T23:59:00+00:00"

    filtered = apply_filters(
        bias(),
        {"EUR": score("EUR", 0.80), "USD": score("USD", -0.80)},
        CONFIG,
        ASOF,
        calendar_guard=blind,
        cost_ratio=0.01,
    )

    assert filtered.tradeable is True
    assert any(entry.startswith(UNKNOWN_MARKER) for entry in filtered.blockers)


def test_the_unknown_marker_carries_why_rather_than_only_that() -> None:
    """A marker a trader cannot act on is a marker that will be ignored.

    "Check the calendar by hand" and "this resolves itself on the next run"
    are different responses, and the reason is what separates them.
    """

    def blind(currency: str, asof: date) -> tuple[tuple[str, ...], str | None]:
        return (), "Request Denied"

    filtered = apply_filters(
        bias(),
        {"EUR": score("EUR", 0.80), "USD": score("USD", -0.80)},
        CONFIG,
        ASOF,
        calendar_guard=blind,
        cost_ratio=0.01,
    )

    reasons = [entry for entry in filtered.blockers if entry.startswith(UNKNOWN_MARKER)]
    assert reasons
    assert all("Request Denied" in entry for entry in reasons)


# --- the marker reaching the trader, which is ADR 0002 rule 3 -----------------


def test_the_markdown_tells_an_unknown_calendar_from_an_unchecked_one(
    tmp_path: Path,
) -> None:
    """The two states had one label between them on the shortlist.

    "Not checked" is true of an offline run and false of a failed fetch, and
    the failed fetch is the one that needs a person to look at a calendar
    within the hour. Printing the same four words over both is the collapse
    this issue exists to undo, one layer above the one #43 undid.
    """
    unknown = render_shortlist(tmp_path, (f"{UNKNOWN_MARKER}: Request Denied",))
    unchecked = render_shortlist(tmp_path, (UNCHECKED_MARKER,))

    assert "Not checked" in labels_of(unchecked)
    assert "Not checked" not in labels_of(unknown)
    assert "Request Denied" in unknown


def test_the_pair_table_shows_the_marker_on_a_tradeable_row(tmp_path: Path) -> None:
    """A row that reads an unqualified "yes" is the defect, not the marker."""
    body = render_shortlist(tmp_path, (f"{UNKNOWN_MARKER}: Request Denied",))

    assert "Request Denied" in body


def test_the_json_sidecar_keeps_the_two_markers_apart(tmp_path: Path) -> None:
    """A distinction that survives the render and dies in the file is no good.

    The sidecar is what ``--compare`` reads and what any later analysis of
    "how often was the calendar blind" has to work from.
    """
    report = report_with((f"{UNKNOWN_MARKER}: Request Denied", UNCHECKED_MARKER))
    markdown = write_report(report, tmp_path)
    payload = json.loads(markdown.with_suffix(".json").read_text(encoding="utf-8"))

    blockers = payload["pairs"][0]["blockers"]
    assert f"{UNKNOWN_MARKER}: Request Denied" in blockers
    assert UNCHECKED_MARKER in blockers


# --- the journal, which is where the override has to be countable ------------


def test_the_three_blackout_states_are_three_values() -> None:
    """Checked and clear, checked and blind, and no guard at all.

    A boolean holds two of the three, and the one it loses is the one proposal
    #2 asks to be counted.
    """
    assert len(set(BlackoutCheck)) == 3
    assert BlackoutCheck.CLEAR is not BlackoutCheck.UNKNOWN
    assert BlackoutCheck.UNKNOWN is not BlackoutCheck.NOT_RUN


def test_a_record_that_never_ran_the_guard_does_not_read_as_clear() -> None:
    """The default has to be the honest one, not the convenient one."""
    assert record().blackout_check is BlackoutCheck.NOT_RUN


@pytest.mark.parametrize("state", list(BlackoutCheck))
def test_every_blackout_state_round_trips_through_the_file(
    state: BlackoutCheck, tmp_path: Path
) -> None:
    """Written as its value and read back as the member, not as a string.

    A state read back as ``"unknown"`` compares False against the member, so a
    count of forced entries finds none and reads that as a disciplined month.
    """
    path = tmp_path / "journal.jsonl"
    append(record(blackout_check=state), path)

    (back,) = load(path=path)

    assert back.blackout_check is state


def test_a_forced_entry_is_countable_against_the_runs_that_fired(
    tmp_path: Path,
) -> None:
    """Proposal #2's falsification, made runnable.

    "If the override is used on most of the runs where the state fires, the
    guard has been converted into a prompt." That is a count over the book, and
    it needs the blind entries to be separable from the clear ones.
    """
    path = tmp_path / "journal.jsonl"
    append(record(trade_id="a", blackout_check=BlackoutCheck.CLEAR), path)
    append(record(trade_id="b", blackout_check=BlackoutCheck.UNKNOWN), path)
    append(record(trade_id="c", blackout_check=BlackoutCheck.UNKNOWN), path)

    book = load(path=path)
    forced = [row for row in book if row.blackout_check is BlackoutCheck.UNKNOWN]

    assert len(book) == 3
    assert [row.trade_id for row in forced] == ["b", "c"]


def test_an_unknown_state_is_refused_as_a_bare_string(tmp_path: Path) -> None:
    """The same guard the other two enum fields already carry.

    Writing a string produces a line ``load`` refuses, which makes every record
    already in the file unreadable, so it is caught on the way in.
    """
    path = tmp_path / "journal.jsonl"

    with pytest.raises(ValueError, match="BlackoutCheck"):
        append(record(blackout_check="unknown"), path)


def test_a_state_the_vocabulary_does_not_hold_is_named_on_read(
    tmp_path: Path,
) -> None:
    """A typo in a hand-edited file must not read back as a valid record."""
    path = tmp_path / "journal.jsonl"
    append(record(blackout_check=BlackoutCheck.CLEAR), path)
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    payload["blackout_check"] = "cleared"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="blackout_check"):
        load(path=path)


# --- helpers -----------------------------------------------------------------


def report_with(blockers: tuple[str, ...]) -> object:
    """A one-pair, one-idea report whose pair carries ``blockers``."""
    from fbe.types import BiasReport, TradeIdea

    row = bias(tradeable=True, blockers=blockers)
    return BiasReport(
        asof=ASOF,
        generated_at=datetime(2026, 9, 14, 5, 30, tzinfo=UTC),
        currencies=(score("EUR", 0.80), score("USD", -0.80)),
        pairs=(row,),
        events=(),
        shortlist=(
            TradeIdea(
                bias=row,
                size=None,
                blackout_until=None,
                rationale="The rates gap is the widest in the universe.",
            ),
        ),
        warnings=(),
        config_digest="abc123",
    )


def render_shortlist(tmp_path: Path, blockers: tuple[str, ...]) -> str:
    """The rendered Markdown for a report whose only pair carries ``blockers``."""
    return render_report(report_with(blockers))  # type: ignore[arg-type]


def labels_of(markdown: str) -> list[str]:
    """Every ``Label: ...`` prefix in the shortlist section of a rendered report.

    The assertion is about the words the reader sees before the marker, not
    about the marker, which is already distinct by construction. A test written
    on the marker alone passes against a template that prints "Not checked"
    over both states, which is the defect.
    """
    marker = "## 3."
    section = markdown.split(marker, 1)[-1].split("## 4.", 1)[0]
    return [
        line.split(":", 1)[0].strip()
        for line in section.splitlines()
        if ":" in line and not line.startswith("|")
    ]
