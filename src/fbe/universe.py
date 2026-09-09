"""The traded universe: G10 currencies, their metadata, and the pair grid.

Scope was fixed at G10 majors deliberately. The trading plan this engine
serves calls for "currency pairs with low spreads and trading costs", and G10
crosses are the only set that reliably clears that bar on a retail account.
Adding ZAR or other EM would widen the opportunity set and the spread bill at
the same time; see ``docs/roadmap.md`` if that changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "CurrencyMeta",
    "G10",
    "CURRENCIES",
    "MAJORS",
    "ALL_PAIRS",
    "pair_name",
    "split_pair",
    "meta",
]


@dataclass(frozen=True, slots=True)
class CurrencyMeta:
    """Static facts about a currency that the pillars need.

    Attributes:
        central_bank: Short name of the rate-setting authority.
        inflation_target: Headline target in percent. Used by the inflation
            pillar to score deviation from target rather than raw CPI, since a
            3% print means something different in Tokyo than in London.
        risk_beta: Rough behaviour in a risk-off shock, in ``-1..+1``.
            Negative marks a funding or haven currency that rallies when
            equities fall; positive marks a high-beta currency that falls with
            them. Consumed by the risk-regime pillar.
        commodity_link: Commodity complex whose terms of trade drive the
            currency, or ``None`` for currencies with no dominant link.
        session: Primary liquidity session, used by the execution layer to
            flag pairs the trader would be holding through a thin book.
    """

    code: str
    name: str
    central_bank: str
    inflation_target: float
    risk_beta: float
    commodity_link: str | None = None
    session: str = "london"
    aliases: Sequence[str] = field(default_factory=tuple)


G10: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")
"""Scored universe. Note this is the FX-market "G10 majors" convention minus
SEK and NOK, which carry materially wider retail spreads."""


CURRENCIES: Mapping[str, CurrencyMeta] = {
    "USD": CurrencyMeta(
        code="USD",
        name="US dollar",
        central_bank="Federal Reserve",
        inflation_target=2.0,
        risk_beta=-0.5,
        session="new_york",
    ),
    "EUR": CurrencyMeta(
        code="EUR",
        name="Euro",
        central_bank="European Central Bank",
        inflation_target=2.0,
        risk_beta=0.1,
        session="london",
    ),
    "GBP": CurrencyMeta(
        code="GBP",
        name="Pound sterling",
        central_bank="Bank of England",
        inflation_target=2.0,
        risk_beta=0.3,
        session="london",
    ),
    "JPY": CurrencyMeta(
        code="JPY",
        name="Japanese yen",
        central_bank="Bank of Japan",
        inflation_target=2.0,
        risk_beta=-0.9,
        session="tokyo",
    ),
    "CHF": CurrencyMeta(
        code="CHF",
        name="Swiss franc",
        central_bank="Swiss National Bank",
        inflation_target=1.0,
        risk_beta=-0.7,
        session="london",
    ),
    "CAD": CurrencyMeta(
        code="CAD",
        name="Canadian dollar",
        central_bank="Bank of Canada",
        inflation_target=2.0,
        risk_beta=0.4,
        commodity_link="crude_oil",
        session="new_york",
    ),
    "AUD": CurrencyMeta(
        code="AUD",
        name="Australian dollar",
        central_bank="Reserve Bank of Australia",
        inflation_target=2.5,
        risk_beta=0.9,
        commodity_link="iron_ore",
        session="sydney",
    ),
    "NZD": CurrencyMeta(
        code="NZD",
        name="New Zealand dollar",
        central_bank="Reserve Bank of New Zealand",
        inflation_target=2.0,
        risk_beta=0.8,
        commodity_link="dairy",
        session="sydney",
    ),
}


MAJORS: tuple[str, ...] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
)
"""Dollar pairs. These carry the tightest spreads and are where the plan's
"major currency pairs" rule points first."""


def pair_name(base: str, quote: str) -> str:
    """Return the market-convention pair string for two currencies."""
    return f"{base}{quote}"


def split_pair(pair: str) -> tuple[str, str]:
    """Split a six-character pair string into ``(base, quote)``."""
    if len(pair) != 6:
        raise ValueError(f"expected a six-character pair, got {pair!r}")
    return pair[:3], pair[3:]


def _build_pairs() -> tuple[str, ...]:
    """Build the 28 G10 crosses using market quoting convention.

    Convention order matters: the market quotes EUR/USD, not USD/EUR, and a
    report that inverts a pair silently inverts its bias. Precedence below is
    the standard FX hierarchy.
    """
    precedence = ("EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY")
    rank = {code: i for i, code in enumerate(precedence)}
    pairs: list[str] = []
    for i, a in enumerate(G10):
        for b in G10[i + 1 :]:
            base, quote = (a, b) if rank[a] < rank[b] else (b, a)
            pairs.append(pair_name(base, quote))
    return tuple(sorted(pairs))


ALL_PAIRS: tuple[str, ...] = _build_pairs()
"""All 28 unique G10 crosses in market quoting convention."""


def meta(currency: str) -> CurrencyMeta:
    """Look up a currency's metadata, raising a clear error if unknown."""
    try:
        return CURRENCIES[currency.upper()]
    except KeyError:
        raise KeyError(
            f"{currency!r} is outside the scored universe {G10}"
        ) from None
