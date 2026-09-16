"""Tests for `BasePillar.compute`, the method every pillar runs inside.

This is where a pillar's four stages are run in order and turned into one
`PillarScore` per currency. The properties worth testing are the ones whose
failure would be invisible in the number: a currency dropped from the result
instead of marked absent, an observation used before it was published, and the
two facts about how the run was scaled going missing so that two runs on
different scales look comparable.

`compute` is tested through a small pillar double for everything it controls
itself, and through the real `MonetaryPillar` for the publication-visibility
rule, which is `_extract`'s and which a double would only be testing against
itself.

Nothing here reaches the network.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest

from fbe.config import ScoringConfig
from fbe.pillars.base import BasePillar
from fbe.pillars.monetary import MonetaryPillar
from fbe.types import Frequency, Observation, PillarName, PillarScore

ASOF = date(2026, 6, 30)
UNIVERSE: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY")

FRESH_PERIOD = date(2026, 4, 1)
"""90 days before ``asof``. Past the 45-day monthly publication lag, so an
observation carrying no ``released_at`` is visible, and inside the 90-day full
-freshness step of a 270-day allowance, so it is not discounted either."""

STALE_PERIOD = date(2026, 1, 1)
"""180 days before ``asof``, which is on the freshness ramp rather than at
either end of it."""


def _observation(
    indicator: str,
    currency: str,
    value: float,
    *,
    period: date = FRESH_PERIOD,
    released_at: datetime | None = None,
    frequency: Frequency = Frequency.MONTHLY,
) -> Observation:
    return Observation(
        indicator=indicator,
        currency=currency,
        value=value,
        period=period,
        source="test",
        series_id="X",
        unit="percent",
        frequency=frequency,
        released_at=released_at,
    )


class _Pillar(BasePillar):
    """A two-component pillar whose extraction is trivial and honest.

    ``_extract`` applies the visibility rule the base docstring states, so the
    double does not quietly admit something a real pillar would refuse. It is
    the simplest implementation of that rule rather than a copy of any pillar's.
    """

    name = PillarName.GROWTH
    requires: Sequence[str] = ("gdp_yoy", "retail_sales_yoy")
    headline_component = "gdp_yoy"

    @property
    def component_weights(self) -> Mapping[str, float]:
        return {"gdp_yoy": 0.6, "retail_sales_yoy": 0.4}

    @property
    def component_indicators(self) -> Mapping[str, tuple[str, ...]]:
        return {"gdp_yoy": ("gdp_yoy",), "retail_sales_yoy": ("retail_sales_yoy",)}

    def _extract(
        self,
        observations: Sequence[Observation],
        currencies: Sequence[str],
        asof: date,
    ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
        return {
            currency: {
                indicator: tuple(
                    observation
                    for observation in observations
                    if observation.currency == currency
                    and observation.indicator == indicator
                    and _visible(observation, asof)
                )
                for indicator in self.requires
            }
            for currency in currencies
        }

    def _transform(
        self,
        extracted: Mapping[str, Mapping[str, Sequence[Observation]]],
        asof: date,
    ) -> Mapping[str, Mapping[str, float | None]]:
        return {
            currency: {
                component: (
                    series[-1].value if (series := indicators[component]) else None
                )
                for component in self.component_weights
            }
            for currency, indicators in extracted.items()
        }


def _visible(observation: Observation, asof: date) -> bool:
    """The rule from the `_extract` docstring, stated once for the double."""
    from fbe.pillars.base import DEFAULT_PUBLICATION_LAG_DAYS

    if observation.released_at is not None:
        return observation.released_at.date() <= asof
    lag = DEFAULT_PUBLICATION_LAG_DAYS[observation.frequency]
    return (observation.period - asof).days + lag <= 0


def _spread(values: Mapping[str, float]) -> list[Observation]:
    """One observation per currency for both of the double's components."""
    return [
        _observation(indicator, currency, value + offset)
        for currency, value in values.items()
        for offset, indicator in ((0.0, "gdp_yoy"), (0.5, "retail_sales_yoy"))
    ]


SPREAD: Mapping[str, float] = {"USD": 4.0, "EUR": 2.0, "GBP": 1.0, "JPY": -1.0}


@pytest.fixture
def config() -> ScoringConfig:
    return ScoringConfig()


# --- criterion 1: every currency comes back ---------------------------------


def test_every_currency_asked_for_is_scored() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert set(scores) == set(UNIVERSE)


def test_a_currency_with_nothing_is_present_rather_than_missing() -> None:
    """The scorer detects thin coverage by ``z is None``, not by an absent key.

    A dropped key reads downstream as a currency nobody scored, which is an
    absence of opportunity rather than an absence of data.
    """
    universe = (*UNIVERSE, "CHF", "CAD", "NZD")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    assert set(scores) == set(universe)
    for currency in ("CHF", "CAD", "NZD"):
        assert scores[currency].z is None, currency


def test_a_currency_absent_from_the_observations_is_still_keyed() -> None:
    scores = _Pillar().compute([], UNIVERSE, ASOF)

    assert set(scores) == set(UNIVERSE)


def test_every_score_names_its_own_pillar_and_currency() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    for currency, score in scores.items():
        assert score.currency == currency
        assert score.pillar is PillarName.GROWTH


# --- criterion 2: the absent case -------------------------------------------


def test_an_unscorable_currency_carries_the_absence_markers() -> None:
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    chf = scores["CHF"]
    assert chf.raw is None
    assert chf.z is None
    assert chf.score == pytest.approx(0.0, abs=1e-12)


def test_an_unscorable_currency_says_which_indicator_was_missing() -> None:
    """Naming the indicator is the difference between a report a reader can act
    on and one that says data was thin."""
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    notes = scores["CHF"].notes
    assert "gdp_yoy" in notes
    assert "retail_sales_yoy" in notes


def test_the_note_names_only_the_indicators_that_were_missing() -> None:
    """CHF holds ``retail_sales_yoy`` at 0.4 of the sub-weight, below the floor.

    So it cannot be scored, and what a reader needs is which series to go and
    find, not a list of everything the pillar consumes.
    """
    observations = [*_spread(SPREAD), _observation("retail_sales_yoy", "CHF", 1.0)]
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(observations, universe, ASOF)

    notes = scores["CHF"].notes
    assert scores["CHF"].z is None
    assert "gdp_yoy" in notes
    assert "retail_sales_yoy" not in notes


def test_a_currency_holding_enough_of_the_sub_weight_still_scores() -> None:
    """The converse: one component of two is above the floor, so it is scored."""
    observations = [*_spread(SPREAD), _observation("gdp_yoy", "CHF", 1.0)]
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(observations, universe, ASOF)

    assert scores["CHF"].z is not None
    assert scores["CHF"].notes == ""


def test_a_scored_currency_carries_no_absence_note() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].notes == ""


def test_the_absent_case_still_carries_the_configured_weight() -> None:
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    assert scores["CHF"].weight == pytest.approx(scores["USD"].weight, abs=1e-12)


# --- criterion 3: publication, not period -----------------------------------


YIELD_PERIOD = date(2026, 6, 28)
"""Two days before ``asof``. The 2-year yield's allowance is 10 days, so a
period further back would expire the component and the pillar would answer
absent for reasons that have nothing to do with the rule under test."""


def _monetary_set() -> list[Observation]:
    """A MONETARY run the real pillar can score, every input stamped.

    Four of the five components, worth 0.85 of the sub-weight, which clears the
    `MIN_COMPONENT_WEIGHT` floor of 0.5. ``cpi_yoy`` is left out so the set
    stays small; its absence costs only ``real_policy_rate``.
    """
    stamped = datetime(2026, 6, 29, tzinfo=UTC)
    return [
        _observation(
            indicator,
            currency,
            value + offset,
            period=YIELD_PERIOD,
            released_at=stamped,
        )
        for currency, value in SPREAD.items()
        for offset, indicator in (
            (0.0, "policy_rate"),
            (0.1, "yield_2y"),
            (0.2, "yield_2y_chg_1m"),
            (0.3, "yield_2y_chg_3m"),
        )
    ]


def test_the_monetary_fixture_actually_scores() -> None:
    """The guard on the three tests below, which would pass vacuously on None."""
    scores = MonetaryPillar().compute(_monetary_set(), UNIVERSE, ASOF)

    assert all(scores[currency].z is not None for currency in UNIVERSE)


def test_an_observation_released_after_the_asof_takes_no_part() -> None:
    """Through the real MONETARY pillar, whose ``_extract`` owns the rule.

    The period precedes ``asof`` and the release follows it, which is the shape
    that makes a backtest read off the answer sheet: a run dated 15 April that
    filters on period keeps a Q1 GDP print published on 25 April.
    """
    pillar = MonetaryPillar()
    published = _monetary_set()
    unpublished = _observation(
        "yield_2y",
        "USD",
        99.0,
        period=date(2026, 6, 29),
        released_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    without = pillar.compute(published, UNIVERSE, ASOF)
    with_future = pillar.compute([*published, unpublished], UNIVERSE, ASOF)

    assert without["USD"].z is not None
    assert with_future["USD"].z == pytest.approx(without["USD"].z, abs=1e-12)


# Deliberately not proportional to `SPREAD`. Every other component in
# `_monetary_set` is a fixed offset from it, so all of them z-score identically
# and a blend of identical z-scores is that same z: a proportional addition
# would be admitted and still move nothing, and the test below would pass
# whether or not it had been let in.
CPI: Mapping[str, float] = {"USD": 0.5, "EUR": 3.0, "GBP": 0.2, "JPY": 1.0}


def test_an_observation_published_on_the_asof_is_admitted() -> None:
    """The converse, so the test above cannot pass by excluding everything."""
    pillar = MonetaryPillar()
    published = _monetary_set()
    on_the_day = [
        _observation(
            "cpi_yoy",
            currency,
            value,
            period=FRESH_PERIOD,
            released_at=datetime(ASOF.year, ASOF.month, ASOF.day, tzinfo=UTC),
        )
        for currency, value in CPI.items()
    ]

    without = pillar.compute(published, UNIVERSE, ASOF)
    with_today = pillar.compute([*published, *on_the_day], UNIVERSE, ASOF)

    # Admitted into the arithmetic. The diagnostics cannot serve as the proof
    # here: see the test below on what a freshness key does and does not mean.
    assert with_today["USD"].z != pytest.approx(without["USD"].z, abs=1e-9)


def test_a_freshness_key_does_not_mean_the_component_entered_the_blend() -> None:
    """Recording the current behaviour, which is easy to misread.

    ``real_policy_rate`` is built from two indicators, and `component_freshness`
    reports a component the currency has *any* of them for, taking the lowest
    factor. ``_transform`` is stricter: with no ``cpi_yoy`` the component is
    ``None`` and the blend never sees it. So a ``freshness.real_policy_rate``
    key can sit beside a component that contributed nothing.

    That is `component_freshness`'s own documented contract and this issue reads
    it rather than reimplementing it, so the behaviour is pinned here rather
    than changed. A reader taking the key as proof the component was blended
    would be wrong, and nothing else says so.
    """
    scores = MonetaryPillar().compute(_monetary_set(), UNIVERSE, ASOF)

    assert "freshness.real_policy_rate" in scores["USD"].diagnostics


def test_an_unpublished_observation_does_not_reach_the_inputs() -> None:
    pillar = MonetaryPillar()
    unpublished = _observation(
        "yield_2y",
        "USD",
        99.0,
        period=date(2026, 6, 29),
        released_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    scores = pillar.compute([*_monetary_set(), unpublished], UNIVERSE, ASOF)

    assert all(
        observation.released_at is None or observation.released_at.date() <= ASOF
        for observation in scores["USD"].inputs
    )
    assert 99.0 not in [observation.value for observation in scores["USD"].inputs]


# --- criterion 4: the per-component freshness factors ------------------------


def test_diagnostics_carry_one_freshness_key_per_component() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    keys = {key for key in scores["USD"].diagnostics if key.startswith("freshness.")}
    assert keys == {"freshness.gdp_yoy", "freshness.retail_sales_yoy"}


def test_a_component_with_no_data_has_no_freshness_key() -> None:
    """Absent and stale are different facts, and `component_freshness` says so."""
    observations = [*_spread(SPREAD), _observation("gdp_yoy", "CHF", 1.0)]
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(observations, universe, ASOF)

    keys = {key for key in scores["CHF"].diagnostics if key.startswith("freshness.")}
    assert keys == {"freshness.gdp_yoy"}


def test_the_freshness_values_are_the_factors_the_pillar_computes() -> None:
    pillar = _Pillar()
    observations = _spread(SPREAD)
    extracted = pillar._extract(observations, UNIVERSE, ASOF)
    expected = pillar.component_freshness(extracted["USD"], ASOF)

    scores = pillar.compute(observations, UNIVERSE, ASOF)

    for component, factor in expected.items():
        assert scores["USD"].diagnostics[f"freshness.{component}"] == pytest.approx(
            factor, abs=1e-12
        )


def test_a_stale_component_reads_lower_than_a_fresh_one() -> None:
    """Otherwise the keys could all be 1.0 and every test above would pass."""
    fresh = _spread(SPREAD)
    stale = [
        _observation(
            observation.indicator,
            observation.currency,
            observation.value,
            period=date(2024, 1, 1),
        )
        if observation.indicator == "gdp_yoy"
        else observation
        for observation in fresh
    ]

    scores = _Pillar().compute(stale, UNIVERSE, ASOF)

    assert (
        scores["USD"].diagnostics["freshness.gdp_yoy"]
        < scores["USD"].diagnostics["freshness.retail_sales_yoy"]
    )


# --- criterion 5: the divisor path, as a field ------------------------------


def test_the_divisor_path_is_recorded_on_every_score() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert all(score.blend_divisor_path for score in scores.values())


def test_a_short_history_records_the_run_local_path(config: ScoringConfig) -> None:
    pillar = _Pillar(blend_sd_history=(0.7,))

    scores = pillar.compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].blend_divisor_path == "run_local"


def test_a_long_history_records_the_rolling_path(config: ScoringConfig) -> None:
    pillar = _Pillar(blend_sd_history=(0.7,) * config.min_restandardisation_runs)

    scores = pillar.compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].blend_divisor_path == "rolling"


def test_the_two_paths_are_told_apart_by_the_field_alone(
    config: ScoringConfig,
) -> None:
    """No string matching on ``notes`` anywhere in this assertion."""
    short = _Pillar(blend_sd_history=(0.7,))
    long = _Pillar(blend_sd_history=(0.7,) * config.min_restandardisation_runs)

    from_short = short.compute(_spread(SPREAD), UNIVERSE, ASOF)["USD"]
    from_long = long.compute(_spread(SPREAD), UNIVERSE, ASOF)["USD"]

    assert from_short.blend_divisor_path != from_long.blend_divisor_path


def test_the_absent_case_records_the_path_too() -> None:
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    assert scores["CHF"].blend_divisor_path == scores["USD"].blend_divisor_path


def test_the_field_defaults_to_empty_on_a_bare_score() -> None:
    """An empty string means the pillar recorded no path, per the ruling."""
    bare = PillarScore(
        pillar=PillarName.GROWTH,
        currency="USD",
        raw=None,
        z=0.0,
        score=0.0,
        weight=0.15,
        asof=ASOF,
    )

    assert bare.blend_divisor_path == ""


# --- criterion 6: how much of the run rests on an assumption ----------------


def test_the_assumed_lag_count_is_the_inputs_with_no_release_stamp() -> None:
    """A mixed set: one component stamped, one not."""
    observations = [
        _observation(
            "gdp_yoy", currency, value, released_at=datetime(2026, 6, 2, tzinfo=UTC)
        )
        for currency, value in SPREAD.items()
    ] + [
        _observation("retail_sales_yoy", currency, value + 0.5)
        for currency, value in SPREAD.items()
    ]

    scores = _Pillar().compute(observations, UNIVERSE, ASOF)

    assert scores["USD"].diagnostics["assumed_lag_inputs"] == pytest.approx(1.0)


def test_a_fully_stamped_run_assumes_nothing() -> None:
    observations = [
        _observation(
            indicator,
            currency,
            value + offset,
            released_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        for currency, value in SPREAD.items()
        for offset, indicator in ((0.0, "gdp_yoy"), (0.5, "retail_sales_yoy"))
    ]

    scores = _Pillar().compute(observations, UNIVERSE, ASOF)

    assert scores["USD"].diagnostics["assumed_lag_inputs"] == pytest.approx(0.0)


def test_a_wholly_unstamped_run_says_every_input_was_assumed() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].diagnostics["assumed_lag_inputs"] == pytest.approx(2.0)


def test_the_assumed_lag_count_is_present_even_when_zero() -> None:
    """A key that disappears when the answer is zero cannot be read as zero."""
    observations = [
        _observation(
            indicator,
            currency,
            value + offset,
            released_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        for currency, value in SPREAD.items()
        for offset, indicator in ((0.0, "gdp_yoy"), (0.5, "retail_sales_yoy"))
    ]

    scores = _Pillar().compute(observations, UNIVERSE, ASOF)

    assert "assumed_lag_inputs" in scores["USD"].diagnostics


# --- criterion 7: notes is prose, and the docstrings say so -----------------


def test_the_notes_field_is_documented_as_prose_nothing_parses() -> None:
    source = inspect.getsource(PillarScore)
    tree = ast.parse(inspect.getsource(inspect.getmodule(PillarScore)))
    assert "notes" in source

    documented = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PillarScore":
            for index, statement in enumerate(node.body):
                target = getattr(statement, "target", None)
                if isinstance(target, ast.Name) and target.id == "notes":
                    following = node.body[index + 1]
                    if isinstance(following, ast.Expr) and isinstance(
                        following.value, ast.Constant
                    ):
                        documented = str(following.value.value)
    assert documented, "PillarScore.notes has no docstring"
    assert "prose" in documented.lower()
    assert "parse" in documented.lower()


@pytest.mark.parametrize(
    "method",
    ["compute", "blend_divisor", "_extract"],
)
def test_the_three_docstrings_point_at_the_field_not_at_notes(method: str) -> None:
    """Each of the three used to require one of these facts inside ``notes``."""
    doc = getattr(BasePillar, method).__doc__ or ""

    assert "blend_divisor_path" in doc or "assumed_lag_inputs" in doc


# --- criterion 8: the aggregator never reads diagnostics --------------------


def test_nothing_in_scoring_reads_diagnostics() -> None:
    """The aggregator must see freshness only through the weight, once."""
    import fbe.scoring

    tree = ast.parse(inspect.getsource(fbe.scoring))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "diagnostics"
    ]
    assert reads == []


# --- criterion 9: the run's own blend standard deviation --------------------


def test_the_run_blend_standard_deviation_reaches_the_caller() -> None:
    """It is the next run's history, so it has to survive the call."""
    pillar = _Pillar()

    pillar.compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert pillar.last_blend_sd is not None
    assert pillar.last_blend_sd > 0.0


def test_the_recorded_standard_deviation_is_this_run_not_the_history() -> None:
    wide = _Pillar(blend_sd_history=(5.0,) * 80)
    narrow = _Pillar(blend_sd_history=(5.0,) * 80)

    wide.compute(_spread(SPREAD), UNIVERSE, ASOF)
    flat = _spread({"USD": 1.0, "EUR": 1.0, "GBP": 1.0, "JPY": 1.0})
    narrow.compute(flat, UNIVERSE, ASOF)

    assert wide.last_blend_sd != pytest.approx(narrow.last_blend_sd, abs=1e-9)


def test_a_run_that_blends_nothing_clears_the_previous_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two recorded facts belong to the run that produced them.

    A pillar overriding ``_normalise`` without blending, which POSITIONING and
    RISK both do, would otherwise hand out the path and the standard deviation
    of whatever this instance last blended. That is a stale fact in a field a
    reader trusts to describe the scores beside it.
    """
    pillar = _Pillar()
    pillar.compute(_spread(SPREAD), UNIVERSE, ASOF)
    assert pillar.last_blend_divisor_path == "run_local"
    assert pillar.last_blend_sd is not None

    monkeypatch.setattr(
        pillar,
        "_normalise",
        lambda components: dict.fromkeys(components, 0.5),
    )
    scores = pillar.compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert pillar.last_blend_divisor_path == ""
    assert pillar.last_blend_sd is None
    assert all(score.blend_divisor_path == "" for score in scores.values())


def test_a_pillar_that_has_not_run_records_nothing() -> None:
    assert _Pillar().last_blend_sd is None
    assert _Pillar().last_blend_divisor_path == ""


# --- the score's own fields -------------------------------------------------


def test_the_headline_component_reaches_raw_in_its_natural_unit() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].raw == pytest.approx(4.0, abs=1e-12)


def test_the_score_is_the_z_clipped_onto_the_band(config: ScoringConfig) -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    for score in scores.values():
        if score.z is not None:
            assert score.score == pytest.approx(
                BasePillar.clip_and_scale(score.z, config.score_clip), abs=1e-12
            )


def test_the_score_never_leaves_the_band(config: ScoringConfig) -> None:
    extreme = _spread({"USD": 500.0, "EUR": 0.0, "GBP": 0.0, "JPY": 0.0})

    scores = _Pillar().compute(extreme, UNIVERSE, ASOF)

    for score in scores.values():
        assert abs(score.score) <= config.score_clip + 1e-12


def test_the_clip_actually_bites_when_the_divisor_is_small(
    config: ScoringConfig,
) -> None:
    """Extreme inputs alone cannot reach the clip, which is why this is separate.

    A cross-sectional z over n currencies cannot exceed ``sqrt(n - 1)``, so with
    the G10 the largest possible z is about 2.65 against a clip of 3.0: no
    arrangement of readings makes the clip fire. What does is the blend divisor.
    A pillar whose components have historically agreed carries a small rolling
    divisor, and dividing an ordinary blend by it puts the z well past the band.
    Without this test the clip could be dropped from `compute` and every other
    test here would still pass.
    """
    tight = _Pillar(blend_sd_history=(0.05,) * config.min_restandardisation_runs)

    scores = tight.compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert max(abs(score.z or 0.0) for score in scores.values()) > config.score_clip
    for score in scores.values():
        assert abs(score.score) <= config.score_clip + 1e-12


def test_a_clipped_score_keeps_the_unclipped_z(config: ScoringConfig) -> None:
    """The band is what the aggregator consumes; ``z`` stays as computed.

    Clipping both would lose the fact that the currency was off the scale, which
    is exactly what a reader checking a dominant pillar wants to see.
    """
    tight = _Pillar(blend_sd_history=(0.05,) * config.min_restandardisation_runs)

    scores = tight.compute(_spread(SPREAD), UNIVERSE, ASOF)

    strongest = max(scores.values(), key=lambda score: abs(score.z or 0.0))
    assert abs(strongest.z or 0.0) > abs(strongest.score)


def test_every_score_carries_the_pillars_configured_weight(
    config: ScoringConfig,
) -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    for score in scores.values():
        assert score.weight == pytest.approx(
            config.weights[PillarName.GROWTH], abs=1e-12
        )


def test_the_inputs_are_the_observations_the_currency_contributed() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert {observation.currency for observation in scores["USD"].inputs} == {"USD"}
    assert len(scores["USD"].inputs) == 2


def test_the_inputs_include_a_series_extracted_under_another_key() -> None:
    """``_extract``'s return type ties no key to ``requires``.

    A pillar may index a helper series it needs for a derived component, and
    those observations still aged the score and still rested on an assumed
    publication lag. Indexing the inputs by ``requires`` would drop them from
    both counts while the score they produced stayed.
    """

    class _WithHelper(_Pillar):
        def _extract(
            self,
            observations: Sequence[Observation],
            currencies: Sequence[str],
            asof: date,
        ) -> Mapping[str, Mapping[str, Sequence[Observation]]]:
            base = super()._extract(observations, currencies, asof)
            return {
                currency: {
                    **indicators,
                    "helper": tuple(
                        observation
                        for observation in observations
                        if observation.currency == currency
                        and observation.indicator == "helper"
                    ),
                }
                for currency, indicators in base.items()
            }

    helpers = [_observation("helper", currency, 1.0) for currency in UNIVERSE]

    scores = _WithHelper().compute([*_spread(SPREAD), *helpers], UNIVERSE, ASOF)

    indicators = {observation.indicator for observation in scores["USD"].inputs}
    assert "helper" in indicators
    assert scores["USD"].diagnostics["assumed_lag_inputs"] == pytest.approx(3.0)


def test_the_staleness_days_are_measured_from_the_newest_period() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert scores["USD"].staleness_days == (ASOF - FRESH_PERIOD).days


def test_every_score_carries_the_run_date() -> None:
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    assert all(score.asof == ASOF for score in scores.values())


def test_the_cross_section_is_what_ranks_the_currencies() -> None:
    """A higher reading scores higher, and the ordering survives the pipeline."""
    scores = _Pillar().compute(_spread(SPREAD), UNIVERSE, ASOF)

    ordered = sorted(UNIVERSE, key=lambda c: scores[c].score, reverse=True)
    assert ordered == ["USD", "EUR", "GBP", "JPY"]


def test_nothing_is_substituted_for_a_currency_with_no_data() -> None:
    universe = (*UNIVERSE, "CHF")

    scores = _Pillar().compute(_spread(SPREAD), universe, ASOF)

    assert scores["CHF"].z is None
    assert scores["CHF"].raw is None
    assert scores["CHF"].inputs == ()
