"""An absent pillar's note says what was missing, never an age it did not have.

Issue #223. ``ScoringConfig.max_staleness_days`` had stopped bounding any ramp
when ADR 0014 derived the ramp from the leg, and its only readers were the
absent-pillar sentinels, each reporting ``max_staleness_days + 1``. That number
reached one place, the expiry note, so a pillar with no data at all printed
"past its staleness allowance at 46 days" beside the real reason, on live
output for CAD, EUR and CHF on 2026-10-02. Ruled on the issue on 2026-09-23:
stop quoting an age for a pillar that has no data, retire the field, and let
``z is None`` be the one marker.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

from fbe.config import ConfigError, ScoringConfig, load_config
from fbe.scoring import apply_staleness_penalty
from fbe.types import PillarName, PillarScore
from tests.test_pillar_blend import Double

ASOF = date(2026, 10, 2)
DAYS = re.compile(r"\d+ days")


def test_scoring_config_carries_no_sentinel_field() -> None:
    assert "max_staleness_days" not in {f.name for f in fields(ScoringConfig)}


def test_a_config_file_setting_it_is_refused(tmp_path: Path) -> None:
    """Refused rather than ignored: a key that does nothing is a setting the
    operator believes is applied."""
    path = tmp_path / "config.yaml"
    path.write_text("scoring:\n  max_staleness_days: 60\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="max_staleness_days"):
        load_config(path)


def test_an_absent_pillar_after_the_penalty_quotes_no_age() -> None:
    """The reason is what was missing. No number of days, real or invented."""
    pillar = Double({"alpha": 1.0})
    absent = pillar.missing_score(
        "CAD", ASOF, notes="employment could not score CAD: no usable alpha"
    )

    penalised = apply_staleness_penalty(absent, pillar.config, 0.0)

    assert penalised.notes == "employment could not score CAD: no usable alpha"
    assert not DAYS.search(penalised.notes)
    assert penalised.z is None
    assert penalised.weight == 0.0


def test_a_scored_pillar_that_expired_still_quotes_its_real_age() -> None:
    """The control. A pillar that had data and aged out did measure an age, and
    the note keeps saying so: only the invented age goes."""
    stale = PillarScore(
        pillar=PillarName.EMPLOYMENT,
        currency="CAD",
        raw=4.1,
        z=0.3,
        score=0.3,
        weight=0.10,
        asof=ASOF,
        staleness_days=212,
    )

    penalised = apply_staleness_penalty(stale, ScoringConfig(), 0.0)

    assert "212 days" in penalised.notes
    assert penalised.z is None


def test_missing_score_does_not_read_config_for_its_age() -> None:
    """The default is no longer ``max_staleness_days + 1``. Absence is marked by
    ``z is None`` and a freshness factor of 0.0, whatever the age field holds."""
    source = inspect.getsource(Double.missing_score)
    absent = Double({"alpha": 1.0}).missing_score("AUD", ASOF)

    assert "config" not in source.split('"""')[-1]
    assert absent.z is None
    assert absent.freshness_factor == 0.0


def test_aging_an_empty_set_is_refused_rather_than_invented() -> None:
    """There is no newest observation to age. `missing_score` is the path for a
    pillar with nothing, and a pillar that raises here is marked failed and
    absent by `score_currencies`, with this reason."""
    with pytest.raises(ValueError, match="missing_score"):
        Double({"alpha": 1.0}).staleness_days([], ASOF)


def test_the_type_says_the_age_means_nothing_when_z_is_none() -> None:
    """A docstring, not a contract change: no field, type or order moves."""
    doc = " ".join(inspect.getsource(PillarScore).split())

    assert "Meaningless when ``z`` is ``None``" in doc
