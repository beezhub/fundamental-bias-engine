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
process. When the Tuesday is a US federal holiday the snapshot moves to the
Monday: the dataset holds twelve such weeks since 2007, each the day before
Christmas Day, New Year's Day, Independence Day or Veterans Day fell on a
Tuesday, and no other weekday ever appears. The release stays on the Friday of
that week, and the CFTC's release schedule adds that federal holidays "may delay
release by one or two days", which this source cannot see (see `_released_at`).

So on any given Wednesday the freshest available number is eight days old, and
by the following Friday morning it is ten. Nothing can be done about that. What
matters is that the engine never treats a COT reading as current: the period on
the `Observation` is the snapshot day, not the Friday, and ``released_at`` is
the Friday. Scoring positioning as though it were a live number is the standard
way to misuse this dataset.

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
leg by its own open interest is what makes the sum mean anything, and this
module does it: `CotSource.fetch` emits the percent, because sources emit
canonical keys and ``cot_net_pct_oi`` is a percent of open interest. Only
`CotSource.fetch_contract` hands back raw rows, for `derive_usd_position` to
normalise leg by leg before it nets them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from fbe.config import DataConfig
from fbe.datasources.base import (
    BaseDataSource,
    ProbeRequest,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.registry import INDICATORS, SeriesRef
from fbe.types import Observation

__all__ = [
    "BASE_URL",
    "CONTRACT_CODES",
    "DATASETS",
    "DEFAULT_DATASET",
    "INDICATOR",
    "PERCENT_SCALE",
    "RATE_LIMIT",
    "RELEASE_DAY",
    "SNAPSHOT_WEEKDAYS",
    "RELEASE_WEEKDAY",
    "RELEASE_TIME_ET",
    "RELEASE_ZONE",
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

SNAPSHOT_WEEKDAYS = (0, 1)
"""``date.weekday()`` values a report date may carry: Monday and Tuesday.

Tuesday is the rule. Monday is the CFTC's own exception for a week whose
Tuesday is a US federal holiday, and it is the only exception: queried live on
2026-09-24, the TFF futures-only dataset carries twelve Monday report dates
since 2007 and no other weekday but Tuesday (#274). Checked rather than
assumed, because `CotSource._released_at` stamps the release from the report
date, and the stamp is what the visibility rule reads. A report date on any
other weekday means a renamed or misread column rather than a week the CFTC
snapped on a different day, so it stops the run. Before #274 this was Tuesday
alone, and the first holiday week inside the lookback failed every refresh."""

RELEASE_DAY = "friday"
RELEASE_TIME_ET = "15:30"
"""Positions are as of the snapshot day's close. The lag is structural."""

RELEASE_WEEKDAY = 4
"""``date.weekday()`` for Friday, the day the CFTC publishes.

Not a guess and not configurable: the CFTC publishes every Friday using the data
from the immediately preceding Tuesday, or Monday in a holiday week, so the
release is the Friday of the report week by the release schedule itself.
Expressed as a weekday rather than as a lag because the lag is three days from
a Tuesday and four from a Monday, and one rule for both is the point.
`CotSource.fetch` stamps ``period`` with the snapshot day and ``released_at``
with the Friday, and a run dated between the two must not see the reading,
which is what the visibility rule in `fbe.pillars.base` uses ``released_at``
for."""

RELEASE_ZONE = ZoneInfo("America/New_York")
"""The release time is quoted in Eastern Time, which observes daylight saving.

A fixed offset would be an hour out for half the year: 15:30 Eastern is 19:30
UTC in summer and 20:30 UTC in winter. Nothing reads the hour today, because
`fbe.pillars.base` compares ``released_at.date()`` against the run date, so the
zone is here because the instant is a fact about the release rather than because
the visibility rule currently turns on it. A wrong instant would be a latent
defect the first time anything compares times."""

INDICATOR = "cot_net_pct_oi"
"""The one canonical key this source serves, read from the registry for its unit
and frequency rather than restated here."""

PERCENT_SCALE = 100.0
"""Factor turning the ratio into the percent this key is named for.

Section 3.6 of ``docs/scoring-spec.md`` wrote this quantity as a plain division,
``(long - short) / open_interest``, and called the result a share. Four other
places read the same key as a percent: the key's own name, `PositioningPillar`'s
docstring in four sentences including "divided by open interest, in percent, so
this pillar never sees a raw count", section 7.4's column header, and the
section 7 fixture whose USD row is ``26.2`` against a three-year mean of
``+9.8``. The registry description used to make the opposite case explicitly,
so it was four against two; ADR 0011 records the argument and reverses it, and
section 3.6 now says the same thing as the rest.

The fixture is read by a person and not by a test. What
``tests/test_worked_example.py`` asserts on that row is ``(net - mean) / sd``,
which is scale-invariant and would pass on ratios too, and
`PositioningPillar._extract` is still scaffolded. So nothing executable links
the spec's own worked example to this constant, which is why the constant
carries the argument.

The scale is free for the score, which is why nothing downstream would have
raised on the ratio. The pillar z-scores this series against its own history,
and multiplying every point by the same constant leaves every z-score
unchanged. What the ratio would have changed is the headline number a report
shows a person, by a factor of a hundred, so the only thing that could pin this
is the name. Reversing the choice is this constant."""

ROW_LIMIT = 2000
"""Explicit ``$limit`` on a contract's window, above Socrata's default of 1000.

A five-year weekly history for one contract is about 260 rows, so this is
generous rather than tight. It is sent explicitly because the default would
truncate a longer horizon silently, and a shortened history produces a
time-series z-score against fewer years without anything raising."""

LATEST_LOOKBACK_ROWS = 50
"""Rows to read when asking which week is newest.

One would do if ``$order`` were always honoured. It is read over a small window
and reduced with ``max`` instead, so a provider that ignored the sort cannot
hand back the oldest week as the newest."""

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
funds are the fast money whose crowding mean-reverts. Issue #174's ruling
settled that against the alternatives and ADR 0011 records it.

Asset manager positions would be worth carrying alongside as the slow-money
view, and dealer positions are mostly the other side of both and carry little
signal on their own. **No pillar consumes either of them today.** The columns
are named here so a cross-check is one query away, not because anything reads
them, and netting the asset manager and other reportable columns in alongside
leveraged funds would reconstruct the Legacy non-commercial bucket out of TFF's
parts, which is the one thing the TFF report exists to avoid."""

RATE_LIMIT = RateLimit(requests=30, per_seconds=60.0, min_interval_seconds=0.5)
"""Anonymous Socrata callers are throttled by IP. This refresh runs weekly and
touches eight contracts, so there is no reason to push."""


class CotSource(BaseDataSource):
    """Fetches CFTC Commitments of Traders positioning.

    Backs POSITIONING and nothing else. Emits ``cot_net_pct_oi``
    observations whose ``period`` is the Tuesday the positions were snapped and
    whose ``released_at`` is the Friday they were published, so that the
    staleness penalty downstream sees the real age of the number.

    Attributes:
        name: ``"cftc"``, matching ``SeriesRef.source``.

    """

    name = "cftc"
    base_url = BASE_URL
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
            Whether this source can be used on this run. Always ``True``: the
            Socrata endpoints are open, so there is nothing about the
            configuration that could rule this source out, and there is no
            directory or credential to check.

        This answers about configuration rather than connectivity, which is the
        contract `BaseDataSource.available` sets and
        ``tests/test_datasource_base.py`` enforces across every source: no
        implementation here may make a request. So an unreachable endpoint is
        not reported here. The response-shape check belongs to
        `probe_request`, which is what ``fbe doctor`` reads, and an offline run
        with nothing cached is reported by `fetch_contract` raising, which is
        the only place that distinction can be made without a network call.

        """
        return True

    def probe_request(self) -> ProbeRequest:
        """Name the one request ``fbe doctor`` should judge this source on.

        Returns:
            A one-row query against the dataset this source actually reads,
            with a check that the body is a Socrata array of TFF rows.

        Socrata answers some failures with HTTP 200 and a JSON error object
        rather than an array, and a maintenance page is a 200 as well, so a
        verdict taken from the status code alone prints a broken source as
        healthy. That is the defect #187 fixed for the other sources. The probe
        also names the default dataset rather than the resource root, because a
        root that answers proves only that Socrata is up, not that the TFF
        report is being served.

        """

        def verify(body: bytes) -> object:
            rows = self._decode(body)
            if not isinstance(rows, list) or not rows:
                raise SourceError(
                    f"{self.name} probe expected a non-empty Socrata array of "
                    f"rows from {DEFAULT_DATASET}, got {type(rows).__name__}"
                )
            first = rows[0]
            if not isinstance(first, Mapping) or TFF_FIELDS["report_date"] not in first:
                raise SourceError(
                    f"{self.name} probe reached {DEFAULT_DATASET} but the row "
                    f"carries no {TFF_FIELDS['report_date']!r} column, so this "
                    "is not the Traders in Financial Futures report"
                )
            return rows

        return ProbeRequest(
            path=f"{DEFAULT_DATASET}.json",
            params={"$limit": "1", "$order": TFF_FIELDS["report_date"]},
            verify=verify,
        )

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch weekly net positioning for the requested currencies.

        Args:
            indicators: Canonical indicator keys. Only ``cot_net_pct_oi`` is
                served; anything else is ignored.
            currencies: ISO 4217 codes. ``"USD"`` triggers the derived
                complement rather than a lookup.
            start: Earliest report date wanted.
            end: Latest report date wanted.

        Returns:
            One observation per currency per weekly report date, carrying
            ``(lev_money_long - lev_money_short) / open_interest_all`` as a
            percent of open interest, positive for a net long in that currency.
            See `PERCENT_SCALE` for why a percent rather than the ratio section
            3.6 of ``docs/scoring-spec.md`` writes.
            ``period`` is the Tuesday the positions were snapped and
            ``released_at`` the Friday they were published.

            Empty when ``cot_net_pct_oi`` is not among ``indicators``, and
            empty for a currency the CFTC published no week for. An empty
            sequence is a reading here: it says the requested window holds no
            published week, which is different from the request failing, and
            the latter raises.

        Raises:
            SourceError: On repeated request failure, an unparseable body, a
                row missing a column this source reads, or a contract reporting
                zero open interest. None of those is a currency with no
                positioning, and answering them with an absence would make a
                broken feed indistinguishable from a quiet one.

        The numerator is leveraged funds alone. Issue #174's ruling settled
        that and ADR 0011 records it: leveraged funds are the money whose
        crowding mean-reverts, which is the property section 3.6 of
        ``docs/scoring-spec.md`` reasons from. The asset manager, dealer and
        other reportable columns are read by nothing.

        ``"USD"`` is derived rather than looked up. There is no liquid dollar
        contract in TFF, so the dollar reading is the sign-flipped net of the
        other seven, which means asking for the dollar costs seven requests
        rather than one. See `derive_usd_position`.

        """
        if INDICATOR not in set(indicators):
            return ()

        wanted = list(currencies)
        spec = INDICATORS[INDICATOR]
        built: list[Observation] = []

        for currency in wanted:
            if currency == "USD":
                continue
            code = CONTRACT_CODES.get(currency)
            if code is None:
                continue
            ref = spec.series.get(currency)
            if ref is None:
                continue
            for report_date, percent in self._percents(
                self.fetch_contract(code, start, end)
            ):
                if not start <= report_date <= end:
                    # The query carries the window, so this only fires if the
                    # provider ignored `$where`. Honouring the argument here
                    # costs two comparisons and stops a widened answer reaching
                    # a caller that asked for a period range.
                    continue
                built.append(
                    self._observation(
                        INDICATOR,
                        currency,
                        ref,
                        report_date,
                        percent,
                        released_at=self._released_at(report_date),
                    )
                )

        if "USD" in wanted:
            ref = spec.series.get("USD")
            if ref is not None:
                legs = {
                    currency: self.fetch_contract(code, start, end)
                    for currency, code in CONTRACT_CODES.items()
                }
                self._check_every_leg_published(legs)
                for report_date, net in self.derive_usd_position(legs):
                    if not start <= report_date <= end:
                        continue
                    built.append(
                        self._observation(
                            INDICATOR,
                            "USD",
                            ref,
                            report_date,
                            net,
                            released_at=self._released_at(report_date),
                        )
                    )

        return tuple(built)

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"cftc"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`. Derived
            from `fbe.datasources.registry.INDICATORS` rather than from a list
            kept here, so routing a ref to this source is a registry edit and
            nothing in this module has to be remembered alongside it.

            The dollar entry is included, because the registry routes it to
            this source and the coverage report counts what the registry says.
            Its ``series_id`` names the ICE Dollar Index, which this source
            does not query: the dollar reading is derived from the other seven.
            The ref's own note records that.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

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
            Rows as returned by Socrata, values still strings, in the order the
            provider sent them. The query asks for oldest first and this method
            does not verify that it happened, which is why `_percents` sorts.
            Empty when the dataset holds no week in the window, which is a
            reading rather than a failure.

        Raises:
            SourceError: On repeated request failure, an unparseable body, or a
                body that is not a Socrata array of rows. Socrata answers some
                failures with HTTP 200 and a JSON error object, so the shape is
                checked rather than the status alone.

        ``$limit`` is sent explicitly. Socrata's default is 1000 and a
        five-year weekly window for one contract is about 260 rows, so the
        default is generous today and would truncate silently if the horizon
        ever widened. A truncated history shortens a time-series z-score
        without raising, which is the failure this argument exists to prevent.

        The window is expressed on ``report_date_as_yyyy_mm_dd``, the Tuesday
        the positions were snapped, so a caller asking for a period range gets
        periods rather than release dates.

        """
        params: Mapping[str, str | int | float] = {
            "$where": (
                f"{TFF_FIELDS['contract_code']}='{contract_code}' "
                f"AND {TFF_FIELDS['report_date']}>='{_socrata_stamp(start)}' "
                f"AND {TFF_FIELDS['report_date']}<='{_socrata_stamp(end)}'"
            ),
            "$order": TFF_FIELDS["report_date"],
            "$limit": ROW_LIMIT,
        }
        rows = self._request(f"{dataset}.json", params)
        if not isinstance(rows, list):
            raise SourceError(
                f"{self.name} expected a Socrata array of rows from {dataset} "
                f"for contract {contract_code}, got {type(rows).__name__}"
            )
        if len(rows) >= ROW_LIMIT:
            raise SourceError(
                f"{self.name} got {len(rows)} rows for contract "
                f"{contract_code} from {dataset}, which is the $limit of "
                f"{ROW_LIMIT}, so the history is truncated at an unknown "
                "point. The rows are sorted oldest first, so what is missing "
                "is the newest weeks, and a shortened history reads as a "
                "quiet market rather than as a truncated query."
            )
        for item in rows:
            if not isinstance(item, Mapping):
                raise SourceError(
                    f"{self.name} got a {type(item).__name__} where "
                    f"{dataset} should hold a row for contract {contract_code}"
                )
            returned = item.get(TFF_FIELDS["contract_code"])
            if returned != contract_code:
                raise SourceError(
                    f"{self.name} asked {dataset} for contract "
                    f"{contract_code} and got a row for {returned!r}. The "
                    "contract code and the report date window are the same "
                    "$where clause, and the window is re-checked downstream, "
                    "so this half is checked too: a mis-routed code returns a "
                    "real position under the wrong currency, which no later "
                    "check would catch."
                )
        return tuple(rows)

    def _check_every_leg_published(
        self, legs: Mapping[str, Sequence[Mapping[str, str]]]
    ) -> None:
        """Refuse a dollar derivation where one leg answered with nothing at all.

        Args:
            legs: Raw rows per currency, one entry per contract in
                `CONTRACT_CODES`.

        Raises:
            SourceError: Some legs returned rows for the window and at least
                one returned none.

        `derive_usd_position` returns only the dates every leg reported, so one
        leg that is empty for the whole window empties the dollar series rather
        than shortening it. That is the right answer for a week nobody
        published and the wrong answer for a contract code that changed
        underneath us: the series would simply stop, and an empty sequence
        reads downstream as a currency with no positioning.

        The distinguishing signal is the other six. All seven empty is a window
        the dataset holds no week for, which is data. Six populated and one
        empty is that one contract, which is a shape change.

        """
        empty = sorted(currency for currency, rows in legs.items() if not rows)
        if empty and len(empty) != len(legs):
            raise SourceError(
                f"{self.name} got no rows at all for "
                f"{', '.join(empty)} while the other "
                f"{len(legs) - len(empty)} legs answered, so the dollar "
                "derivation has no complete week. A contract that returns "
                "nothing for a whole window is a changed contract code rather "
                "than a quiet market, and deriving the dollar from the legs "
                "that did answer would report it as less exposed than they "
                "imply."
            )

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
            ``(report_date, net_position)`` pairs for the dollar, oldest first,
            where the net position is the negated sum of the legs, each a
            percent of its own open interest. Positive means the complex
            implies a net long dollar position.

            Only a date that every supplied leg reported is returned. A date
            one leg is missing is not returned at all, because a sum cannot
            tell an absent leg from a leg at zero: dropping the term and adding
            a zero give the same number, and that number is smaller in
            magnitude than the published legs imply with nothing marking it.
            On the committed capture, losing the Canadian leg alone would move
            the dollar from +30.64 to +14.08, in a complex whose individual
            legs span -16.6 to +10.9. A week with no dollar reading is an
            absence the pillar can represent. A halved one is a number.

        Raises:
            SourceError: A row is missing a column this source reads, a
                contract reports zero open interest, or one leg carries two
                rows for the same report date. The last would count that leg
                twice, and one row per contract per week is the shape of this
                dataset rather than an assumption about it.

        Each leg is a percent of its own open interest before the sum, not a
        contract count. Open interest differs by more than an order of magnitude
        between the euro and the New Zealand dollar, so a raw sum is a euro
        reading wearing a dollar label. On the 2026-09-08 capture a raw count
        sum gives +84,215 against this method's +30.64, and the two agree in
        sign there, so the argument is about what the number means rather than
        about a reproduced sign flip.

        Summing rather than averaging is free, and the completeness rule above
        is what makes it free. The pillar z-scores this series against its own
        history, and dividing every point by the same constant leaves every
        z-score unchanged. That constant is the leg count, so it has to be the
        same on every returned date: with a varying leg count, sum and mean
        differ by a date-varying divisor and the choice is no longer free.

        """
        per_leg: dict[str, dict[date, float]] = {}
        for currency, legs in rows.items():
            weeks: dict[date, float] = {}
            for report_date, percent in self._percents(legs):
                if report_date in weeks:
                    raise SourceError(
                        f"{self.name} got two rows for {currency} on "
                        f"{report_date}. One row per contract per week is this "
                        "dataset's shape, and summing both would double that "
                        "leg's weight in the dollar reading."
                    )
                weeks[report_date] = percent
            per_leg[currency] = weeks
        if not per_leg:
            return []
        complete = set.intersection(*(set(weeks) for weeks in per_leg.values()))
        return [
            (when, -sum(weeks[when] for weeks in per_leg.values()))
            for when in sorted(complete)
        ]

    def latest_report_date(self) -> date | None:
        """Return the most recent Tuesday for which positions are published.

        Used to age the positioning pillar honestly rather than assuming the
        weekly cadence held. Publication has been interrupted before.

        Returns:
            The latest report date, or ``None`` when the dataset is empty.
            ``None`` means the dataset holds no week, which is why a failed
            request raises instead of answering ``None``: the two would
            otherwise be the same answer, and one of them means the pillar
            should age against nothing while the other means the run is broken.

        Raises:
            SourceError: On repeated request failure or an unparseable body.

        The maximum is taken over the rows returned rather than trusting the
        ``$order`` to have been applied. A provider that ignored it would
        otherwise hand back the oldest week as the newest, which reads as a
        dataset months behind and is indistinguishable from a real outage.

        """
        params: Mapping[str, str | int | float] = {
            "$select": TFF_FIELDS["report_date"],
            "$order": f"{TFF_FIELDS['report_date']} DESC",
            "$limit": LATEST_LOOKBACK_ROWS,
        }
        rows = self._request(f"{DEFAULT_DATASET}.json", params)
        if not isinstance(rows, list):
            raise SourceError(
                f"{self.name} expected a Socrata array of rows from "
                f"{DEFAULT_DATASET}, got {type(rows).__name__}"
            )
        dates = [
            _parse_report_date(item[TFF_FIELDS["report_date"]], self.name)
            for item in rows
            if isinstance(item, Mapping) and TFF_FIELDS["report_date"] in item
        ]
        if not dates:
            if rows:
                raise SourceError(
                    f"{self.name} got {len(rows)} rows from {DEFAULT_DATASET} "
                    f"and not one carried {TFF_FIELDS['report_date']!r}, so "
                    "the dataset has changed shape. Answering `None` here "
                    "would report a renamed column as a dataset holding no "
                    "week, and the positioning pillar ages against the "
                    "difference."
                )
            return None
        return max(dates)

    def _percents(self, rows: Sequence[Mapping[str, str]]) -> list[tuple[date, float]]:
        """Turn raw rows into ``(report_date, percent)`` pairs, oldest first.

        Args:
            rows: Raw rows for one contract, as `fetch_contract` returns them.

        Returns:
            The leveraged-funds net position as a percent of open interest,
            one pair per report date, sorted by date. Positive is a net long in
            the contract's currency. Percent rather than the ratio section 3.6
            writes, for the reason `PERCENT_SCALE` gives.

        Raises:
            SourceError: A row is missing one of the three columns this reads,
                carries a value that is not a finite number, or reports zero or
                negative open interest. Each is a feed that changed shape rather
                than a week with no position, so each stops the run instead of
                producing a reading.

        """
        built: list[tuple[date, float]] = []
        for item in rows:
            long = _column(item, "lev_money_long", self.name)
            short = _column(item, "lev_money_short", self.name)
            open_interest = _column(item, "open_interest", self.name)
            # `_column` has already refused a non-finite value, so this
            # comparison cannot be defeated by a NaN divisor.
            if open_interest <= 0.0:
                raise SourceError(
                    f"{self.name} got zero open interest for contract "
                    f"{item.get(TFF_FIELDS['contract_code'], '?')} on "
                    f"{item.get(TFF_FIELDS['report_date'], '?')}, which has no "
                    "position to express as a percent of it. A contract with no "
                    "open interest is not a contract at a flat position."
                )
            report_date = _parse_report_date(item[TFF_FIELDS["report_date"]], self.name)
            built.append((report_date, (long - short) / open_interest * PERCENT_SCALE))
        return sorted(built)

    def _released_at(self, report_date: date) -> datetime:
        """Return when the CFTC published the week snapped on ``report_date``.

        Args:
            report_date: The day the positions were snapped: a Tuesday, or the
                Monday of a week whose Tuesday is a US federal holiday.

        Returns:
            The Friday of that week at 15:30 Eastern, as an aware datetime.
            Eastern rather than a fixed offset because the release time is
            quoted in local time and observes daylight saving, so 15:30 is
            19:30 UTC in summer and 20:30 UTC in winter.

        Raises:
            SourceError: ``report_date`` is neither a Monday nor a Tuesday.
                The Friday below is computed from the weekday, so a Saturday
                report date would produce a release stamp six days earlier
                than its period with nothing objecting, and a Wednesday one is
                a day the CFTC has never snapped on. See `SNAPSHOT_WEEKDAYS`.

        **This is derived from ``period``, which `BaseDataSource._observation`
        says a release stamp never is.** That rule is there because a period and
        a publication date are different facts and a lag assumed from the period
        is the look-ahead bias Phase 6 has to avoid. The exception is taken
        deliberately and only here: the CFTC publishes on a fixed schedule, so
        the Friday is the release date rather than an assumption about it, and
        the second acceptance criterion of #174 requires it. ADR 0011 records
        the exception.

        What the exception costs is the case where the schedule does not hold.
        Publication has been suspended and backfilled before, and every report
        date inside such a span is stamped as released that Friday although
        nobody could read any of them until the catch-up. The CFTC also says a
        federal holiday "may delay release by one or two days", so a holiday
        week's reading may be stamped a day or two before anyone could see it.
        Closing either needs a published release calendar, which this dataset
        does not carry.

        The hour is not read by anything today. `fbe.pillars.base` compares
        ``released_at.date()`` against the run date, so a run dated that Friday
        sees the reading whatever the hour, and the aware timestamp is kept
        because the moment is a fact about the release rather than because the
        visibility rule currently uses it.

        """
        if report_date.weekday() not in SNAPSHOT_WEEKDAYS:
            raise SourceError(
                f"{self.name} got report date {report_date}, a "
                f"{report_date.strftime('%A')} rather than a Tuesday or the "
                "Monday of a US holiday week. Those are the only days the CFTC "
                "snaps positions on, so this is a renamed or misread column "
                "rather than a week the CFTC snapped on another day."
            )
        published = report_date + timedelta(
            days=RELEASE_WEEKDAY - report_date.weekday()
        )
        hour, minute = (int(part) for part in RELEASE_TIME_ET.split(":"))
        return datetime.combine(published, time(hour, minute), tzinfo=RELEASE_ZONE)


def _socrata_stamp(when: date) -> str:
    """Render a date as the floating timestamp Socrata compares against.

    Args:
        when: A report date.

    Returns:
        ``YYYY-MM-DDT00:00:00.000``. Socrata stores
        ``report_date_as_yyyy_mm_dd`` as a floating timestamp, so a bare date
        in a ``$where`` clause is a type mismatch and the comparison silently
        matches nothing, which reads downstream as a currency with no
        positioning.

    """
    return f"{when.isoformat()}T00:00:00.000"


def _parse_report_date(raw: str, source: str) -> date:
    """Read the Tuesday out of a Socrata timestamp.

    Args:
        raw: The value of ``report_date_as_yyyy_mm_dd``.
        source: This source's name, for the error message.

    Returns:
        The date part, which is the Tuesday the positions were snapped. The
        time part is always midnight and carries no information.

    Raises:
        SourceError: The value is not a date this can read. A date format
            change must stop the run rather than drop a week: a dropped week
            shortens a history without anything raising.

    """
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as error:
        raise SourceError(
            f"{source} could not read {raw!r} as a report date, so the "
            "dataset's date format has changed"
        ) from error


def _column(row: Mapping[str, str], field: str, source: str) -> float:
    """Read one numeric TFF column, which Socrata returns as a string.

    Args:
        row: One raw row.
        field: A key of `TFF_FIELDS`, not the underlying column name.
        source: This source's name, for the error message.

    Returns:
        The value as a float, in contracts.

    Raises:
        SourceError: The column is absent, not a number, or not finite. The
            first two mean the dataset changed shape, and skipping the row would
            drop a week from a history with nothing raising, which is the same
            defect as a truncated query. The third is checked here, on every
            column rather than on the divisor alone, because `float` reads
            ``"NaN"`` and ``"Infinity"`` without complaint: a NaN in either
            leg divides into a NaN reading that the pillar z-scores into a NaN
            score, and an infinite open interest passes any positivity check and
            divides into zero, which reads as a flat book.

    """
    column = TFF_FIELDS[field]
    if column not in row:
        raise SourceError(
            f"{source} found no {column!r} column on a row from the Traders "
            "in Financial Futures report, so the dataset has changed shape"
        )
    try:
        value = float(row[column])
    except (TypeError, ValueError) as error:
        raise SourceError(
            f"{source} could not read {row[column]!r} as a number for {column!r}"
        ) from error
    if not isfinite(value):
        raise SourceError(
            f"{source} read {row[column]!r} as a non-finite number for "
            f"{column!r}, which is a feed that changed shape rather than a "
            "week with no position"
        )
    return value
