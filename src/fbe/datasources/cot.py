"""CFTC Commitments of Traders: the POSITIONING pillar's only input.

Every week the CFTC publishes how many futures contracts each category of
trader holds in each market. For the seven non-dollar G10 currencies there is a
CME contract, and the net position of the speculative categories is the closest
thing retail has to a free read on how crowded a trade already is.

Why positioning is scored against the other pillars
---------------------------------------------------
The other six pillars answer "which currency deserves to be stronger". This one
answers "who already owns that view". A currency with strong fundamentals and a
record long position has had its move; the marginal buyer is gone and the risk
is a squeeze. So the positioning pillar reduces conviction where the crowd
already agrees with the model, and adds to it where the crowd does not. It is
meant to disagree with the composite, and a run where it never does is a sign
it is not being computed correctly.

The API
-------
The CFTC publishes on a Socrata instance at ``https://publicreporting.cftc.gov``
with no key required. Verified live:

* ``/resource/gpe5-46if.json``, Traders in Financial Futures, futures only.
* ``/resource/yw9f-hn96.json``, Traders in Financial Futures, futures and
  options combined.
* ``/resource/6dca-aqww.json``, Legacy, futures only.
* ``/resource/jun7-fc8e.json``, Legacy, futures and options combined.
* ``/resource/72hh-3qpy.json``, Disaggregated, futures only.

TFF is the correct report for currencies. The Legacy report splits traders into
"commercial" and "non-commercial", a classification designed for grain markets
in the 1920s that lumps every financial participant into one bucket. TFF splits
the same open interest into dealer, asset manager, leveraged funds and other
reportables. Leveraged funds are the hedge funds and CTAs whose positions
actually mean-revert, which is the signal this pillar wants. Asset managers are
slower real money and are worth tracking separately rather than netting in.

Socrata query parameters, all prefixed with a dollar sign: ``$select``,
``$where``, ``$order``, ``$limit`` (default 1000, raise it explicitly),
``$offset``, ``$group``. Rate limiting is by IP for anonymous callers; an
``X-App-Token`` header, free from the Socrata developer portal, moves the caller
onto a higher shared budget. One weekly refresh does not need one.

Release timing: stale by design
-------------------------------
The CFTC's own wording: the reports are released "each Friday at 3:30 pm
Eastern Time (US), using the data from the immediately preceding Tuesday of
that week". Positions are snapped at Tuesday's close and take three days to
process.

So on any given Wednesday the freshest available number is eight days old, and
by the following Friday morning it is ten. Nothing can be done about that. What
matters is that the engine never treats a COT reading as current: the period on
the `Observation` is the Tuesday, not the Friday, and ``released_at`` is the
Friday. Scoring positioning as though it were a live number is the standard way
to misuse this dataset.

Deriving a dollar position
--------------------------
There is no liquid CME "US dollar" contract in TFF. The ICE Dollar Index
(contract code 098662) exists in the Legacy report but carries roughly 50,000
contracts of open interest against the euro contract's 800,000, so it is too
thin to lead.

The dollar read comes from the complement instead. Every one of the seven
contracts below is quoted as foreign currency per dollar's counterpart, that is,
a long EUR future is short dollars. Net the seven, flip the sign, and the result
is the speculative dollar position implied by the currency futures complex. It
needs normalising before the legs can be added: raw contract counts are not
comparable across contracts with different notional sizes, and open interest
differs by more than an order of magnitude between EUR and NZD. Normalising each
leg by its own open interest, or by its own multi-year percentile, is what makes
the sum mean anything. That normalisation is the scoring layer's job, not this
module's; this module returns raw contract counts and open interest and lets the
pillar decide.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "BASE_URL",
    "CONTRACT_CODES",
    "DATASETS",
    "RATE_LIMIT",
    "RELEASE_DAY",
    "RELEASE_TIME_ET",
    "TFF_FIELDS",
    "CotSource",
]


BASE_URL = "https://publicreporting.cftc.gov/resource/"
"""Socrata resource root. Verified live, no key required."""

DATASETS: Mapping[str, str] = {
    "tff_futures_only": "gpe5-46if",
    "tff_combined": "yw9f-hn96",
    "legacy_futures_only": "6dca-aqww",
    "legacy_combined": "jun7-fc8e",
    "disaggregated_futures_only": "72hh-3qpy",
}
"""All five verified live by query. ``tff_futures_only`` is the default: options
positions are delta-ambiguous in the combined series, so the futures-only view
is the cleaner directional read."""

DEFAULT_DATASET = DATASETS["tff_futures_only"]

CONTRACT_CODES: Mapping[str, str] = {
    "EUR": "099741",
    "GBP": "096742",
    "JPY": "097741",
    "CHF": "092741",
    "CAD": "090741",
    "AUD": "232741",
    "NZD": "112741",
}
"""CFTC contract market codes for the seven non-dollar G10 currency futures,
all on the Chicago Mercantile Exchange. Every code verified by querying the TFF
dataset and confirming the returned ``market_and_exchange_names``.

Note two historical name changes that a text-matching implementation would trip
over and a code-matching one will not: GBP appears as both "BRITISH POUND" and
"BRITISH POUND STERLING", and NZD as both "NZ DOLLAR" and "NEW ZEALAND DOLLAR",
with the change falling in early 2022. Match on the code."""

USD_INDEX_CODE = "098662"
"""ICE Dollar Index, present in the Legacy report only, not TFF. A cross-check
on the derived dollar position, never the primary read: see the module
docstring."""

RELEASE_DAY = "friday"
RELEASE_TIME_ET = "15:30"
"""Positions are as of the preceding Tuesday's close. The lag is structural."""

TFF_FIELDS: Mapping[str, str] = {
    "report_date": "report_date_as_yyyy_mm_dd",
    "contract_code": "cftc_contract_market_code",
    "market_name": "market_and_exchange_names",
    "open_interest": "open_interest_all",
    "dealer_long": "dealer_positions_long_all",
    "dealer_short": "dealer_positions_short_all",
    "asset_mgr_long": "asset_mgr_positions_long",
    "asset_mgr_short": "asset_mgr_positions_short",
    "lev_money_long": "lev_money_positions_long",
    "lev_money_short": "lev_money_positions_short",
    "other_rept_long": "other_rept_positions_long",
    "other_rept_short": "other_rept_positions_short",
    "nonrept_long": "nonrept_positions_long_all",
    "nonrept_short": "nonrept_positions_short_all",
}
"""TFF column names, verified against a live row. Socrata returns every numeric
column as a string, so each one needs an explicit cast.

The pillar's headline number is ``lev_money_long - lev_money_short``: leveraged
funds are the fast money whose crowding mean-reverts. Asset manager positions
are worth carrying alongside as the slow-money view, and dealer positions are
mostly the other side of both and carry little signal on their own."""

RATE_LIMIT = RateLimit(requests=30, per_seconds=60.0, min_interval_seconds=0.5)
"""Anonymous Socrata callers are throttled by IP. This refresh runs weekly and
touches eight contracts, so there is no reason to push."""


class CotSource(BaseDataSource):
    """Fetches CFTC Commitments of Traders positioning.

    Backs POSITIONING and nothing else. Emits ``cot_net_position``
    observations whose ``period`` is the Tuesday the positions were snapped and
    whose ``released_at`` is the Friday they were published, so that the
    staleness penalty downstream sees the real age of the number.

    Attributes:
        name: ``"cftc"``, matching ``SeriesRef.source``.

    """

    name = "cftc"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=2.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed; the
                Socrata endpoints are open.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report availability, which is always true: the endpoints need no key.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch weekly net positioning for the requested currencies.

        Args:
            indicators: Canonical indicator keys. Only ``cot_net_position`` is
                served; anything else is ignored.
            currencies: ISO 4217 codes. ``"USD"`` triggers the derived
                complement rather than a lookup.
            start: Earliest report date wanted.
            end: Latest report date wanted.

        Returns:
            One observation per currency per weekly report date.

        Raises:
            SourceError: On repeated request failure or an unparseable body.

        """
        raise NotImplementedError

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"cftc"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        raise NotImplementedError

    def fetch_contract(
        self,
        contract_code: str,
        start: date,
        end: date,
        dataset: str = DEFAULT_DATASET,
    ) -> Sequence[Mapping[str, str]]:
        """Fetch raw weekly rows for one contract.

        Args:
            contract_code: A value from `CONTRACT_CODES`.
            start: Earliest ``report_date_as_yyyy_mm_dd`` wanted.
            end: Latest report date wanted.
            dataset: A Socrata dataset identifier from `DATASETS`.

        Returns:
            Rows as returned by Socrata, values still strings.

        Raises:
            SourceError: On repeated request failure.

        """
        raise NotImplementedError

    def derive_usd_position(
        self,
        rows: Mapping[str, Sequence[Mapping[str, str]]],
    ) -> Sequence[tuple[date, float]]:
        """Derive the implied dollar position from the other seven contracts.

        Each currency future is a long position in that currency and therefore
        a short position in dollars. Netting the seven and flipping the sign
        gives the speculative dollar position the complex implies.

        The legs must be put on a common footing before they are added.
        Contract notionals differ and open interest differs by more than an
        order of magnitude across the seven, so a raw sum is dominated by the
        euro and says almost nothing about the dollar. Normalise each leg by
        its own open interest, or by its own history, before summing.

        Args:
            rows: Raw rows per currency, as returned by `fetch_contract`.

        Returns:
            ``(report_date, net_position)`` pairs for the dollar.

        """
        raise NotImplementedError

    def latest_report_date(self) -> date | None:
        """Return the most recent Tuesday for which positions are published.

        Used to age the positioning pillar honestly rather than assuming the
        weekly cadence held. Publication has been interrupted before.

        Returns:
            The latest report date, or ``None`` when the dataset is empty.

        Raises:
            SourceError: On repeated request failure.

        """
        raise NotImplementedError
