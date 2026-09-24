"""``BiasReport.config_digest`` says what the digest covers and what it does not.

`Config.digest` is scoped to the settings that change what a run computes: the
whole of `ScoringConfig`, and `RiskConfig` without ``account_balance``. Three
groups are left out. `BiasReport.config_digest` is the field a reader of a
stored report meets, and it described the value only as a hash of the effective
config, so nothing told that reader that two reports carrying the same digest
may have been sized on different balances, run against different data settings
and produced under different broker profiles.

That gap matters because of what the digest is used for. ``--compare`` treats a
digest change as a re-weighting and reports the two runs as not comparable, so
the field is read for a decision and not for display. A reader who believes it
covers the whole configuration will draw the opposite conclusion from a matching
digest to the one they should.

The tests come in two halves. The wording half pins what the contract says. The
behaviour half pins that each claim in it is true of `Config.digest`, by moving
one field at a time and watching the digest. Pinning only the words would let
the contract drift from the function again, which is the defect; pinning only
the behaviour would leave the reader where they started.

The wording is transcribed from `Config.digest`, which the architect named as
the definition of record on 2026-09-12, and deliberately does not repeat its
per-exclusion reasoning. `test_the_restatement_stays_short` is what holds that
condition.

Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import replace

from fbe.config import Config
from fbe.types import BiasReport

SUPERSEDED = "Hash of the effective config"
"""The phrase the field carried, kept so the test and the issue look at the same
characters rather than at two approximations of them."""


def _config_digest_doc() -> str:
    """The docstring written under ``config_digest`` in `BiasReport`'s source.

    Read out of the source rather than from ``__doc__``, because an attribute
    docstring on a dataclass field is not kept at runtime.
    """
    source = inspect.getsource(BiasReport)
    match = re.search(
        r'^\s+config_digest:[^\n]*\n\s+"""(.*?)"""',
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "no attribute docstring under BiasReport.config_digest"
    return match.group(1)


def test_the_docstring_names_what_the_digest_covers() -> None:
    """Criterion 1, the scope half.

    Naming the two configs is the whole of the positive claim. Without it the
    exclusions below have nothing to be exclusions from.
    """
    doc = _config_digest_doc()

    assert "ScoringConfig" in doc, doc
    assert "RiskConfig" in doc, doc


def test_the_docstring_names_what_the_digest_leaves_out() -> None:
    """Criterion 1, the exclusions half.

    ``account_balance`` and ``DataConfig`` are the two the issue names.
    ``BrokerConfig`` joined them in `Config.digest` after this issue was
    written, and criterion 2 sends the wording to `Config.digest` rather than
    to the issue, so all three are named.
    """
    doc = _config_digest_doc()

    assert "account_balance" in doc, doc
    assert "DataConfig" in doc, doc
    assert "BrokerConfig" in doc, doc


def test_the_docstring_points_at_the_definition_of_record() -> None:
    """Criterion 2, and the architect's condition of 2026-09-12.

    Duplicating the covered and not-covered lists onto this field gives two
    places that drift the next time the digest changes, which is the failure
    this issue is an instance of. The field says enough to stop a reader being
    wrong and points at `Config.digest` for the rest.
    """
    doc = _config_digest_doc()

    assert "Config.digest" in doc, doc


def test_the_docstring_keeps_the_purpose_and_says_who_reads_it() -> None:
    """Criterion 2. The original clause was right and is extended, not replaced.

    "so a report can be tied to the weights that produced it" is the field's
    purpose in one clause and the architect asked for it to stay. What it did
    not say is that ``--compare`` acts on the field, which is what makes this a
    contract read for correctness rather than a line of provenance.
    """
    doc = _config_digest_doc()

    assert "compare" in doc, doc
    assert "weights" in doc, doc


def test_the_superseded_phrase_does_not_survive() -> None:
    """Criterion 3, from the other side.

    Leaving the old sentence in place above the new ones would pass every test
    above and leave the contract saying both a narrow and a broad thing, and a
    reader picks whichever they already believed.
    """
    assert SUPERSEDED.lower() not in _config_digest_doc().lower()


def test_the_restatement_is_far_shorter_than_the_definition_of_record() -> None:
    """The architect's condition, in the only form a test can hold it.

    "Two sentences, not the full list", and no reasoning for the individual
    exclusions, because that belongs where the decision was made.
    `Config.digest` runs to 34 lines carrying that reasoning, and a copy of it
    here is the drift this issue exists to close.

    The bar is relative rather than an absolute line count. An absolute one
    says nothing about duplication: it is a number someone picks, and the next
    author tunes it. Measured against the reference, a copy sits at 1.0 and
    the restatement is at 0.38 by characters and 0.32 by lines, so half is a
    bar a duplicate cannot pass and a two-sentence restatement is not near.
    """
    doc = _config_digest_doc()
    reference = inspect.getdoc(Config.digest) or ""

    assert len(doc) < len(reference) / 2, (len(doc), len(reference))
    assert len(doc.splitlines()) < len(reference.splitlines()) / 2, doc


def _moved(**groups: object) -> str:
    """The digest of a config with one group replaced.

    Built with `dataclasses.replace` from `Config()` so every test moves
    exactly one thing and the rest is the shipped default. No value in
    `ScoringConfig` or `RiskConfig` is changed in the source; these are
    test-local copies, which is the only way to check that the digest responds
    to a field rather than that it equals a constant.
    """
    return replace(Config(), **groups).digest()


def test_moving_the_account_balance_does_not_move_the_digest() -> None:
    """The first exclusion the docstring claims, checked against the function.

    This is the one with teeth for a reader of a stored report: two reports
    with the same digest were sized against whatever the balance was that day,
    and the digest will not tell them apart.
    """
    base = Config()
    tripled = replace(base.risk, account_balance=base.risk.account_balance * 3.0)

    assert base.risk.account_balance != tripled.account_balance
    assert _moved(risk=tripled) == base.digest()


def test_moving_the_data_config_does_not_move_the_digest() -> None:
    """The second exclusion. Nothing in `DataConfig` changes a score.

    ``offline`` is the field used here because it is a plain bool with an
    obvious opposite, so the test cannot pass by accident of the value chosen.
    """
    base = Config()
    flipped = replace(base.data, offline=not base.data.offline)

    assert base.data.offline != flipped.offline
    assert _moved(data=flipped) == base.digest()


def test_moving_the_broker_profile_does_not_move_the_digest() -> None:
    """The third exclusion, and the one the issue's criteria predate.

    ``confirmed`` flips once, the first time the owner places a minimum-size
    trade and confirms the fill. Folding `BrokerConfig` into the digest would
    make every comparison across that date report as not comparable, which is
    the defect ``--compare`` exists to avoid.
    """
    base = Config()
    flipped = replace(base.broker, confirmed=not base.broker.confirmed)

    assert base.broker.confirmed != flipped.confirmed
    assert _moved(broker=flipped) == base.digest()


def test_moving_a_scoring_threshold_does_move_the_digest() -> None:
    """The positive claim. Without this the three tests above pass on a constant.

    A digest that never moves would satisfy every exclusion test and be
    useless, so one covered field from each covered config is moved here and in
    the test below.
    """
    base = Config()
    widened = replace(base.scoring, max_dispersion=base.scoring.max_dispersion + 0.05)

    assert _moved(scoring=widened) != base.digest()


def test_moving_a_risk_limit_does_move_the_digest() -> None:
    """The same for `RiskConfig`, whose covered half is everything but balance.

    The percentages are the rule and shape every `PositionSize`, so a report
    sized under a different cap must not look like the same run.
    """
    base = Config()
    tighter = replace(
        base.risk, risk_per_trade_max=base.risk.risk_per_trade_max - 0.005
    )

    assert _moved(risk=tighter) != base.digest()
