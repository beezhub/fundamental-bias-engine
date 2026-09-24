"""Rebuild ``dashboard_report.json``, the run the dashboard tests render.

Committed so the fixture has a definition rather than a history of hand edits.
A JSON file of this size maintained by editing JSON drifts from the invariants
it was built to carry, and the drift is silent: every number still parses.

Run it from the repository root::

    python3 tests/fixtures/make_dashboard_report.py

What each currency is here to exercise, and why the dashboard needs it:

* ``USD`` is the widest score in the run, so it is the scale every ranking bar
  is drawn against.
* ``GBP`` sits below full coverage, so the page has a row that must print its
  coverage figure rather than only fade.
* ``AUD`` sits below full coverage at a different figure, so a page printing
  one currency's coverage for every thin row fails rather than passing on the
  one row a single-currency test looks at.
* ``JPY`` scored on nothing at all. Its row must carry no bar: drawn at the
  centre line it is indistinguishable from a currency the model placed at the
  middle of its band, which is the opposite fact.
* ``CHF`` sits just off zero, so the matrix has cells inside the band the
  engine grades as no view.

The pairs are built through `fbe.bias.build_pair_biases` rather than from a
ladder written here. A second ladder is the config-drift defect: this file
would grade a spread one way, `fbe.config.ScoringConfig` would grade it
another, and the dashboard colours cells from the config while labelling them
from the record, so the page would render cells whose colour and tooltip
disagree in a direction the real engine cannot produce.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fbe.bias import build_pair_biases  # noqa: E402
from fbe.config import (  # noqa: E402
    BrokerConfig,
    Config,
    DataConfig,
    RiskConfig,
    ScoringConfig,
)
from fbe.report import write_report  # noqa: E402
from fbe.risk import position_size  # noqa: E402
from fbe.types import (  # noqa: E402
    BiasReport,
    CalendarEvent,
    CurrencyScore,
    PillarName,
    PillarScore,
    TradeIdea,
)

ASOF = date(2026, 6, 30)
GENERATED_AT = datetime(2026, 6, 30, 6, 30, tzinfo=UTC)

COMPOSITES = {
    "USD": 1.84,
    "NZD": 0.97,
    "GBP": 0.41,
    "CHF": 0.05,
    "CAD": -0.33,
    "AUD": -0.88,
    "EUR": -1.52,
    "JPY": 0.0,
}

COVERAGE = {
    "USD": 1.0,
    "NZD": 1.0,
    "GBP": 0.6,
    "CHF": 1.0,
    "CAD": 1.0,
    "AUD": 0.86,
    "EUR": 1.0,
    "JPY": 0.0,
}

WEIGHTS = ScoringConfig().weights
"""The shipped weights, so ``coverage`` below is the figure they define."""


def pillars(
    code: str, composite: float, coverage: float
) -> dict[PillarName, PillarScore]:
    """Seven pillar scores whose effective weights sum to ``coverage``.

    The scores are spread around the composite rather than equal to it, so a
    renderer that re-derived the headline from the pillars would print a
    plausible number for the wrong reason and the composite assertions could
    not tell. The weights are the shipped ones scaled to the stated coverage,
    because `fbe.config.ScoringConfig` defines coverage as the sum of effective
    pillar weight, and a fixture that states one and carries another cannot
    support a test that compares the two.
    """
    return {
        name: PillarScore(
            pillar=name,
            currency=code,
            raw=round(composite * 1.1 + index, 3),
            # A real normalised value, not ``None``. `fbe.scoring.coverage`
            # counts a pillar only when its ``z`` is set, and
            # `fbe.bias.agreement` skips any pillar whose ``z`` is absent on
            # either leg, so a fixture of ``None`` states one coverage and
            # carries another: every pair would agree at 0% of pillar weight
            # and every conviction would be capped there, which is not a run
            # the engine can produce.
            z=round((composite + 0.2 * index - 0.6) / 1.2, 3),
            score=round(composite + 0.2 * index - 0.6, 2),
            weight=round(WEIGHTS[name] * coverage, 4),
            asof=ASOF,
            staleness_days=index,
        )
        for index, name in enumerate(PillarName)
    }


def dispersion_of(scores: dict[PillarName, PillarScore], composite: float) -> float:
    """Weighted standard deviation of the pillars about the composite."""
    total = sum(item.weight for item in scores.values())
    if total <= 0.0:
        return 0.0
    variance = (
        sum(item.weight * (item.score - composite) ** 2 for item in scores.values())
        / total
    )
    return round(variance**0.5, 4)


def score(code: str, rank: int) -> CurrencyScore:
    composite = COMPOSITES[code]
    coverage = COVERAGE[code]
    carried = {} if coverage == 0.0 else pillars(code, composite, coverage)
    return CurrencyScore(
        currency=code,
        composite=composite,
        pillars=carried,
        asof=ASOF,
        rank=rank,
        dispersion=dispersion_of(carried, composite),
        coverage=coverage,
    )


RANKS = {
    code: index + 1
    for index, code in enumerate(sorted(COMPOSITES, key=lambda c: (-COMPOSITES[c], c)))
}
CURRENCIES = tuple(score(code, RANKS[code]) for code in COMPOSITES)

CONFIG = Config(risk=RiskConfig(), scoring=ScoringConfig(), data=DataConfig())
PAIRS = tuple(build_pair_biases(CURRENCIES, CONFIG, ASOF))
BY_PAIR = {row.pair: row for row in PAIRS}

RATES = {"USDZAR": 18.5}
"""The rate the sizing needs, at the figure this repository's examples use.

Every money field on `fbe.types.PositionSize` is in the account currency, ZAR
here, and reaching ZAR from a EURUSD position needs a USD leg. Stated once so
`fbe.risk.position_size` does the conversion, rather than this file doing it by
hand and dropping the leg on ``notional``, which is the defect
``tests/test_position_size.py`` already holds a test against.
"""

SIZE = position_size(
    "EURUSD",
    # A 20 pip stop, short, so the stop sits above the entry. The distance is
    # what makes the ticket sizeable at all on this account: 2% of R2,000 is
    # R40, a pip on one unit of EURUSD is R0.00185 at the rate above, and a
    # stop much wider than this rounds to zero lots at the broker's 0.01 step
    # and `position_size` refuses it rather than taking the minimum.
    entry=1.0850,
    stop=1.0870,
    config=RiskConfig(),
    rates=RATES,
    broker=BrokerConfig(),
    risk_fraction=0.02,
)
"""The ticket on the widest pair, sized by the function that sizes tickets.

Built through `fbe.risk.position_size` for the same reason the pairs are built
through `fbe.bias`: a sizing ladder written out here is a second copy of the
rule, and a copy states a position the real function cannot produce on this
account while the page prints a share of balance the account is not carrying.

It carries the lot-step gap on purpose. The rule asks for R40 and the rounded
size exposes R37, which is the difference the card prints side by side and the
reason `realised_risk_amount` exists as its own field.
"""

EVENTS = (
    CalendarEvent(
        title="Core CPI y/y",
        currency="USD",
        scheduled_for=datetime(2026, 6, 30, 12, 30, tzinfo=UTC),
        impact="High",
    ),
    CalendarEvent(
        title="FOMC Member Speaks",
        currency="USD",
        scheduled_for=datetime(2026, 6, 30, 13, 0, tzinfo=UTC),
        impact="High",
    ),
    CalendarEvent(
        title="Retail Sales m/m",
        currency="EUR",
        scheduled_for=datetime(2026, 7, 1, 1, 0, tzinfo=UTC),
        impact="High",
    ),
)

SHORTLIST = (
    TradeIdea(
        bias=BY_PAIR["EURUSD"],
        size=SIZE,
        blackout_until=None,
        rationale=(
            "The rates gap is the widest in the universe and the growth "
            "pillar agrees with it."
        ),
    ),
    TradeIdea(
        bias=BY_PAIR["AUDUSD"],
        size=None,
        blackout_until=datetime(2026, 6, 30, 14, 0, tzinfo=UTC),
        rationale="Second widest, but the US print sits inside the window.",
    ),
)

REPORT = BiasReport(
    asof=ASOF,
    generated_at=GENERATED_AT,
    currencies=CURRENCIES,
    pairs=PAIRS,
    events=EVENTS,
    shortlist=SHORTLIST,
    warnings=(
        "JPY scored on no data: every pillar was outside its staleness limit.",
        "GBP is at 60% pillar coverage.",
    ),
    config_digest=CONFIG.digest(),
)


def main() -> None:
    out = Path(__file__).resolve().parent
    markdown = write_report(REPORT, out, overwrite=True)
    sidecar = markdown.with_suffix(".json")
    target = out / "dashboard_report.json"
    target.write_bytes(sidecar.read_bytes())
    markdown.unlink()
    sidecar.unlink()
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
