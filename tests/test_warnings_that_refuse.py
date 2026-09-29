"""Which sizing warnings stop a trade, and which only describe one: #189.

Section 8 of `docs/risk-and-execution.md` told the owner to tick the sizing box
only when ``warnings`` is empty, and closed with "If any box is unchecked, there
is no trade". `position_size` puts two different kinds of thing in that tuple.
One is a refusal, returned with a size of zero. The other is information about a
size that was produced successfully, and the tuple is where
`engineering-standards` says that information belongs: "size what was asked for
and say what is wrong with it, rather than silently fixing it".

Measured against `main` at `0256f86`, on the issue's own settings, `RiskConfig()`
at R2,000 and `BrokerConfig()` with its 0.01 lot step, EURUSD entry 1.0850,
``USDZAR`` 18.50, HIGH conviction:

* 167 stop distances between 5.0 and 39.9 pips produce a size. The rounding
  shortfall warning fires on 81 of them, and `realised_risk_amount` is inside
  the plan's R20 to R40 on all 81. Half of every sizeable setup was refused by
  the checklist for being the right size.
* The unconfirmed-profile warning rides every result under the packaged broker
  profile, so out of the box the box could not be ticked at all.

`fbe.bias` already solved this problem for `PairBias.blockers`: a `BLOCKERS` map
of kind to whether it blocks, a longest-matching-prefix rule, and a `blocking`
helper that raises on a marker matching no kind. `WARNINGS` and `refusing` are
the same mechanism for the same reason, and the two maps stay separate because
they answer different questions about different objects.

Nothing here reaches the network. Every figure is computed by the test from the
packaged defaults rather than copied from the issue.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

import fbe.risk as risk_module
from fbe.bias import UNCHECKED_SUFFIX
from fbe.config import BrokerConfig, RiskConfig
from fbe.risk import (
    BROKER_UNCONFIRMED,
    SIZING_SHORTFALL_TOLERANCE,
    WARNING_RISK_FRACTION_CLAMPED,
    WARNING_SIZE_BELOW_MINIMUM,
    WARNING_SIZE_SHORTFALL,
    WARNING_SPREAD_UNCHECKED,
    WARNING_STOP_TIGHT,
    WARNING_STOP_ZERO,
    WARNINGS,
    position_size,
    refusing,
    risk_fraction_for,
)
from fbe.types import Conviction, PositionSize

RATES = {"USDZAR": 18.50}
"""The one rate a EURUSD ticket on a rand account needs, at the issue's value."""

ENTRY = 1.0850
"""EURUSD entry from the issue's failure scenario."""

PIP = 0.0001
"""One EURUSD pip, so a stop distance can be written in pips and read as one."""

DOC = Path(__file__).resolve().parents[1] / "docs" / "risk-and-execution.md"


def sized(
    stop_pips: float,
    *,
    broker: BrokerConfig | None = None,
    config: RiskConfig | None = None,
    risk_fraction: float | None = None,
) -> PositionSize:
    """Size EURUSD at HIGH conviction with a stop ``stop_pips`` below entry."""
    settings = config if config is not None else RiskConfig()
    fraction = (
        risk_fraction
        if risk_fraction is not None
        else risk_fraction_for(Conviction.HIGH, settings)
    )
    return position_size(
        "EURUSD",
        ENTRY,
        ENTRY - stop_pips * PIP,
        settings,
        RATES,
        broker if broker is not None else BrokerConfig(),
        risk_fraction=fraction,
    )


def of_kind(size: PositionSize, kind: str) -> list[str]:
    """Every warning on ``size`` whose text begins with ``kind``."""
    return [warning for warning in size.warnings if warning.startswith(kind)]


# ----------------------------------------------------------------------
# The map itself
# ----------------------------------------------------------------------


def test_the_two_refusals_are_the_only_refusals() -> None:
    """Criteria 1 and 2, as the ruling classifies them.

    Below the broker minimum and entry equal to stop are the two cases that
    return no size. The rounding shortfall and `spread:unchecked` describe a
    size that was produced, and the ruling names both as not refusing. The
    remaining three are settled elsewhere and asserted in the next test.
    """
    assert WARNINGS[WARNING_SIZE_BELOW_MINIMUM] is True
    assert WARNINGS[WARNING_STOP_ZERO] is True
    assert WARNINGS[WARNING_SIZE_SHORTFALL] is False
    assert WARNINGS[WARNING_SPREAD_UNCHECKED] is False
    assert WARNING_SPREAD_UNCHECKED == "spread" + UNCHECKED_SUFFIX


def test_the_three_the_ruling_left_open_are_classified_where_they_were_settled() -> (
    None
):
    """The other three kinds, each settled by something already in the tree.

    `MIN_STOP_SPREAD_MULTIPLE`'s docstring says of the tight-stop warning that
    "It warns and does not refuse", and gives the reason: the spread it is
    measured against is indicative and the owner watching their own terminal
    knows better than the profile does.

    `BROKER_UNCONFIRMED`'s docstring says it rides every result including the
    refusals, and `BrokerConfig()` ships unconfirmed on purpose, so classifying
    it as refusing would refuse every trade out of the box.

    The clamp reports that the requested fraction was brought inside the
    configured band. The size that comes back is inside the plan's band by
    construction, which is the thing the next box down checks.
    """
    assert WARNINGS[WARNING_STOP_TIGHT] is False
    assert WARNINGS[BROKER_UNCONFIRMED] is False
    assert WARNINGS[WARNING_RISK_FRACTION_CLAMPED] is False


def test_every_warning_position_size_emits_carries_a_declared_kind() -> None:
    """Criterion 2, read as a completeness check rather than a spot check.

    Walks every ``warnings.append`` inside `position_size` and reads the first
    fragment of the message. A literal must start with a declared kind; an
    interpolated leading name must be a constant whose value is one. A warning
    added later without a kind fails here rather than reaching `refusing`, where
    it would raise in front of the owner mid-ticket.
    """
    tree = ast.parse(inspect.getsource(risk_module))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "position_size"
    )

    leading: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "append"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "warnings"):
            continue
        (message,) = node.args
        first = message.values[0] if isinstance(message, ast.JoinedStr) else message
        if isinstance(first, ast.Constant):
            leading.append(str(first.value))
        elif isinstance(first, ast.FormattedValue) and isinstance(
            first.value, ast.Name
        ):
            leading.append(getattr(risk_module, first.value.id))
        else:
            raise AssertionError(f"unreadable warning opening: {ast.dump(first)}")

    assert len(leading) == len(WARNINGS), leading
    for opening in leading:
        assert any(opening.startswith(kind) for kind in WARNINGS), opening


def test_no_kind_shadows_another_today_and_the_helper_would_cope_if_one_did() -> None:
    """The longest-matching-prefix rule, which `BLOCKERS` documents and needs.

    No key here is a prefix of another, so the rule is inert on today's map and
    a helper taking the first match would agree. `BLOCKERS` has four such pairs
    and taking the first match there reads three non-blocking markers as hard
    blocks, so the rule is written into this helper before the map grows into
    that shape rather than after.
    """
    for kind in WARNINGS:
        others = [other for other in WARNINGS if other != kind]
        assert not any(other.startswith(kind) for other in others), kind

    shadowed = {"size": True, "size:shortfall": False}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(risk_module, "WARNINGS", shadowed)
        assert risk_module.refusing(("size:shortfall: rounded down",)) == ()
        assert risk_module.refusing(("size: something else",)) == (
            "size: something else",
        )


def test_an_unrecognised_kind_raises_rather_than_defaulting() -> None:
    """Criterion 6. Neither answer is safe as a default.

    Defaulting to refusing throws away trades for a marker nobody classified.
    Defaulting to not refusing lets a real refusal disappear from the one list
    the owner reads before committing money. `bias.blocking` raises for the same
    reason and this mirrors it.
    """
    with pytest.raises(ValueError) as caught:
        refusing(("liquidity:thin: the book is empty",))

    assert "liquidity:thin" in str(caught.value)
    assert "WARNINGS" in str(caught.value)


def test_the_helper_keeps_order_and_returns_only_the_refusals() -> None:
    """A mixed tuple comes back as its refusing subset, in the order given."""
    entries = (
        f"{BROKER_UNCONFIRMED}: profile unconfirmed",
        f"{WARNING_SIZE_BELOW_MINIMUM}: rounds below the minimum",
        f"{WARNING_SIZE_SHORTFALL}: rounding left less at risk",
        f"{WARNING_STOP_ZERO}: entry equals stop",
    )

    assert refusing(entries) == (entries[1], entries[3])
    assert refusing(()) == ()


# ----------------------------------------------------------------------
# The failure scenario, recomputed rather than copied
# ----------------------------------------------------------------------


def test_the_fifteen_pip_trade_is_the_size_the_plan_asks_for() -> None:
    """Criterion 4, the case the checklist refused.

    R2,000 at the HIGH fraction of 2% intends R40. One micro lot on a 15-pip
    EURUSD stop puts R27.75 on the book, which is inside the plan's R20 to R40,
    and the shortfall warning fires because 27.75 is more than a fifth below 40.
    Every figure is computed here from `RiskConfig` and `BrokerConfig`, not
    copied from the issue, because the issue's sweep predates #177 and #204.
    """
    settings = RiskConfig()
    size = sized(15.0)

    assert risk_fraction_for(Conviction.HIGH, settings) == settings.risk_per_trade_max
    assert size.risk_amount == pytest.approx(40.00)
    assert size.lots == pytest.approx(0.01)
    assert size.realised_risk_amount == pytest.approx(27.75)
    assert 20.0 <= size.realised_risk_amount <= 40.0

    shortfall = of_kind(size, WARNING_SIZE_SHORTFALL)
    assert len(shortfall) == 1
    assert size.realised_risk_amount < size.risk_amount * (
        1.0 - SIZING_SHORTFALL_TOLERANCE
    )
    assert refusing(size.warnings) == ()


def test_the_shortfall_warning_covers_half_the_tradeable_stop_range() -> None:
    """Why this is not a corner case, measured rather than asserted.

    Sweeping the stop from 5.0 to 39.9 pips in tenths: of the distances that
    produce a size at all, the shortfall fires on about half, and every one of
    those is inside the plan's R20 to R40. The refused half is the
    tighter-stopped half, which is the half the plan's own setup rules favour.

    The bound is deliberately loose. The exact count moves whenever the lot
    step, the balance or the conviction ladder moves, and a test asserting 81
    would fail on a change that did not break anything.
    """
    produced = [
        size
        for size in (sized(stop / 10.0) for stop in range(50, 400))
        if size.units > 0.0
    ]
    flagged = [size for size in produced if of_kind(size, WARNING_SIZE_SHORTFALL)]

    assert len(produced) > 100
    assert 0.35 < len(flagged) / len(produced) < 0.65
    assert all(20.0 <= size.realised_risk_amount <= 40.0 for size in flagged)
    assert all(refusing(size.warnings) == () for size in flagged)


def test_the_packaged_profile_puts_a_warning_on_every_single_ticket() -> None:
    """The half of the defect the issue's sweep does not capture.

    `BrokerConfig()` ships with ``confirmed`` false, on purpose, because nobody
    has checked its lot geometry against a broker contract specification. So the
    unconfirmed marker rides every result, and "warnings is empty" was not a
    box that failed half the time. It was a box that could never be ticked at
    all until the owner confirmed a profile.
    """
    size = sized(21.0)

    assert BrokerConfig().confirmed is False
    assert of_kind(size, BROKER_UNCONFIRMED)
    assert refusing(size.warnings) == ()
    assert size.units > 0.0


# ----------------------------------------------------------------------
# The refusals still refuse
# ----------------------------------------------------------------------


def test_a_size_below_the_broker_minimum_refuses_and_carries_no_size() -> None:
    """Criterion 5. Nothing here loosens what is actually refused.

    A 400-pip stop at R40 of intended risk computes a fraction of a micro lot,
    which rounds down to zero and is below the 0.01 minimum. The result carries
    zero units and zero lots, and the helper says so independently, so a
    renderer reading the tuple reaches the same answer as one reading the size.
    """
    size = sized(400.0)

    assert size.units == 0.0
    assert size.lots == 0.0
    assert size.realised_risk_amount == 0.0
    assert refusing(size.warnings) == tuple(of_kind(size, WARNING_SIZE_BELOW_MINIMUM))
    assert len(refusing(size.warnings)) == 1


def test_an_entry_equal_to_its_stop_refuses() -> None:
    """The second refusal. No distance, so no size can be derived from it."""
    size = sized(0.0)

    assert size.units == 0.0
    assert size.stop_distance_pips == 0.0
    assert refusing(size.warnings) == tuple(of_kind(size, WARNING_STOP_ZERO))
    assert len(refusing(size.warnings)) == 1


def test_a_refusal_is_visible_through_the_helper_and_through_the_size() -> None:
    """The two readings agree on every case `position_size` can return.

    The ruling observes that a refusal returns zero units, and classifying by
    that would have worked today. The map is keyed by kind instead, because a
    helper reading the size would agree with the map until the first refusing
    warning that arrives on a non-zero size, and then disagree silently.
    """
    cases = (sized(400.0), sized(0.0), sized(15.0), sized(21.0))

    for size in cases:
        assert bool(refusing(size.warnings)) == (size.units == 0.0)


# ----------------------------------------------------------------------
# The other warnings still fire, and still do not refuse
# ----------------------------------------------------------------------


def test_an_unlisted_spread_is_a_statement_about_the_check_not_the_trade() -> None:
    """`spread:unchecked` on a profile that lists no spread for the pair.

    This is the half of the defect that is not live yet. `BrokerConfig()` lists
    a typical spread for all 28 pairs, so the branch is quiet today. It fires
    the moment the owner replaces the packaged profile with one sampled from
    their own terminal, which `BrokerConfig`'s own docstring instructs, and a
    profile covering only the pairs they sampled would have refused every other
    pair for a reason whose text says it is not about the trade.
    """
    blind = BrokerConfig(typical_spread_pips={})
    size = sized(21.0, broker=blind)

    assert of_kind(size, WARNING_SPREAD_UNCHECKED)
    assert UNCHECKED_SUFFIX in WARNING_SPREAD_UNCHECKED
    assert refusing(size.warnings) == ()
    assert size.units > 0.0


def test_a_stop_inside_the_spread_warns_and_does_not_refuse() -> None:
    """The tight-stop warning, on a profile whose spread is wide enough to reach.

    A 5-pip stop against a 4-pip typical spread is inside the 2x multiple. The
    trade is still sized: the profile's spreads are indicative and sampled at
    one time of day, and the owner watching their own terminal is the one who
    can tell whether this particular stop is really inside the round turn.
    """
    wide = BrokerConfig(typical_spread_pips={"EURUSD": 4.0})
    size = sized(5.0, broker=wide)

    assert of_kind(size, WARNING_STOP_TIGHT)
    assert refusing(size.warnings) == ()
    assert size.units > 0.0


def test_a_clamped_risk_fraction_warns_and_does_not_refuse() -> None:
    """The clamp. The size that comes back is inside the configured band."""
    settings = RiskConfig()
    size = sized(15.0, risk_fraction=0.05)

    assert size.risk_fraction == settings.risk_per_trade_max
    assert of_kind(size, WARNING_RISK_FRACTION_CLAMPED)
    assert refusing(size.warnings) == ()


# ----------------------------------------------------------------------
# The checklist the code is read through
# ----------------------------------------------------------------------


def section_eight() -> str:
    """The pre-trade checklist, from its heading to the end of the document.

    It is the last section, so there is no following heading to stop at. Sliced
    rather than read whole, because several assertions below are about what the
    checklist does not say and the rest of the document says some of it.
    """
    text = DOC.read_text(encoding="utf-8")
    return text[text.index("## 8. Pre-trade checklist") :]


def size_boxes() -> str:
    """The **Size** group of the checklist, which this issue rewrites."""
    section = section_eight()
    start = section.index("**Size**")
    return section[start : section.index("**Limits**", start)]


def test_the_size_boxes_no_longer_ask_for_an_empty_warnings_list() -> None:
    """Criterion 3. The box that refused half the tradeable range is gone.

    Asserted as the absence of the instruction rather than the absence of the
    word, because the boxes still have to talk about warnings: the point is that
    the owner reads them, not that they are absent.
    """
    boxes = size_boxes()

    assert "`warnings` is empty" not in boxes
    assert "warnings is empty" not in boxes
    assert "warnings" in boxes


def test_the_size_boxes_still_refuse_a_size_below_the_broker_minimum() -> None:
    """The half of the old box that was right, which must survive the rewrite.

    Rounding up to reach the minimum breaches the cap by a little on every
    trade forever, which is the fifth row of the prime directive's table.
    """
    boxes = size_boxes()

    assert "below the broker minimum, the trade does not happen" in " ".join(
        boxes.split()
    )
    assert "rounded up" in boxes


def test_the_size_boxes_read_warnings_the_way_the_blockers_box_reads_blockers() -> None:
    """Criterion 3's "same shape as the box above them".

    The `blockers` box tells the owner to read every marker and says which kinds
    are not a real block. The `warnings` box now does the same, and names the
    helper, so the rule lives in one place and the document points at it rather
    than restating it.
    """
    section = section_eight()
    boxes = size_boxes()

    assert "Read every one of them" in section
    assert "refusing" in boxes


def test_nothing_in_the_checklist_claims_the_risk_per_trade_changed() -> None:
    """Criterion 7, on the side a test can reach.

    The plan's band is 1% to 2% and this issue does not touch it. The
    configured values are asserted here so a rewrite of the prose that also
    moved a number would fail rather than read as an editorial change.
    """
    settings = RiskConfig()

    assert settings.risk_per_trade_min == 0.01
    assert settings.risk_per_trade_max == 0.02
    assert SIZING_SHORTFALL_TOLERANCE == 0.20
    assert "R20 and R40" in size_boxes()


def test_the_document_no_longer_says_an_empty_warnings_list_is_a_pass() -> None:
    """Section 1 made the same claim as section 8 and has to move with it.

    It is the stated reason `position_size` raises on a malformed input rather
    than warning about it. The raise is right and stays; the reason given for it
    cannot be a rule that no longer exists.
    """
    text = DOC.read_text(encoding="utf-8")

    assert not re.search(r"empty\s+`?warnings`?\s+list\s+as\s+a\s+pass", text)
    assert re.search(r"None is a fact about the\s+trade", text)
