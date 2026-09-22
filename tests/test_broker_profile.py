"""The broker profile carries a confirmation state, and that state is legible.

Issue #90. Three surfaces have to agree, and none of them may read a size built
on lot values nobody has checked as if it were built on verified ones: the
config that holds the profile, `fbe doctor` that reports it, and the sized
ticket that a run produces.

The defect this guards against is the one ADR 0002
(`docs/decisions/0002-representing-not-known.md`) names against
`DEFAULT_BROKER` directly: a position sized on an unconfirmed
`min_lot` is indistinguishable from one sized on a verified one. The old
`DEFAULT_BROKER` was a module constant used as a parameter default, so a caller
that forgot to pass a broker sized against it silently. The fix is a
`BrokerConfig` on `Config` carrying `confirmed`, `position_size` taking it with
no default, and a `broker:unconfirmed` marker enumerated once and rendered.

No network anywhere. Every object is built in this file.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

import fbe
from fbe.config import (
    DEFAULT_TYPICAL_SPREAD_PIPS,
    BrokerConfig,
    Config,
    RiskConfig,
    load_config,
)
from fbe.report import render_report
from fbe.risk import BROKER_UNCONFIRMED, position_size
from fbe.types import (
    BiasReport,
    Conviction,
    Direction,
    PairBias,
    TradeIdea,
)

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent
ASOF = date(2026, 9, 22)
GENERATED_AT = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)

# The rates for a USD-quoted size, through the ZAR pivot.
RATES: dict[str, float] = {"USDZAR": 18.50}


# --- the config section ------------------------------------------------------


def test_confirmed_defaults_to_false() -> None:
    """A config that names no broker gets an unconfirmed profile, not an absent
    one and not a silently confirmed one."""
    assert Config().broker.confirmed is False


def test_a_config_file_that_omits_the_broker_section_is_unconfirmed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk:\n  account_balance: 2000.0\n")

    assert load_config(path).broker.confirmed is False


def test_the_broker_section_is_read_from_the_file(tmp_path: Path) -> None:
    """The values reach the resolved config, so nothing sizes from a constant."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "broker:\n"
        "  name: acme-fx\n"
        "  min_lot: 0.001\n"
        "  lot_step: 0.001\n"
        "  confirmed: true\n"
    )
    broker = load_config(path).broker

    assert broker.name == "acme-fx"
    assert broker.min_lot == 0.001
    assert broker.confirmed is True


def test_setting_the_spread_map_in_a_file_is_refused_clearly(tmp_path: Path) -> None:
    """`typical_spread_pips` is the second mapping field on a config section, and
    the file reader's mapping path is specific to pillar weights. Without a gate
    it would read the spreads as weights and fail with `unknown pillar 'EURUSD'`.
    It is set from the code default, so the refusal names the field instead."""
    path = tmp_path / "config.yaml"
    path.write_text("broker:\n  typical_spread_pips:\n    EURUSD: 0.5\n")

    with pytest.raises(Exception, match="typical_spread_pips"):
        load_config(path)


@pytest.mark.parametrize("field", ["min_lot", "lot_step", "contract_size"])
@pytest.mark.parametrize("value", [0.0, -0.01])
def test_validate_rejects_non_positive_lot_geometry(field: str, value: float) -> None:
    """A zero or negative one either divides by zero or rounds every size to
    nothing while still returning a ticket, so it is refused at load with the
    field named."""
    config = dataclasses.replace(
        Config(), broker=dataclasses.replace(Config().broker, **{field: value})
    )
    problems = config.validate()

    assert any(f"broker.{field}" in problem for problem in problems), problems


def test_validate_accepts_an_unconfirmed_profile() -> None:
    """Unconfirmed is a valid, expected state, not a config problem: it is
    reported at the ticket and by doctor, never refused at load."""
    assert Config().validate() == []


# --- the digest --------------------------------------------------------------


def test_confirming_the_broker_does_not_change_the_digest() -> None:
    """`confirmed` flips from false to true the first time the owner confirms
    the fill. Nothing has been re-weighted, so `--compare` must not read every
    run across that date as not comparable."""
    unconfirmed = Config()
    confirmed = dataclasses.replace(
        unconfirmed, broker=dataclasses.replace(unconfirmed.broker, confirmed=True)
    )

    assert unconfirmed.digest() == confirmed.digest()


def test_changing_the_lot_geometry_does_not_change_the_digest() -> None:
    """The whole broker section is out of the digest, not only `confirmed`. A
    broker profile changes what can be sized, not what anything scores."""
    base = Config()
    moved = dataclasses.replace(
        base, broker=dataclasses.replace(base.broker, min_lot=0.001, name="other")
    )

    assert base.digest() == moved.digest()


# --- the marker on the ticket ------------------------------------------------


def _config() -> RiskConfig:
    return RiskConfig(account_balance=2000.0)


def _unconfirmed() -> BrokerConfig:
    return BrokerConfig(confirmed=False)


def _confirmed() -> BrokerConfig:
    return BrokerConfig(confirmed=True)


def test_an_unconfirmed_profile_marks_every_result() -> None:
    size = position_size(
        "EURUSD", 1.0850, 1.0825, _config(), RATES, broker=_unconfirmed()
    )

    assert any(warning.startswith(BROKER_UNCONFIRMED) for warning in size.warnings)


def test_a_confirmed_profile_leaves_no_broker_marker() -> None:
    size = position_size(
        "EURUSD", 1.0850, 1.0825, _config(), RATES, broker=_confirmed()
    )

    assert not any(warning.startswith(BROKER_UNCONFIRMED) for warning in size.warnings)


def test_a_refused_size_still_carries_the_marker() -> None:
    """The marker rides every result, including the refusals: a ticket refused
    because the profile's own minimum is too coarse was still refused on values
    nobody confirmed."""
    coarse = BrokerConfig(min_lot=0.5, lot_step=0.5, confirmed=False)

    size = position_size("EURUSD", 1.0850, 1.0825, _config(), RATES, broker=coarse)

    assert size.units == 0.0
    assert any(warning.startswith(BROKER_UNCONFIRMED) for warning in size.warnings)


def test_the_marker_names_the_lot_geometry() -> None:
    """So a reader can see which numbers are unconfirmed without opening the
    config."""
    size = position_size(
        "EURUSD", 1.0850, 1.0825, _config(), RATES, broker=_unconfirmed()
    )
    marker = next(w for w in size.warnings if w.startswith(BROKER_UNCONFIRMED))

    assert "min_lot" in marker and "lot_step" in marker and "contract_size" in marker


def test_position_size_requires_a_broker() -> None:
    """No default: a caller that forgets the profile would otherwise size
    against a module constant, which is the second-home defect this removed."""
    with pytest.raises(TypeError):
        position_size("EURUSD", 1.0850, 1.0825, _config(), RATES)  # type: ignore[call-arg]


# --- the marker reaches both rendered surfaces -------------------------------


def _pair() -> PairBias:
    return PairBias(
        pair="EURUSD",
        base="EUR",
        quote="USD",
        spread=1.42,
        direction=Direction.LONG,
        conviction=Conviction.MEDIUM,
        asof=ASOF,
        base_score=0.81,
        quote_score=-0.61,
        agreement=0.71,
    )


def _sized_idea() -> TradeIdea:
    size = position_size(
        "EURUSD", 1.0850, 1.0825, _config(), RATES, broker=_unconfirmed()
    )
    return TradeIdea(bias=_pair(), size=size, rationale="widest rates gap")


def _report() -> BiasReport:
    return BiasReport(
        asof=ASOF,
        generated_at=GENERATED_AT,
        currencies=(),
        pairs=(_pair(),),
        shortlist=(_sized_idea(),),
        config_digest="abc123",
    )


def test_the_markdown_report_shows_the_marker_on_a_sized_ticket() -> None:
    rendered = render_report(_report())

    assert BROKER_UNCONFIRMED in rendered


def _dashboard_context() -> dict[str, object]:
    report = _report()
    return {
        "report": SimpleNamespace(
            asof=report.asof,
            generated_at=report.generated_at,
            config_digest=report.config_digest,
            shortlist=report.shortlist,
            events=(),
            warnings=(),
        ),
        "diff": None,
        "config": None,
        "grid": {},
        "pillar_order": (),
        "currencies": (),
        "pairs": report.pairs,
        # The template's arithmetic namespace, stubbed to fixed numbers: this
        # test is about one label reaching the card, not the bar geometry.
        "view": SimpleNamespace(
            title="FX bias",
            bar_pct=lambda value: 50.0,
            heat=lambda spread: "heat-p1",
            at_pct=lambda when: 50.0,
            span_pct=lambda a, b: 10.0,
            legend=(),
            hour_marks=(),
            blackouts=(),
        ),
    }


def test_the_dashboard_shows_the_marker_on_a_sized_ticket() -> None:
    """The dashboard rendered no size warnings at all before this, which is the
    #17-class defect on this surface: the marker reached the dataclass and
    stopped before the reader (ADR 0002 rule 3)."""
    environment = Environment(
        loader=FileSystemLoader(PACKAGE_ROOT / "dashboard" / "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("dashboard.html.j2").render(
        **_dashboard_context()
    )

    assert BROKER_UNCONFIRMED in rendered


def test_the_default_spread_table_is_populated() -> None:
    """The default profile ships the spread table, so a run on it does not
    report `spread:unchecked` on all 28 pairs, which would be a marker on
    nothing. The table is still unconfirmed, covered by `confirmed`."""
    assert "EURUSD" in DEFAULT_TYPICAL_SPREAD_PIPS
    assert Config().broker.typical_spread_pips["EURUSD"] > 0.0
