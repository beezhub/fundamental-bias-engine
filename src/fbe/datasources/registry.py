"""Canonical indicator registry: the map from indicator keys to real series IDs.

This module is the single place where the engine's vocabulary meets the outside
world's. Pillars ask for ``yield_2y`` for ``"CAD"``; the registry says that
means Bank of Canada Valet series ``BD.CDN.2YR.DQ.YLD``, in percent, daily,
as published. Every data source translates into these keys before returning
`Observation`s, so no pillar ever sees a vendor identifier.

Why this file is worth reading carefully
----------------------------------------
A wrong series ID does not raise. It silently scores a currency off the wrong
number, and the error surfaces months later as an unexplained run of losses.
Every ID below was checked against the live source before being written down.
``SeriesRef.verified`` records that check and ``SeriesRef.last_observed``
records the newest observation the source actually held when it was made.

Freshness is a first-class fact here, not a comment
---------------------------------------------------
An identifier that resolves is not the same as an identifier that updates. FRED
in particular is full of series that answer happily and stopped publishing in
2024 or 2025. `coverage_report` is therefore freshness-aware by default: a ref
counts toward coverage only if it was verified, is not manual, and its
``last_observed`` falls inside the indicator's ``max_staleness_days``. The old
identifier-only count is still available as `identifier_coverage`, clearly
named so nobody reaches for it by accident.

``max_staleness_days`` lives on the `IndicatorSpec` because the registry is
where the release calendar is known. A quarterly balance-of-payments figure
cannot be 45 days old and a daily bond yield should never be, and only this
module knows which is which.

Where the data comes from, and why not all from one place
----------------------------------------------------------
FRED is the widest single free source and is still the backbone, but it mirrors
the OECD with a long and uneven delay, and it carries no non-US front-end
yields at all. Two additions fix most of that:

* **The OECD's own SDMX API** carries current data where FRED's mirror of the
  same OECD material is frozen. FRED's CPI complex stops in March or April
  2025; the OECD API serves the same countries through July or August 2026. It
  also runs two months ahead of FRED on policy rates, 10-year yields and share
  price indices. Anywhere this registry used to point at a frozen FRED OECD
  series, it now points at the OECD directly.
* **National central banks and debt offices** publish their own curves, free
  and without a key. That is where the 2-year yields come from: Bank of Canada,
  the ECB Data Portal, Japan's Ministry of Finance, the Bank of England, the
  Reserve Bank of Australia and the Reserve Bank of New Zealand between them
  cover seven of the eight. The RBNZ one comes with a condition: the RBNZ
  answers every data-centre or cloud egress with a JavaScript challenge, so
  the fetch works from the owner's own residential or mobile connection, which
  is the only place the engine runs live, and from nowhere else.

What is still missing, stated plainly
--------------------------------------
* **The CHF 2-year yield.** The SNB publishes a Confederation spot curve
  and the endpoint is verified, but the cube stopped at 2025-07-31 while the
  rest of the SNB portal stayed current. A genuine discontinuation, with
  nothing to retry, so the gap is accepted and CHF is manual.
* **PMIs**, all eight. Licensed by S&P Global and ISM, on no free API.
* **Current account**, all eight. The FRED family stopped at 2024Q4 and no free
  replacement was found. The staleness allowance below is set to what a
  quarterly balance-of-payments release honestly justifies, which means the
  frozen series correctly fails it rather than being waved through.
* **Industrial production** for CHF, AUD and NZD, and the euro-area aggregate.
* **Euro-area aggregates** for several indicators, where the registry
  substitutes German national series and says so in the note.

Verification date for everything below: 2026-09-09.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date

from fbe.types import Frequency, PillarName
from fbe.universe import G10

__all__ = [
    "CYCLE_DAYS",
    "DEFAULT_PUBLICATION_LAG_DAYS",
    "GLOBAL",
    "INDICATORS",
    "IndicatorSpec",
    "SeriesRef",
    "SOURCE_BOC",
    "SOURCE_BOE",
    "SOURCE_CFTC",
    "SOURCE_ECB",
    "SOURCE_FRED",
    "SOURCE_MANUAL",
    "SOURCE_MOF_JP",
    "SOURCE_OECD",
    "SOURCE_RBA",
    "SOURCE_RBNZ",
    "SOURCE_SNB",
    "SOURCE_STOOQ",
    "TRANSFORMS",
    "UNCONSUMED_INDICATORS",
    "VERIFIED_ON",
    "coverage_report",
    "identifier_coverage",
    "full_weight_age",
    "indicators_for_pillar",
    "publication_lag",
    "staleness_allowance",
    "series_for",
    "stale_refs",
]


VERIFIED_ON = date(2026, 9, 9)
"""When every identifier below was last checked against its live source. The
``last_observed`` dates are as at this date, so freshness computed against a
much later ``asof`` is measuring the age of this file as much as the data."""

DEFAULT_PUBLICATION_LAG_DAYS: Mapping[Frequency, int] = {
    Frequency.DAILY: 1,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 45,
    Frequency.QUARTERLY: 120,
    Frequency.ANNUAL: 552,
    Frequency.IRREGULAR: 45,
}
"""Assumed gap between a period starting and its number being published, per
cadence, for a leg that carries no measured lag of its own.

Read through `publication_lag`, never directly, because a leg can override it:
`SeriesRef.publication_lag_days` is where the data engineer records that one
source is slower than its cadence. This table lives here rather than in
``pillars/`` because the registry is the module that knows the release
calendar, and it cannot import from the pillars. It moved from
``fbe.pillars.base`` in #222; there is no re-export.

The values are measured from ``period``, the first day of the span described
per the `Observation` contract, so the monthly figure of 45 days covers a month
elapsing plus the usual two-week statistical lag, and the quarterly figure of
120 days covers a quarter elapsing plus a month.

The annual figure of 552 days is measured rather than assumed. The World Bank
nominal GDP family on FRED, the only annual series the registry carries, last
published its 2025 reference year on 2026-07-07, and 2025-01-01 to 2026-07-07 is
552 days. It is the binding case of the eight: the US series published a week
earlier. Issue #158 carries the measurements and the ruling that set this entry.

They are deliberately generous. An assumed lag that is too long costs a backtest
a little realism at the margin; one that is too short manufactures profit out of
numbers nobody had, and that error flatters rather than penalises, so it survives
review. Where a source can supply a real ``released_at``, it should, and this
table should never be reached.
"""

CYCLE_DAYS: Mapping[Frequency, int] = {
    Frequency.DAILY: 4,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 31,
    Frequency.QUARTERLY: 92,
    Frequency.ANNUAL: 366,
    Frequency.IRREGULAR: 31,
}
"""Longest calendar gap between consecutive periods of a punctual series.

A punctual leg's newest print is never older than its publication lag plus
one cycle: the day before the next print is due, it is exactly that old. That
bound is what the registry walk in ``tests/test_publication_lag.py`` enforces
on every verified leg as at `VERIFIED_ON`, and it is what the staleness ramp
is built from once #126 lands (ADR 0014). Daily is four rather than one
because a Friday close is the newest print until Tuesday over a long weekend,
and a cycle of one would zero every yield on a Monday. Irregular takes the
monthly figure, which is the cadence the irregular series here approximate.
"""

GLOBAL = "GLOBAL"
"""Pseudo-currency for cross-market series such as VIX. Matches the convention
`Observation.currency` documents: a series that describes the whole market, not
one economy, is filed under ``"GLOBAL"`` rather than duplicated eight times."""

SOURCE_FRED = "fred"
SOURCE_OECD = "oecd"
SOURCE_CFTC = "cftc"
SOURCE_STOOQ = "stooq"
SOURCE_MANUAL = "manual"
SOURCE_BOC = "boc"
SOURCE_ECB = "ecb"
SOURCE_MOF_JP = "mof_jp"
SOURCE_BOE = "boe"
SOURCE_RBA = "rba"
SOURCE_SNB = "snb"
SOURCE_RBNZ = "rbnz"

CURVE_SOURCES: frozenset[str] = frozenset(
    {
        SOURCE_BOC,
        SOURCE_ECB,
        SOURCE_MOF_JP,
        SOURCE_BOE,
        SOURCE_RBA,
        SOURCE_SNB,
        SOURCE_RBNZ,
    }
)
"""The central bank and debt office sources, all served by
`fbe.datasources.curves`. Grouped because they share one thing that matters:
each publishes only its own country, so none of them can ever be a fallback for
another, and losing one is losing a currency rather than degrading a series."""

TRANSFORMS: tuple[str, ...] = (
    "level",
    "yoy",
    "diff",
    "net_position",
    "chg_1m",
    "chg_3m",
)
"""Transform hints a source applies before emitting an `Observation`.

``level`` means the published number is already what the indicator asks for.
``yoy`` means the source publishes an index or a level and the caller must take
the year-on-year percentage change. ``diff`` means take the period-on-period
change, which is how a stock of employed persons becomes an employment change.
``net_position`` is the COT-specific reduction of long and short contract
counts to the canonical percent: the leveraged-funds net divided by open
interest and multiplied by a hundred, which is what `fbe.datasources.cot`
emits and what ``cot_net_pct_oi`` means. It used to stop at the signed
contract count, which left the ref's declared unit naming a quantity no
consumer wanted; issue #174 completed it. The hint covers the whole reduction
because it is used by that one key and by nothing else, so there is no second
consumer to surprise.
``chg_1m`` and ``chg_3m`` mean the source
publishes a percent level and the caller must resample it to month-end (or
quarter-end for the three-month case), difference over one or three periods,
and rescale the percentage-point result into basis points by multiplying by
100.

The last two exist for one reason: no source anywhere publishes a
pre-differenced government bond yield change. ``yield_2y_chg_1m`` and
``yield_2y_chg_3m`` are not separate series with their own identifiers; they
reuse ``yield_2y``'s own `SeriesRef` for each currency, unchanged, and differ
from it only in this transform. See `_yield_change_series`.

The hint lives here rather than in the pillar because the choice is a property
of the series, not of the question being asked: ``CPIAUCSL`` is an index and
the OECD's ``GY`` transformation is already a growth rate, and only the
registry knows which.
"""


@dataclass(frozen=True, slots=True)
class SeriesRef:
    """One source's identifier for one indicator for one currency.

    Attributes:
        source: Short source key matching a `DataSource.name`.
        series_id: The source's own identifier. FRED uses a series ID, the CFTC
            a six-digit contract market code, the OECD a ``flow/key`` pair, the
            curve sources whatever their own API takes. `fbe.datasources.curves`
            and `fbe.datasources.oecd` document how each is composed.
        unit: Unit of the published value, echoed onto every `Observation`.
        frequency: How often the source publishes this particular series. This
            is authoritative and may differ from the parent `IndicatorSpec`,
            since the same indicator arrives monthly in one country and
            quarterly in another.
        transform: One of `TRANSFORMS`, describing what the source must do to
            the published number to produce the canonical indicator.
        verified: True when this identifier was confirmed against the live
            source on `VERIFIED_ON`. False marks a ref that could not be
            checked, which in this registry means either that no free source
            exists or that the source refused an automated request.
        last_observed: Newest observation the source held on `VERIFIED_ON`.
            ``None`` means freshness could not be established, which
            `coverage_report` treats as not fresh. This is the field that stops
            a frozen series from counting as coverage.
        note: Free text: what the series actually measures, and any caveat.
        publication_lag_days: Days from ``period``, the first day of the span
            a figure describes, to the day this leg's source publishes it.
            ``None``, the default, means the leg publishes about as fast as
            its cadence assumes and `DEFAULT_PUBLICATION_LAG_DAYS` applies.
            A value here records that this source is slower: FRED's mirror of
            the OECD tables runs two to three months behind the statistics
            offices, and reading the cadence table for those legs let a
            historical run see a figure before FRED had it. Read through
            `publication_lag` by `BasePillar._visible`, and by the staleness
            ramp once #126 lands. Always measured, never typed to make the
            registry walk pass, and the ``note`` says how and when.

    """

    source: str
    series_id: str
    unit: str
    frequency: Frequency
    transform: str = "level"
    verified: bool = True
    last_observed: date | None = None
    note: str = ""
    publication_lag_days: int | None = None

    @property
    def fetchable(self) -> bool:
        """True when a machine can retrieve this without an operator typing it."""
        return self.verified and self.source != SOURCE_MANUAL

    def stale_on(self, asof: date, max_staleness_days: int) -> bool:
        """Say whether this ref's newest observation is too old to use.

        Args:
            asof: The date to age against.
            max_staleness_days: The indicator's allowance.

        Returns:
            True when the ref is stale, including when ``last_observed`` is
            unknown. Unknown freshness counts as stale on purpose: an
            unverifiable number should not quietly earn a currency a score.

        """
        if self.last_observed is None:
            return True
        return (asof - self.last_observed).days > max_staleness_days


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Everything the engine knows about one canonical indicator.

    Attributes:
        key: The canonical indicator key, repeated here so a spec passed around
            on its own still knows its own name.
        pillar: Which pillar consumes this indicator. One pillar owns each
            indicator; an indicator wanted by two pillars is a sign the pillar
            boundary is drawn in the wrong place.
        unit: Canonical unit after ``transform`` is applied. This is what the
            pillar can assume, regardless of what the underlying source
            published.
        frequency: The indicator's typical release frequency across the
            universe. Per-currency reality lives on each `SeriesRef`.
        description: What the number means and why the pillar wants it.
        series: Per-currency refs. A currency absent from this mapping has no
            source at all for this indicator, which is different from having a
            manual one.

    """

    key: str
    pillar: PillarName
    unit: str
    frequency: Frequency
    description: str
    series: Mapping[str, SeriesRef] = field(default_factory=dict)


def _ref(
    source: str,
    series_id: str,
    unit: str,
    frequency: Frequency,
    last_observed: date | None,
    transform: str = "level",
    note: str = "",
    verified: bool = True,
    lag: int | None = None,
) -> SeriesRef:
    """Build a `SeriesRef`. Exists only to keep the tables below readable.

    ``lag`` is `SeriesRef.publication_lag_days`, shortened because it appears
    on forty legs and the field name would push every one onto a second line.
    """
    return SeriesRef(
        source=source,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform=transform,
        verified=verified,
        last_observed=last_observed,
        note=note,
        publication_lag_days=lag,
    )


def _manual(
    series_id: str,
    unit: str,
    frequency: Frequency,
    note: str,
) -> SeriesRef:
    """Build a manual `SeriesRef`.

    Manual refs are always unverified with no ``last_observed``. Neither flag
    comments on the operator's typing: together they record that no free
    machine-readable source was found, which is the fact a coverage report
    needs to surface.
    """
    return SeriesRef(
        source=SOURCE_MANUAL,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        transform="level",
        verified=False,
        last_observed=None,
        note=note,
    )


def _cftc(series_id: str, last_observed: date, note: str = "") -> SeriesRef:
    """Build a CFTC `SeriesRef` for a currency futures contract market code.

    The unit is the percent of open interest, not the contract count.
    `SeriesRef.unit` is echoed onto every `Observation`, and
    `fbe.datasources.cot` divides the leveraged-funds net by open interest and
    scales it before emitting, so naming contracts here would put a unit on the
    observation that its value is not in. Issue #174 is where that division
    landed and ADR 0011 records the scale.

    The dollar ref is the loose one. Its value is the negated sum of the seven
    other legs, so it is a sum of seven percents of seven different
    denominators rather than a percent of any one open interest, and it sits on
    roughly seven times their scale. It carries this unit because the scale of
    its terms is the closest true thing and because `_observation` copies the
    unit from the ref, not because the label is exact. Whether the derived leg
    deserves a unit of its own is recorded as open in ADR 0011.
    """
    return SeriesRef(
        source=SOURCE_CFTC,
        series_id=series_id,
        unit="percent_of_open_interest",
        frequency=Frequency.WEEKLY,
        transform="net_position",
        verified=True,
        last_observed=last_observed,
        note=note,
    )


# Recurring notes, written once so the tables stay scannable and so a change of
# fact is a change in one place.
_EA_AGGREGATE_DEAD = (
    "euro-area aggregate stopped updating; German national series used as the "
    "euro-area proxy"
)
_PMI_LICENSED = (
    "PMIs are licensed by S&P Global and ISM and are on no free API; operator "
    "enters the headline print by hand"
)
_FRED_LAG = (
    "the largest first-appearance lag on FRED over the twelve prints to 2026-09-21"
)
"""How every measured `SeriesRef.publication_lag_days` on a FRED leg was
taken: each of the series' twelve newest observations' ``realtime_start`` on
FRED's archive (``output_type=4``, initial releases only) minus its period
start, and the largest of the twelve kept. The largest rather than the median
because a lag that is too short flatters a backtest and a lag that is too long
only delays a figure the engine would have seen a little sooner."""

_OECD_FRESHER = (
    "taken from the OECD API rather than FRED's mirror of the same OECD "
    "material, which runs two months behind"
)


POLICY_RATE = IndicatorSpec(
    key="policy_rate",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    description=(
        "The central bank's target rate, or the overnight rate that tracks it. "
        "The level matters less than where it sits relative to the rest of the "
        "G10, which is the whole premise of a relative-value framework."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DFEDTARU",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="fed funds target range upper limit, published daily",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "ECBDFR",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="ECB deposit facility rate, the effective policy rate",
        ),
        "GBP": _ref(
            SOURCE_BOE,
            "IUDBEDR",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 1),
            note=(
                "Bank Rate itself from the Bank of England database, which "
                "replaces the earlier SONIA proxy. lag: 8 days, the age of the "
                "newest session on VERIFIED_ON; the database refused an "
                "automated re-check on 2026-09-21 with HTTP 403, so this is "
                "the one measurement held"
            ),
            lag=8,
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/JPN.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"uncollateralised overnight call rate; {_OECD_FRESHER}",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "SARON-area overnight rate. Closes a real gap: FRED's "
                "IRSTCI01CHM156N stopped at 2024-03."
            ),
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"overnight money market rate; {_OECD_FRESHER}",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"interbank overnight cash rate; {_OECD_FRESHER}",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.IRSTCI.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "overnight interbank rate. Closes a real gap: FRED's "
                "IRSTCI01NZM156N stopped at 2024-12."
            ),
        ),
    },
)


YIELD_2Y = IndicatorSpec(
    key="yield_2y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.DAILY,
    description=(
        "Two-year government bond yield, the market's own forecast of where "
        "policy goes next. The 2y differential is the strongest single "
        "fundamental driver of a G10 pair over a multi-week horizon, and the "
        "monetary pillar derives most of its sub-weight from it, so a missing "
        "leg here costs a currency the heaviest pillar in the model."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DGS2",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Treasury constant maturity",
        ),
        "EUR": _ref(
            SOURCE_ECB,
            "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "euro-area AAA government spot curve, 2-year point, Svensson "
                "fit. AAA issuers only, so this is close to the Bund and does "
                "not carry periphery spread."
            ),
        ),
        "GBP": _ref(
            SOURCE_BOE,
            "GLC_NOMINAL_SPOT_SHORT/2.0",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "UK nominal spot curve, 2-year point, from the yield curve "
                "archive. Read from sheet '3. spot, short end', the column "
                "whose header maturity is 2.0 years. Needs a spreadsheet "
                "reader, unlike every other source here."
            ),
        ),
        "JPY": _ref(
            SOURCE_MOF_JP,
            "2Y",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "JGB par yield, 2-year column of the Ministry of Finance CSV. "
                "The current-month file and the full history are separate "
                "downloads; both are needed."
            ),
        ),
        "CHF": _manual(
            "yield_2y",
            "percent",
            Frequency.DAILY,
            "the SNB publishes a Confederation spot curve and the endpoint is "
            "verified (cube 'rendoblid', dimension '2J'), but it stopped at "
            "2025-07-31 while the rest of the SNB portal stayed current. No "
            "free replacement found. Enter by hand or accept that the Swiss "
            "franc runs the monetary pillar without a front end.",
        ),
        "CAD": _ref(
            SOURCE_BOC,
            "BD.CDN.2YR.DQ.YLD",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Government of Canada 2-year benchmark bond yield",
        ),
        "AUD": _ref(
            SOURCE_RBA,
            "FCMYGBAG2D",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 2),
            note=(
                "Australian Government 2-year bond, interpolated, from RBA "
                "statistical table F2. lag: 7 days; the table is a daily series "
                "updated in batches, and its newest session was 7 days old on "
                "VERIFIED_ON and 5 days old on a live read on 2026-09-21"
            ),
            lag=7,
        ),
        "NZD": _ref(
            SOURCE_RBNZ,
            "INM.DG102.NZZCF",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "secondary market government bond closing yield, 2 year, from "
                "RBNZ table B2 (hb2-daily-close.xlsx), column located by this "
                "series ID. Verified by the owner from a residential or mobile "
                "connection on 2026-09-15, not from a run: the RBNZ answers "
                "every data-centre or cloud egress with HTTP 403 and a "
                "JavaScript challenge, so no unattended run can repeat the "
                "check or the fetch. The 2-year column is blank for most of "
                "2020, which a backtest over that year must know."
            ),
        ),
    },
)


def _yield_change_series(transform: str, note_suffix: str) -> Mapping[str, SeriesRef]:
    """Reuse `YIELD_2Y`'s identifiers for a momentum indicator derived from them.

    Args:
        transform: ``"chg_1m"`` or ``"chg_3m"``, from `TRANSFORMS`.
        note_suffix: Appended to each ref's existing note, explaining the
            derivation without losing the original per-currency context (the
            AAA-curve caveat on EUR, the spreadsheet dependency on GBP, and so
            on).

    Returns:
        One `SeriesRef` per currency in `YIELD_2Y.series`, identical to the
        level ref in ``source``, ``series_id``, ``unit``, ``frequency`` and
        ``last_observed``, differing in ``transform`` and ``note``, and with
        ``verified`` False.

    Why this exists rather than a second literal table. No source publishes a
    pre-differenced two-year yield change for any G10 issuer, so
    ``yield_2y_chg_1m`` and ``yield_2y_chg_3m`` cannot be verified against a
    live endpoint the way every other indicator in this file was: there is
    nothing to fetch that is not `yield_2y` itself. Building them from
    `YIELD_2Y.series` rather than retyping eight entries twice keeps the two
    change indicators unable to drift from the level they are computed from:
    a currency added to, or dropped from, `YIELD_2Y` changes both change
    indicators the moment this function runs again, rather than needing three
    tables edited in step.

    Why ``verified`` is False rather than copied. The level ref's identifier
    was confirmed live, but confirming it says nothing about the change:
    `fbe.datasources.fred` and `fbe.datasources.curves` both pass these refs
    over because no source computes the ADR 0004 derivation yet. Copying
    ``verified`` made `stale_refs` name CHF and AUD as the only gaps while
    nothing served the other six either, which is the quiet misreport issue
    #169 is about. The flag flips back in the commit that implements the
    derivation, since that commit is what makes the ref retrievable.

    """
    return {
        code: replace(
            ref,
            transform=transform,
            verified=False,
            note=f"{ref.note}; {note_suffix}" if ref.note else note_suffix,
        )
        for code, ref in YIELD_2Y.series.items()
    }


YIELD_2Y_CHG_1M = IndicatorSpec(
    key="yield_2y_chg_1m",
    pillar=PillarName.MONETARY,
    unit="basis_points",
    frequency=Frequency.DAILY,
    description=(
        "One-month change in the two-year government bond yield, in basis "
        "points, resampled to month-end before differencing. Carries a fifth "
        "of the monetary pillar on its own: the direction of repricing "
        "typically leads the level in FX. There is no separately published "
        "series for this anywhere; see `_yield_change_series` for how it is "
        "derived from `yield_2y` rather than sourced independently. Coverage "
        "and freshness are therefore identical to `yield_2y`'s currency by "
        "currency: CHF is manual for the same reason the level is."
    ),
    series=_yield_change_series(
        "chg_1m",
        "derived: one-month, month-end-resampled change in this same series, "
        "in basis points, not a separately published number",
    ),
)


YIELD_2Y_CHG_3M = IndicatorSpec(
    key="yield_2y_chg_3m",
    pillar=PillarName.MONETARY,
    unit="basis_points",
    frequency=Frequency.DAILY,
    description=(
        "Three-month change in the two-year government bond yield, in basis "
        "points, resampled to quarter-end before differencing. The single "
        "heaviest sub-indicator in the model at 0.25 of the monetary pillar, "
        "which is itself the heaviest pillar in the composite. Derived from "
        "`yield_2y` the same way `yield_2y_chg_1m` is; see "
        "`_yield_change_series`. A currency without a live `yield_2y` cannot "
        "have a live reading here either."
    ),
    series=_yield_change_series(
        "chg_3m",
        "derived: three-month, quarter-end-resampled change in this same "
        "series, in basis points, not a separately published number",
    ),
)


YIELD_10Y = IndicatorSpec(
    key="yield_10y",
    pillar=PillarName.MONETARY,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Ten-year benchmark government bond yield. Registered and verified "
        "across all eight with clean current coverage, and consumed by no "
        "pillar today: MONETARY is attributed here but asks for five "
        "components and none is a 10-year series. Held ready, so read its "
        "coverage as availability rather than as a live input. Specifically "
        "not a fallback for yield_2y, and not what CHF falls back on when "
        "its 2-year is missing, because there is no such fallback. "
        "Section 3.1 of docs/scoring-spec.md values the 2-year as the priced "
        "policy path, while a 10-year is dominated by term premium and "
        "long-run growth and inflation expectations, so it answers a "
        "different question. Whether MONETARY gains a 10-year term is a "
        "scoring decision and is open."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "DGS10",
            "percent",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="Treasury constant maturity, daily",
        ),
        "EUR": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/DEU.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"10y Bund, the euro-area benchmark; {_OECD_FRESHER}",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/GBR.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/JPN.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.IRLT.PA......",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=_OECD_FRESHER,
        ),
    },
)


CPI_YOY = IndicatorSpec(
    key="cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Headline consumer price inflation, year on year. The inflation pillar "
        "scores the gap to each central bank's target rather than the raw "
        "print, so `CurrencyMeta.inflation_target` is the other half of this "
        "input. Coverage here was two of eight until the OECD's own API "
        "replaced FRED's frozen mirror."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "CPIAUCSL",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="CPI-U all items, seasonally adjusted index, from the BLS",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "CP0000EZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="Eurostat HICP all items, euro area 19, a true bloc aggregate",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="national CPI, growth over one year. FRED's mirror froze at 2025-03.",
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note=(
                "Japan sits in the COICOP 2018 dataflow, not the 1999 one. "
                "FRED's mirror froze at 2021-06, a five-year hole."
            ),
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CHE.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note="COICOP 2018 dataflow. FRED's mirror froze at 2025-04.",
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/CAN.M.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="national CPI. FRED's mirror froze at 2025-03.",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly by publication, not by choice; 2026Q2",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._T.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly; 2026Q2. FRED's mirror froze at 2023Q3.",
        ),
    },
)


CORE_CPI_YOY = IndicatorSpec(
    key="core_cpi_yoy",
    pillar=PillarName.INFLATION,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Consumer prices excluding food and energy, year on year. Central banks "
        "react to this more than to the headline, so it leads policy and "
        "therefore leads the currency."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "CPILFESL",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="CPI-U less food and energy, seasonally adjusted index",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "00XEFDEZ19M086NEST",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="yoy",
            note="Eurostat HICP excluding energy, food, alcohol and tobacco",
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="all items less food and energy",
        ),
        "JPY": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="COICOP 2018 dataflow",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG"
            "/CHE.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                "the only Swiss core series found anywhere free: it is absent "
                "from both general price dataflows and lives in the dedicated "
                "core flow"
            ),
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CAN.M.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="COICOP 2018 dataflow; the 1999 flow has no Canadian core",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="2026Q2. Not the RBA's trimmed mean, which is its preferred cut.",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._TXCP01_NRG.N.GY",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="2026Q2. Not the RBNZ sectoral factor model estimate.",
        ),
    },
)


GDP_YOY = IndicatorSpec(
    key="gdp_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.QUARTERLY,
    description=(
        "Real GDP growth, year on year. Slow and heavily revised, so it anchors "
        "the growth pillar rather than driving it. Full G10 coverage, which is "
        "rare enough in this registry to be worth stating."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "GDPC1",
            "billions_chained_usd",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="real GDP, SAAR chained 2017 dollars; 2026Q2",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "CLVMNACSCAB1GQEA19",
            "millions_chained_eur",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="Eurostat real GDP, euro area 19, a true bloc aggregate; 2026Q2",
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCGBQ",
            "millions_chained_gbp",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNRGDPEXP",
            "billions_chained_jpy",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="real GDP by expenditure; 2026Q2",
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "CLVMNACSCAB1GQCH",
            "millions_chained_chf",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCCAQ",
            "millions_chained_cad",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "NGDPRSAXDCAUQ",
            "millions_chained_aud",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="yoy",
            note="2026Q2",
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "NZLGDPRQPSMEI",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note=(
                "already published as a year-on-year growth rate, so no "
                "transform; 2026Q1"
                f". lag: 198 days, {_FRED_LAG}"
            ),
            lag=198,
        ),
    },
)


UNEMPLOYMENT_RATE = IndicatorSpec(
    key="unemployment_rate",
    pillar=PillarName.EMPLOYMENT,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Harmonised unemployment rate. Compared cross-sectionally against the "
        "rest of the G10 and against its own recent trend, since the level that "
        "counts as full employment differs by country."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "UNRATE",
            "percent",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note="BLS headline U-3, roughly one month behind",
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "LRHUTTTTDEM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                f"German harmonised rate. {_EA_AGGREGATE_DEAD}: "
                "LRHUTTTTEZM156S stopped at 2023-01."
                f". lag: 103 days, {_FRED_LAG}"
            ),
            lag=103,
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "LRHUTTTTGBM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note=(f"lag: 169 days, {_FRED_LAG}"),
            lag=169,
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "LRHUTTTTJPM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 77 days, {_FRED_LAG}"),
            lag=77,
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "LRUN64TTCHQ156S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note=(
                "quarterly ILO rate, aged 15-64; Switzerland publishes no "
                "monthly harmonised rate on FRED. 2026Q1."
                f". lag: 226 days, {_FRED_LAG}"
            ),
            lag=226,
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "LRHUTTTTCAM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "LRHUTTTTAUM156S",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 77 days, {_FRED_LAG}"),
            lag=77,
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "LRHUTTTTNZQ156S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            note="quarterly by publication; 2026Q2",
        ),
    },
)


EMPLOYMENT_CHG = IndicatorSpec(
    key="employment_chg",
    pillar=PillarName.EMPLOYMENT,
    unit="persons",
    frequency=Frequency.MONTHLY,
    description=(
        "Change in the number of people employed. The flow, not the stock: a "
        "falling unemployment rate driven by people leaving the labour force is "
        "a different signal from one driven by hiring, and this indicator is "
        "what separates them."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "PAYEMS",
            "thousands_of_persons",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            transform="diff",
            note="total nonfarm payrolls; the differenced level is the NFP headline",
        ),
        "EUR": _manual(
            "employment_chg",
            "persons",
            Frequency.QUARTERLY,
            "no live euro-area or German employment level on FRED "
            "(LFEMTTTTEZQ647S stopped at 2022-10); take the Eurostat quarterly "
            "employment release by hand",
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "LFEMTTTTGBQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            transform="diff",
            note=(f"2026Q1. lag: 196 days, {_FRED_LAG}"),
            lag=196,
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "LFEMTTTTJPM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            transform="diff",
            note=(f"lag: 77 days, {_FRED_LAG}"),
            lag=77,
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "LFEMTTTTCHQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            transform="diff",
            note=(f"2026Q1. lag: 226 days, {_FRED_LAG}"),
            lag=226,
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "LFEMTTTTCAM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            transform="diff",
            note="",
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "LFEMTTTTAUM647S",
            "persons",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            transform="diff",
            note=(f"lag: 77 days, {_FRED_LAG}"),
            lag=77,
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "LFEMTTTTNZQ647S",
            "persons",
            Frequency.QUARTERLY,
            date(2026, 4, 1),
            transform="diff",
            note="2026Q2",
        ),
    },
)


def _employment_level_series() -> Mapping[str, SeriesRef]:
    """Reuse `EMPLOYMENT_CHG`'s identifiers for the stock they are differenced from.

    Returns:
        One `SeriesRef` per currency whose `employment_chg` entry reaches the
        flow through ``diff``, identical to it in ``source``, ``series_id``,
        ``unit``, ``frequency``, ``verified`` and ``last_observed``, and
        differing only in ``transform`` and ``note``. Currencies whose flow is
        not derived from a published level are absent, which today means EUR
        alone.

    ``last_observed`` is inherited rather than re-measured, and that is correct
    rather than convenient: a ``diff`` transform drops the *first* observation
    of a series and never the last, so the differenced series and the level it
    is differenced from necessarily share a newest period. The property depends
    entirely on the transform being ``diff``, which is what the filter below
    selects on, so widening that filter would break it.

    Why the refs are reused rather than retyped. ``employment_trend`` divides a
    change in employment by the level of employment, and the two only cancel if
    they describe the same population measured the same way. Seven of the eight
    flows are already a published level under ``diff``, so the stock is those
    same series under ``level``, and building the table from
    `EMPLOYMENT_CHG.series` makes it impossible for one currency's identifier to
    drift between the two keys. A retyped second table would let that happen
    silently, and the result would be a percentage of the wrong workforce, which
    is in range and plausible. `_yield_change_series` exists for the same reason
    in the other direction.

    Why EUR is filtered rather than listed. Its flow is a manual entry carrying
    ``transform="level"``: the euro-area employment level stopped publishing at
    2022-10 and the number is keyed in from the Eurostat release by hand, so
    there is no series to take a stock from. The filter is on the transform
    rather than on the currency code so that a EUR flow sourced from a published
    level in future gains its stock here without anyone remembering to add it.

    No substitute is invented for EUR. `blend_components` renormalises over the
    sub-weight present and `MIN_COMPONENT_WEIGHT` decides what is left, which
    for EMPLOYMENT's two equal components means the pillar is absent for that
    currency. A German level is current and is deliberately not used: EUR's flow
    is a euro-area figure and dividing it by one member state's workforce would
    overstate hiring by roughly a factor of four.

    """
    return {
        code: replace(
            ref,
            transform="level",
            note=(
                f"{ref.note}; the level `employment_chg` is differenced from"
                if ref.note
                else "the level `employment_chg` is differenced from"
            ),
        )
        for code, ref in EMPLOYMENT_CHG.series.items()
        if ref.transform == "diff"
    }


EMPLOYMENT_LEVEL = IndicatorSpec(
    key="employment_level",
    pillar=PillarName.EMPLOYMENT,
    unit="persons",
    frequency=Frequency.MONTHLY,
    description=(
        "Number of people employed. The stock that `employment_chg` is the flow "
        "of, and it exists for one purpose: `employment_trend` is specified as "
        "an annualised percent of the employment level, and without a "
        "denominator the component would score a raw count. A US payrolls print "
        "is in the hundreds of thousands and a New Zealand quarterly change is "
        "in the thousands, so a cross-sectional z-score of the count ranks the "
        "size of the economies with a little hiring information on top. Section "
        "3.4 of docs/scoring-spec.md rejects that explicitly.\n"
        "\n"
        "Derived from `employment_chg`'s own refs rather than sourced "
        "separately; see `_employment_level_series`. Coverage, freshness and "
        "verification are therefore identical to `employment_chg`'s currency by "
        "currency, with one exception: EUR has no entry here at all, because "
        "its flow is keyed in by hand and has no published level behind it.\n"
        "\n"
        "The allowance is `employment_chg`'s 270 days, and necessarily so: the "
        "same publication carries both, so a stock judged on a tighter "
        "allowance than the flow it scales would expire first and take the "
        "component out while the numerator was still current.\n"
        "\n"
        "USD arrives in thousands of persons while this key's canonical unit is "
        "persons, inherited from `PAYEMS` through `employment_chg`. The "
        "component that reads it is a ratio of two numbers from that same "
        "series, so the scale cancels and the mismatch cannot reach a score. It "
        "is recorded here because a reader comparing this level with another "
        "currency's would otherwise find the United States a thousand times "
        "smaller than Japan."
    ),
    series=_employment_level_series(),
)


RETAIL_SALES_YOY = IndicatorSpec(
    key="retail_sales_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Retail trade volume, year on year. The fastest read on household "
        "demand, and the growth pillar's main monthly input given that GDP "
        "arrives quarterly and late."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "USASLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(
                "OECD retail volume growth, chosen over the fresher US-only "
                "RSAFS so the eight legs are measured the same way"
                f". lag: 136 days, {_FRED_LAG}"
            ),
            lag=136,
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "DEUSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(
                f"{_EA_AGGREGATE_DEAD}: EA19SLRTTO01GYSAM stopped at 2023-10. "
                f"lag: 106 days, {_FRED_LAG}"
            ),
            lag=106,
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "GBRSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 77 days, {_FRED_LAG}"),
            lag=77,
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(f"lag: 134 days, {_FRED_LAG}"),
            lag=134,
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "CHESLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(f"lag: 106 days, {_FRED_LAG}"),
            lag=106,
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "CANSLRTTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note=(f"lag: 139 days, {_FRED_LAG}"),
            lag=139,
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "SLRTTO01AUQ659S",
            "percent",
            Frequency.QUARTERLY,
            date(2025, 4, 1),
            note=(
                "DISCONTINUED at 2025Q2. Australia has no live retail series "
                "on FRED and none was found on the OECD API either. Unverified "
                "since #222: 526 days old on VERIFIED_ON against a measured "
                "first-appearance lag of 192, so dead rather than late."
            ),
            verified=False,
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "SLRTTO01NZQ659S",
            "percent",
            Frequency.QUARTERLY,
            date(2026, 1, 1),
            note=(f"2026Q1. lag: 284 days, {_FRED_LAG}"),
            lag=284,
        ),
    },
)
"""The 380-day allowance is interim, for the same reason as `TRADE_BALANCE`'s:
#222 measured the New Zealand quarterly leg arriving 284 days after the
period starts, and 270 admitted it at zero weight. It goes with #126."""


INDPRO_YOY = IndicatorSpec(
    key="indpro_yoy",
    pillar=PillarName.GROWTH,
    unit="percent",
    frequency=Frequency.MONTHLY,
    description=(
        "Industrial production, year on year. Coverage here is the worst of the "
        "growth inputs: four of eight are live. Weight it accordingly, or the "
        "growth pillar ends up scoring the countries that happen to publish "
        "rather than the countries that happen to be growing."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "USAPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                "OECD basis for cross-country comparability; INDPRO is the "
                "fresher US-only alternative"
                f". lag: 106 days, {_FRED_LAG}"
            ),
            lag=106,
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "DEUPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2023, 12, 1),
            note=(
                f"DISCONTINUED at 2023-12. {_EA_AGGREGATE_DEAD}, and the "
                "German proxy has now stopped too. Unverified since #222: the "
                "newest print was first carried on 2024-04-10 and nothing has "
                "followed, so dead rather than late."
            ),
            verified=False,
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "GBRPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(f"lag: 108 days, {_FRED_LAG}"),
            lag=108,
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "JPNPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(f"lag: 108 days, {_FRED_LAG}"),
            lag=108,
        ),
        "CHF": _manual(
            "indpro_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no Swiss industrial production series on FRED in any live form",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "CANPRINTO01GYSAM",
            "percent",
            Frequency.MONTHLY,
            date(2026, 4, 1),
            note=(f"lag: 139 days, {_FRED_LAG}"),
            lag=139,
        ),
        "AUD": _manual(
            "indpro_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no Australian industrial production series on FRED",
        ),
        "NZD": _manual(
            "indpro_yoy",
            "percent",
            Frequency.QUARTERLY,
            "no New Zealand industrial production series on FRED",
        ),
    },
)


PMI_COMPOSITE = IndicatorSpec(
    key="pmi_composite",
    pillar=PillarName.GROWTH,
    unit="index",
    frequency=Frequency.MONTHLY,
    description=(
        "Composite purchasing managers' index, manufacturing and services "
        "blended, 50 being the expansion line. "
        "**Registered but consumed by nothing**, and listed in "
        "``UNCONSUMED_INDICATORS``. It held GROWTH's leading-survey slot until "
        "issue #23, which ruled that the slot takes "
        "``business_confidence_mfg`` at the same 0.30 instead. The reason is "
        "coverage rather than quality: this series is licensed, 0/8 on free "
        "coverage, and arrives only in months an operator keys eight numbers "
        "in by hand, so a slot the spec describes as holding one leading "
        "survey held nothing on almost every run, and three currencies fell "
        "through the component floor and lost GROWTH outright. It is kept "
        "registered rather than deleted because re-adopting it if it is ever "
        "licensed is then a one-line change. See ADR 0005. "
        "Named ``pmi_composite`` rather than ``pmi_manufacturing`` because "
        "the growth pillar wants the whole-economy read, manufacturing being "
        "a small and shrinking share of most G10 economies; see "
        "`fbe.pillars.growth.GrowthPillar`. Until an operator has both a "
        "manufacturing and a services print to blend, entering the "
        "manufacturing headline alone under this key is a stated "
        "approximation, not silent: record it as such in the manual entry's "
        "``meta``, per `docs/answers/data.md` question 5, which found a free "
        "OECD business-confidence proxy worth a future, separate indicator "
        "rather than a value folded into this one, since its unit is a "
        "percentage balance, not a 50-centred diffusion index, and blending "
        "the two under one key would misscore every observation."
        "The allowance is 75 rather than 45 because 45 is the age of a "
        "punctual monthly print under first-day period stamping, so the "
        "series was expiring on the day it published. 75 is this table's own "
        "rule: a month elapsing, the survey's own lag, and one more month "
        "before the next print is due."
    ),
    series={
        code: _manual("pmi_composite", "index", Frequency.MONTHLY, _PMI_LICENSED)
        for code in G10
    },
)


TRADE_BALANCE = IndicatorSpec(
    key="trade_balance",
    pillar=PillarName.EXTERNAL,
    unit="usd",
    frequency=Frequency.MONTHLY,
    description=(
        "Merchandise trade balance in US dollars, seasonally adjusted. Already "
        "currency-converted by the source, so the eight legs are directly "
        "comparable without an FX step. Full, current G10 coverage."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "XTNTVA01USM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(
                "OECD basis for comparability; BOPGSTB is the fresher US-only "
                "goods and services balance"
                f". lag: 165 days, {_FRED_LAG}"
            ),
            lag=165,
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "XTNTVA01DEM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 5, 1),
            note=(
                f"{_EA_AGGREGATE_DEAD}: XTNTVA01EZM667S stopped at 2022-12. "
                "Germany runs a structural surplus larger than the bloc's, so "
                "this proxy flatters the euro."
                f". lag: 138 days, {_FRED_LAG}"
            ),
            lag=138,
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "XTNTVA01GBM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 134 days, {_FRED_LAG}"),
            lag=134,
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "XTNTVA01JPM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 134 days, {_FRED_LAG}"),
            lag=134,
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "XTNTVA01CHM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 258 days, {_FRED_LAG}"),
            lag=258,
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "XTNTVA01CAM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 165 days, {_FRED_LAG}"),
            lag=165,
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "XTNTVA01AUM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 108 days, {_FRED_LAG}"),
            lag=108,
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "XTNTVA01NZM667S",
            "usd",
            Frequency.MONTHLY,
            date(2026, 6, 1),
            note=(f"lag: 108 days, {_FRED_LAG}"),
            lag=108,
        ),
    },
)
"""The 300-day allowance is interim. The cadence alone justifies about 150,
but #222 measured FRED's mirror of these eight legs arriving up to 258 days
after the period starts, and under the ramp in `fbe.scoring.freshness` an
allowance below the lag admits a print already at zero weight. The number is
the largest measured lag plus one cycle, rounded up, and it goes when #126
replaces this table with the lag and cycle themselves."""


GDP_NOMINAL_USD = IndicatorSpec(
    key="gdp_nominal_usd",
    pillar=PillarName.EXTERNAL,
    unit="usd",
    frequency=Frequency.ANNUAL,
    description=(
        "Nominal gross domestic product at market prices, in actual US dollars, "
        "from the World Bank's national accounts through FRED. The denominator "
        "``trade_trend`` is taken over, and nothing else consumes it. "
        "**Actual dollars, not millions or billions.** ``trade_balance`` is "
        "published on the same scale, so the ratio is taken between two "
        "quantities in one unit with no conversion step, which is the whole "
        "reason issue #158 was ruled this way: a conversion is where this "
        "project's worst recorded defect came from. A series filed here in "
        "national currency, or in millions, gives a plausible number for every "
        "currency and raises nothing, because the cross-sectional z-score "
        "absorbs a common factor exactly. "
        "Annual rather than quarterly, and that was measured rather than "
        "preferred. The OECD quarterly family ``*GDPNQDSMEI`` resolves for all "
        "eight and is national currency, and it stopped publishing at "
        "2023-07-01, so converting it would buy an FX step, a date convention "
        "and a series three years stale. There is nothing to convert. "
        "The allowance of 916 days follows `IndicatorSpec.max_staleness_days`' "
        "own rule from a measured lag: the 2025 reference year was published on "
        "2026-07-07, which is 188 days after the year ended, so the newest print "
        "is 916 days old on the day before its successor is due. On a 2026-09 "
        "run that leaves ``trade_trend`` entering at about half of its declared "
        "0.30 rather than at full weight. Whether ageing a scaling constant like "
        "a signal is right at all is argued on #126; it is not decided here. "
        "Verified 8 of 8 on 2026-09-18, one year behind on every leg."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "MKTGDPUSA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "EUR": _ref(
            SOURCE_FRED,
            "MKTGDPDEA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "GBP": _ref(
            SOURCE_FRED,
            "MKTGDPGBA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "MKTGDPJPA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "CHF": _ref(
            SOURCE_FRED,
            "MKTGDPCHA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "MKTGDPCAA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "MKTGDPAUA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
        "NZD": _ref(
            SOURCE_FRED,
            "MKTGDPNZA646NWDB",
            "usd",
            Frequency.ANNUAL,
            date(2025, 1, 1),
        ),
    },
)


CURRENT_ACCOUNT_GDP = IndicatorSpec(
    key="current_account_gdp",
    pillar=PillarName.EXTERNAL,
    unit="percent_of_gdp",
    frequency=Frequency.QUARTERLY,
    description=(
        "Current account balance as a share of GDP. A structural measure of "
        "whether a currency is financed by the world or financing it."
    ),
    series={
        code: _ref(
            SOURCE_FRED,
            series_id,
            "percent_of_gdp",
            Frequency.QUARTERLY,
            date(2024, 10, 1),
            note=(
                "DISCONTINUED at 2024Q4, as is every leg of this family. No "
                "free replacement was found: the OECD API's balance of "
                "payments dataflows cover trade in services and merchandise, "
                "not the quarterly current account balance. Unverified since "
                "#222: 708 days old on VERIFIED_ON against a measured "
                "first-appearance lag under 300, so it is dead rather than late."
            ),
            verified=False,
        )
        for code, series_id in (
            ("USD", "USAB6BLTT02STSAQ"),
            ("EUR", "DEUB6BLTT02STSAQ"),
            ("GBP", "GBRB6BLTT02STSAQ"),
            ("JPY", "JPNB6BLTT02STSAQ"),
            ("CHF", "CHEB6BLTT02STSAQ"),
            ("CAD", "CANB6BLTT02STSAQ"),
            ("AUD", "AUSB6BLTT02STSAQ"),
            ("NZD", "NZLB6BLTT02STSAQ"),
        )
    },
)
"""The 210-day allowance is what a quarterly balance-of-payments release
honestly justifies: the quarter has to end, the statistics office needs about
two months, and the next quarter is then already half over. It is deliberately
not set high enough to let the frozen 2024Q4 data through. Setting it to 700
would make the indicator report as covered while feeding the model numbers two
years old, which is the exact failure this registry exists to prevent. As it
stands the indicator correctly reports zero coverage until a live source is
found, and the external pillar leans on `trade_balance`, which is current for
all eight."""


COT_NET_PCT_OI = IndicatorSpec(
    key="cot_net_pct_oi",
    pillar=PillarName.POSITIONING,
    unit="percent_of_open_interest",
    frequency=Frequency.WEEKLY,
    description=(
        "Net speculative position in CME currency futures from the CFTC "
        "Commitments of Traders report, as a percent of open interest. A "
        "crowded position is a reason to fade a fundamental view, not to add "
        "to it, so this pillar usually works against the others by design. "
        "``(lev_money_positions_long - lev_money_positions_short) / "
        "open_interest_all * 100`` on the Traders in Financial Futures "
        "futures-only dataset, which `fbe.datasources.cot` performs, so "
        "what arrives under this key is that percent and not the contract "
        "count the key held under its previous name. "
        "**Leveraged funds, not non-commercial.** Issue #174's ruling settled "
        "that and `docs/decisions/0011-positioning-reads-leveraged-funds.md` "
        "records it. Leveraged funds are the money whose crowding "
        "mean-reverts, which is the property section 3.6 of "
        "`docs/scoring-spec.md` reasons from; non-commercial is a Legacy "
        "report category that TFF does not carry, and reassembling it from "
        "TFF's asset manager, leveraged funds and other reportable columns "
        "discards the only thing the TFF report buys. "
        "**A percent, matching the key's name.** Section 3.6 wrote the "
        "quantity as a plain division and called it a share, and this "
        "description said the same until #174, against the key's own name, "
        "`fbe.pillars.positioning.PositioningPillar`'s docstring, section "
        "7.4's column header and the section 7 fixture, all of which read a "
        "percent. ADR 0011 settled it for the percent and section 3.6 has "
        "been corrected. The scale is free for the score, since the pillar "
        "z-scores this series against its own history, and it is not free "
        "for a reader: see `fbe.datasources.cot.PERCENT_SCALE`. "
        "The dollar leg carries no contract of its own and is derived: see "
        "its ref note below and `fbe.datasources.cot.CotSource.derive_usd_position`."
    ),
    series={
        "USD": _cftc(
            "098662",
            date(2026, 9, 1),
            "USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The "
            "primary dollar read is the sign-flipped complement of the other "
            "seven; this contract is a small, thinly held cross-check.",
        ),
        "EUR": _cftc("099741", date(2026, 9, 1), "EURO FX, CME, TFF gpe5-46if"),
        "GBP": _cftc("096742", date(2026, 9, 1), "BRITISH POUND, CME"),
        "JPY": _cftc("097741", date(2026, 9, 1), "JAPANESE YEN, CME"),
        "CHF": _cftc("092741", date(2026, 9, 1), "SWISS FRANC, CME"),
        "CAD": _cftc("090741", date(2026, 9, 1), "CANADIAN DOLLAR, CME"),
        "AUD": _cftc("232741", date(2026, 9, 1), "AUSTRALIAN DOLLAR, CME"),
        "NZD": _cftc("112741", date(2026, 9, 1), "NZ DOLLAR, CME"),
    },
)


EQUITY_INDEX = IndicatorSpec(
    key="equity_index",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    description=(
        "Benchmark equity index for each economy. Registered and verified "
        "across all eight with clean current coverage, and consumed by no "
        "pillar today: RISK is attributed here but asks for "
        "`world_equity_index` and `vol_index`, and neither of those is this "
        "key. Held ready, so read its coverage as availability rather than as "
        "a live input. RISK wants one regime number that all eight currencies "
        "share, which the betas in `CurrencyMeta` then re-sign per currency; "
        "eight local indices would give eight regimes and leave the betas with "
        "nothing to act on. Six of the eight refs below are monthly OECD share "
        "price indices in any case, which cannot date a drawdown. Whether a "
        "per-currency equity term belongs in GROWTH, where a local index reads "
        "as an earnings and growth signal rather than as a regime one, is a "
        "scoring decision and is open."
    ),
    series={
        "USD": _ref(
            SOURCE_FRED,
            "SP500",
            "index",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="daily close; FRED holds a rolling ten-year window only",
        ),
        "JPY": _ref(
            SOURCE_FRED,
            "NIKKEI225",
            "index",
            Frequency.DAILY,
            date(2026, 9, 9),
            note="daily close",
        ),
        "EUR": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/DEU.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=(
                f"OECD share price index, monthly average. {_OECD_FRESHER}. "
                "Monthly is slow for a risk pillar: prefer the Stooq daily "
                "feed where it can be made to work, and keep this as the "
                "dependable fallback."
            ),
        ),
        "GBP": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/GBR.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "CHF": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CHE.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "CAD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/CAN.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "AUD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/AUS.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
        "NZD": _ref(
            SOURCE_OECD,
            "DSD_STES@DF_FINMARK/NZL.M.SHARE.IX......",
            "index",
            Frequency.MONTHLY,
            date(2026, 8, 1),
            note=f"OECD share price index; {_OECD_FRESHER}",
        ),
    },
)


WORLD_EQUITY_INDEX = IndicatorSpec(
    key="world_equity_index",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    description=(
        "A single global equity benchmark, currently the S&P 500. **A United "
        "States index is standing in for the world here, and the substitution "
        "is the risk pillar's largest known weakness.** What the pillar "
        "therefore measures is US risk appetite, so a European or Japanese "
        "shock registers only once it crosses the Atlantic. The stand-in is "
        "recorded here, at the point of use, rather than left implicit in a "
        "ticker, because a reader who does not recognise SP500 would otherwise "
        "take a global reading at face value. Replace the ref with a genuine "
        "free daily world index when one can be verified; nothing else has to "
        "change, which is the point of naming the key for what it measures.\n"
        "\n"
        "Separate from `equity_index`, which holds eight per-currency "
        "benchmarks, and deliberately so. A key whose refs are GLOBAL is a "
        "global reading and a key whose refs are per-currency is a "
        "per-currency reading; one key never holds both. Putting a GLOBAL ref "
        "on `equity_index` instead would have silently changed what "
        "`coverage_report` and `identifier_coverage` describe, because both "
        "short-circuit on a GLOBAL ref and report the key on that ref alone, "
        "so the eight would have stopped being counted with nothing raising.\n"
        "\n"
        "The 7-day allowance is `vol_index`'s, and for the same reason: both "
        "are daily closes of the same market, so a gap wider than a long "
        "weekend means the feed has stopped rather than that the exchange was "
        "shut. It is far tighter than `equity_index`'s 75 days because that "
        "key's six monthly refs have to live inside the same number."
    ),
    series={
        GLOBAL: _ref(
            SOURCE_FRED,
            "SP500",
            "index",
            Frequency.DAILY,
            date(2026, 9, 8),
            note=(
                "daily close; FRED holds a rolling ten-year window only. A US "
                "index used as the world proxy, per this key's description"
            ),
        ),
    },
)


VOL_INDEX = IndicatorSpec(
    key="vol_index",
    pillar=PillarName.RISK,
    unit="index",
    frequency=Frequency.DAILY,
    description=(
        "A global volatility benchmark, currently the CBOE's implied "
        "volatility on the S&P 500 (VIX). A single global number, not a "
        "per-currency one: it sets the risk regime, and the currencies then "
        "sort themselves by `CurrencyMeta.risk_beta`. "
        "Named for what it measures, not for the vendor's ticker, matching "
        "every other key in this registry: the day a non-US or a "
        "cross-asset volatility measure is added instead or alongside, it "
        "belongs under this same key, not a new one, or the risk pillar "
        "would need to know which vendor is behind the number it asks for."
    ),
    series={
        GLOBAL: _ref(
            SOURCE_FRED,
            "VIXCLS",
            "index",
            Frequency.DAILY,
            date(2026, 9, 8),
            note="daily close",
        ),
    },
)


COMMODITY_PRICE = IndicatorSpec(
    key="commodity_price",
    pillar=PillarName.EXTERNAL,
    unit="index",
    frequency=Frequency.MONTHLY,
    description=(
        "Terms-of-trade proxy for the commodity currencies, plus a global "
        "benchmark. Only the three currencies with a `commodity_link` in "
        "`CurrencyMeta` carry a specific ref; the others take the global index "
        "or nothing."
    ),
    series={
        GLOBAL: _ref(
            SOURCE_FRED,
            "PALLFNFINDEXM",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="IMF all commodity price index, 2016=100",
        ),
        "CAD": _ref(
            SOURCE_FRED,
            "DCOILWTICO",
            "usd_per_barrel",
            Frequency.DAILY,
            date(2026, 9, 1),
            note=(
                "WTI spot, the standard Canadian dollar terms-of-trade proxy. "
                "lag: 8 days, the largest first-appearance lag on FRED over the "
                "twelve sessions to 2026-09-21; the daily close skips holidays "
                "and the mirror adds a day or two after a long weekend"
            ),
            lag=8,
        ),
        "AUD": _ref(
            SOURCE_FRED,
            "PIORECRUSDM",
            "index",
            Frequency.MONTHLY,
            date(2026, 7, 1),
            note="IMF iron ore price index",
        ),
        "NZD": _manual(
            "commodity_price",
            "index",
            Frequency.IRREGULAR,
            "no dairy price index on FRED, verified by search. The "
            "GlobalDairyTrade auction index is the right series and is "
            "published fortnightly on globaldairytrade.info. PFOODINDEXM is a "
            "poor but free substitute.",
        ),
    },
)


_BTS_LAG_DAYS: Mapping[Frequency, int] = {
    Frequency.MONTHLY: DEFAULT_PUBLICATION_LAG_DAYS[Frequency.MONTHLY],
    Frequency.QUARTERLY: 161,
}
"""Days from a survey period starting to the OECD publishing it, per cadence.

The quarterly figure is measured and is longer than the 120 the cadence table
assumes. The 2026-Q2 print, stamped 2026-04-01, was still the newest one on
`VERIFIED_ON`, 2026-09-09, which is 161 days, so the lag is at least that. One
observation is an upper bound on nothing, but it is a lower bound on the lag,
and understating a lag is the error that costs a punctual print its weight:
``tests/test_business_confidence_indicator.py`` derives the same 161 from the
same print and asserts a current survey never reads stale on any day of its
cycle. The monthly legs publish inside the cadence assumption and take it.

Japan's is the Tankan and the other three follow their own national surveys,
which is why the cadence differs per leg and the lag with it.
"""

BUSINESS_CONFIDENCE_MFG = IndicatorSpec(
    key="business_confidence_mfg",
    pillar=PillarName.GROWTH,
    unit="percentage_balance",
    frequency=Frequency.MONTHLY,
    description=(
        "OECD composite business confidence for manufacturing, from the "
        "Business Tendency Surveys the OECD harmonises out of each country's "
        "own national survey. A net percentage: respondents answering "
        "positively minus those answering negatively. "
        "**Neutral is zero, not 50.** This is not a purchasing managers' "
        "index and must never be blended with ``pmi_composite`` under one "
        "key. A PMI is a diffusion index whose expansion line is 50; a "
        "percentage balance sits either side of zero and is routinely "
        "negative in a healthy economy. Code written against distance from 50 "
        "and pointed at this series would shift every currency in the "
        "universe the same way, the cross-sectional z-score would absorb the "
        "offset, and the ranking would still look orderly with nothing "
        "raising anywhere. That is why this is a separate key rather than a "
        "second source behind the PMI one; see ``docs/answers/data.md`` "
        "question 5 and issue #6. "
        "GROWTH's leading component, at a sub-weight of 0.30, which "
        "``pmi_composite`` cannot fill because it is licensed and 0/8 on free "
        "coverage, so it arrived only in months an operator keyed eight "
        "numbers in by hand. Issue #23 took that scoring decision and ruled "
        "substitution rather than addition: the slot holds one leading "
        "survey, and this is the one that is present. See ADR 0005. "
        "The frequency field says monthly because that is the commoner "
        "cadence, not because it is true of everyone. Four currencies survey "
        "monthly and four quarterly, and each ``SeriesRef`` carries its own. "
        "That mixed cadence is a real cost and not a rounding error: half the "
        "universe would be compared on a number up to a quarter older than "
        "the other half. The component-level freshness discount in "
        "`fbe.pillars.base.BasePillar.component_freshness` is what makes it "
        "visible, and issue #23 weighed it: at its freshest a quarterly leg "
        "enters at an effective 0.192 against the monthly half's 0.30, which "
        "is a permanent cross-sectional inconsistency accepted because the "
        "slot otherwise holds nothing. Issue #126 is where the discount "
        "itself is wrong, and fixing it raises the quarterly half to 0.30. "
        "The allowance is 270 and is derived from the quarterly half, which "
        "is the binding one. This is the figure this table's own rule gives "
        "for a quarterly series stamped on its period's first day, and it is "
        "what ``gdp_yoy`` uses, the other first-day-stamped quarterly input "
        "to this pillar. "
        "The derivation, on the real calendar rather than on nominal "
        "90-day quarters: a print is the newest one until its successor "
        "publishes, which is one further quarter end plus the survey's own "
        "lag. That lag is bounded by observation and not known exactly. The "
        "2026-Q2 print was still the newest on ``VERIFIED_ON``, which puts "
        "the lag at no more than 71 days past the quarter end; one "
        "observation cannot narrow it further. Taking that bound, the worst "
        "case across the four stamp positions is 253 days, reached by a Q3 "
        "print. So 270 covers a punctual print in every quarter, with no day "
        "on which a current series reads stale, while a leg that misses a "
        "whole release reaches 253 + 90 and expires. "
        "An allowance sized from the monthly half would fail four currencies "
        "on day one: the quarterly legs were 161 days old on ``VERIFIED_ON`` "
        "while entirely current."
    ),
    series={
        currency: _ref(
            SOURCE_OECD,
            f"DSD_STES@DF_BTS/{area}.{freq}.BCICP.PB.C.Y...",
            "percentage_balance",
            frequency,
            last_observed,
            note=f"{note} lag: {_BTS_LAG_DAYS[frequency]} days, see _BTS_LAG_DAYS.",
            lag=_BTS_LAG_DAYS[frequency],
        )
        for currency, area, freq, frequency, last_observed, note in (
            (
                "USD",
                "USA",
                "M",
                Frequency.MONTHLY,
                date(2026, 8, 1),
                "US manufacturing business tendency survey as the OECD "
                "compiles it. Not the ISM headline, which is licensed.",
            ),
            (
                "EUR",
                "DEU",
                "M",
                Frequency.MONTHLY,
                date(2026, 8, 1),
                "Germany standing in for the euro area, matching the "
                "REF_AREA convention in fbe.datasources.oecd. The OECD "
                "serves member states here rather than the bloc. Same "
                "survey family as the ifo, not verified to be the ifo "
                "headline number for number.",
            ),
            (
                "GBP",
                "GBR",
                "M",
                Frequency.MONTHLY,
                date(2026, 8, 1),
                "UK manufacturing business tendency survey.",
            ),
            (
                "JPY",
                "JPN",
                "Q",
                Frequency.QUARTERLY,
                date(2026, 4, 1),
                "Quarterly because Japan's survey is the Tankan, which is "
                "quarterly. 2026-Q2 stamped on its first day per issue #27.",
            ),
            (
                "CHF",
                "CHE",
                "M",
                Frequency.MONTHLY,
                date(2026, 8, 1),
                "Swiss manufacturing business tendency survey, the KOF "
                "family, as the OECD compiles it.",
            ),
            (
                "CAD",
                "CAN",
                "Q",
                Frequency.QUARTERLY,
                date(2026, 4, 1),
                "Quarterly: the Bank of Canada Business Outlook Survey "
                "family, republished by the OECD.",
            ),
            (
                "AUD",
                "AUS",
                "Q",
                Frequency.QUARTERLY,
                date(2026, 4, 1),
                "Quarterly: the NAB business survey family. AUD is the "
                "currency this indicator most changes, being the one holding "
                "the least GROWTH sub-weight without it.",
            ),
            (
                "NZD",
                "NZL",
                "Q",
                Frequency.QUARTERLY,
                date(2026, 4, 1),
                "Quarterly: the ANZ business outlook family.",
            ),
        )
    },
)
"""The free leading growth series, verified 8/8 live on ``VERIFIED_ON``.

Registered and consumed by nothing. See `UNCONSUMED_INDICATORS`.
"""


INDICATORS: Mapping[str, IndicatorSpec] = {
    spec.key: spec
    for spec in (
        POLICY_RATE,
        YIELD_2Y,
        YIELD_2Y_CHG_1M,
        YIELD_2Y_CHG_3M,
        YIELD_10Y,
        CPI_YOY,
        CORE_CPI_YOY,
        GDP_YOY,
        UNEMPLOYMENT_RATE,
        EMPLOYMENT_CHG,
        EMPLOYMENT_LEVEL,
        RETAIL_SALES_YOY,
        INDPRO_YOY,
        PMI_COMPOSITE,
        BUSINESS_CONFIDENCE_MFG,
        TRADE_BALANCE,
        CURRENT_ACCOUNT_GDP,
        GDP_NOMINAL_USD,
        COT_NET_PCT_OI,
        EQUITY_INDEX,
        WORLD_EQUITY_INDEX,
        VOL_INDEX,
        COMMODITY_PRICE,
    )
}
"""The registry. Keyed by canonical indicator key; this is what `Pillar.requires`
entries name and what `Observation.indicator` carries."""


UNCONSUMED_INDICATORS: frozenset[str] = frozenset(
    {"yield_10y", "pmi_composite", "equity_index"}
)
"""Registered indicators that name a pillar but that no pillar asks for.

An `IndicatorSpec` carries a ``pillar`` field, and the natural reading of that
field is that the pillar consumes the series. For everything in this set the
reading is wrong: the attribution records which pillar the series would belong
to if it were ever used, and nothing reads it today.

The set exists so that a reader checking a coverage figure can tell a live input
from a series held ready, and so that a second orphan cannot appear silently.
``tests/test_registry_pillar_agreement.py`` asserts both directions: nothing
attributed to a pillar is missing from both that pillar's ``requires`` and this
set, and nothing in this set is in fact consumed. Adding a key here is a
deliberate statement, not a way to quiet the test.

``yield_10y`` is here rather than unregistered because its coverage is genuinely
verified 8/8 and losing that would mean re-verifying eight sources if a scoring
decision ever wants it. It is specifically not a fallback for ``yield_2y``; see
its own description and issue #26.

``equity_index`` is here because the risk pillar reads a global regime and this
key holds eight per-currency benchmarks, which is a different quantity.
``world_equity_index`` carries the reading the pillar actually consumes. The
eight stay registered rather than being deleted: their coverage is verified and
a later per-currency equity component, a local growth or earnings read, is the
obvious consumer for them. Marking them unconsumed is the reversible choice and
keeps their coverage figure from reading as a live input while it is not one.
See the ruling on issue #159.

``pmi_composite`` is here for a different reason, and the difference matters. It
is not held in reserve for want of a decision; it is retired from GROWTH by one.
Issue #23 ruled that GROWTH's single leading-survey slot takes
``business_confidence_mfg`` at 0.30 instead, on the ground that the slot's stated
role in section 3.3 of ``docs/scoring-spec.md`` is "one leading survey" and a
licensed series at 0/8 free coverage fills it on almost no run. The entry stays
registered rather than being deleted so that re-adopting it, if it is ever
licensed, is a one-line change, and so that its coverage figure stops reading as
a live input while it is not one. See ADR 0005.
"""


def publication_lag(ref: SeriesRef | None, frequency: Frequency) -> int:
    """Return the days from a period's start to its publication for one leg.

    Args:
        ref: The leg's registry entry, or ``None`` when the registry carries
            no entry for the indicator and currency, which is the case for an
            observation typed into the manual source under a key the registry
            has not routed.
        frequency: The frequency stamped on the observation being judged. The
            source copies it from `SeriesRef.frequency`, so it is the leg's
            own cadence rather than the parent `IndicatorSpec`'s, which
            differs from it on 36 legs.

    Returns:
        ``ref.publication_lag_days`` where the data engineer has measured one,
        otherwise `DEFAULT_PUBLICATION_LAG_DAYS` for ``frequency``. Days.

    Raises:
        ValueError: If the override is zero or negative. A lag of zero admits
            a figure on the first day of the span it describes, which is the
            look-ahead this number exists to prevent, so it is a registry
            error rather than a fast source.

    """
    if ref is not None and ref.publication_lag_days is not None:
        if ref.publication_lag_days <= 0:
            raise ValueError(
                f"{ref.source} {ref.series_id} has publication_lag_days "
                f"{ref.publication_lag_days}; a lag must be at least one day"
            )
        return ref.publication_lag_days
    return DEFAULT_PUBLICATION_LAG_DAYS[frequency]


def full_weight_age(ref: SeriesRef | None, frequency: Frequency) -> int:
    """Return the oldest a punctual newest print of this leg gets, in days.

    ``lag + cycle``: the publication lag, plus the longest gap to the next
    period. On the day before its successor is admitted, a punctual print is
    exactly this old, so this is the last age at which nothing is late.

    Args:
        ref: The leg's registry entry, or ``None`` for a key or currency the
            registry does not carry.
        frequency: The observation's own frequency, which the source copies
            from `SeriesRef.frequency`. Never `IndicatorSpec.frequency`: 36
            legs differ from their spec's, and a rule keyed on the spec calls
            a punctual quarterly print 44 days late (ADR 0014).

    Returns:
        Days. The point below which `fbe.scoring.freshness` returns 1.0.

    """
    return publication_lag(ref, frequency) + CYCLE_DAYS[frequency]


def staleness_allowance(ref: SeriesRef | None, frequency: Frequency) -> int:
    """Return how old this leg may be and still carry any weight, in days.

    ``lag + 2 * cycle``: one full release cycle past the point where the next
    print was due. A series that has missed a whole cycle is not late, it has
    stopped, and it stops counting.

    Args:
        ref: The leg's registry entry, or ``None`` when the registry has none.
        frequency: The observation's own frequency, as for `full_weight_age`.

    Returns:
        Days. `fbe.scoring.freshness` returns 0.0 at and above this age, and
        `SeriesRef.stale_on` gives up one day earlier, so the registry and the
        scorer cannot give two answers about one series.

    Replaced ``IndicatorSpec.max_staleness_days``, which was hand-keyed per
    indicator and therefore per universe rather than per leg. Nine indicators
    carried an allowance below the oldest age their own punctual print
    reaches, so they hit zero weight before their next print was due. See
    issue #126 and ADR 0014.

    """
    return publication_lag(ref, frequency) + 2 * CYCLE_DAYS[frequency]


def series_for(indicator: str, currency: str) -> SeriesRef | None:
    """Look up the source reference for one indicator and one currency.

    Args:
        indicator: Canonical indicator key, e.g. ``"yield_2y"``.
        currency: ISO 4217 code, or ``"GLOBAL"`` for cross-market series.

    Returns:
        The `SeriesRef`, or ``None`` when the registry has no source for this
        pair. ``None`` and a manual ref mean different things: the first is
        silence, the second is a known gap with a named fallback.

    Raises:
        KeyError: If ``indicator`` is not a registered indicator key. An
            unknown indicator is a programming error, not missing data, so it
            fails loudly rather than returning ``None``.

    """
    spec = INDICATORS[indicator]
    return spec.series.get(currency.upper())


def indicators_for_pillar(pillar: PillarName) -> tuple[str, ...]:
    """Return the indicator keys one pillar consumes, in registry order.

    Args:
        pillar: The pillar to look up.

    Returns:
        Indicator keys, which a `Pillar` implementation can use directly as its
        ``requires`` sequence.

    """
    return tuple(key for key, spec in INDICATORS.items() if spec.pillar is pillar)


def coverage_report(asof: date | None = None) -> Mapping[str, float]:
    """Report the fraction of the G10 each indicator covers with usable data.

    A currency counts only when its ref is verified, is not manual, and its
    ``last_observed`` falls inside the indicator's ``max_staleness_days``. All
    three conditions matter, and the third is the one that was missing before:
    an identifier that resolves but stopped publishing in 2024 is not coverage,
    it is a number that will pass every type check and score a currency wrongly.

    An indicator holding a ``GLOBAL`` ref covers the whole universe by
    construction, since one VIX print serves all eight currencies.

    Args:
        asof: Date to age the registry against. Defaults to `VERIFIED_ON`,
            which is the honest default: the ``last_observed`` dates were
            recorded then, so ageing against a much later date measures how
            long since this file was checked as much as how stale the data is.
            Pass a real run date to see the position on that day, and re-verify
            the registry when the two drift far apart.

    Returns:
        Indicator key to fraction in ``0.0..1.0``.

    """
    when = asof or VERIFIED_ON
    report: dict[str, float] = {}
    for key, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            usable = global_ref.fetchable and not global_ref.stale_on(
                when, staleness_allowance(global_ref, global_ref.frequency)
            )
            report[key] = 1.0 if usable else 0.0
            continue
        covered = sum(
            1
            for code in G10
            if (ref := spec.series.get(code)) is not None
            and ref.fetchable
            and not ref.stale_on(when, staleness_allowance(ref, ref.frequency))
        )
        report[key] = covered / len(G10)
    return report


def identifier_coverage() -> Mapping[str, float]:
    """Report how many G10 legs have a fetchable identifier, ignoring freshness.

    Useful for one thing only: telling a wiring problem apart from a data
    problem. If `coverage_report` says 0.0 and this says 1.0, every identifier
    is right and the source has stopped publishing. If both say 0.0, the
    registry has no source at all.

    Do not use this to decide whether a pillar can score. That is
    `coverage_report`'s job, and the difference between the two functions is
    exactly the class of bug this registry is built to avoid.

    Returns:
        Indicator key to fraction in ``0.0..1.0``.

    """
    report: dict[str, float] = {}
    for key, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            report[key] = 1.0 if global_ref.fetchable else 0.0
            continue
        covered = sum(
            1
            for code in G10
            if (ref := spec.series.get(code)) is not None and ref.fetchable
        )
        report[key] = covered / len(G10)
    return report


def stale_refs(asof: date | None = None) -> Mapping[str, tuple[str, ...]]:
    """List the currencies whose ref for each indicator is unusable.

    The complement of `coverage_report`, and the more actionable of the two:
    this is the list an operator works through, and the list a report shows so
    a reader can see which legs of a score were extrapolated.

    Args:
        asof: Date to age against. Defaults to `VERIFIED_ON`.

    Returns:
        Indicator key to the currencies with no usable ref. Indicators with
        full coverage are omitted, so an empty mapping means the registry is
        entirely healthy.

        An indicator holding a ``GLOBAL`` ref reports the whole of ``G10`` when
        that ref is unusable, and is omitted otherwise. There is no partial
        answer to give: one print serves all eight currencies, per
        `coverage_report`, so the ref is either carrying the universe or none of
        it. The currencies are listed rather than ``GLOBAL`` itself because the
        caller is an operator or a report asking which currencies lost a leg,
        and the answer is all of them.

    A ``GLOBAL`` ref was previously skipped here, which made both sentences
    above false: the function was not the complement of `coverage_report`, and
    an empty mapping did not mean a healthy registry. The two indicators
    concerned are each the sole input to a pillar component, so the gap this hid
    was never one leg of one score. It was a component going dark for every
    currency at once, with nothing on the page to say so. See issue #49.

    """
    when = asof or VERIFIED_ON
    out: dict[str, tuple[str, ...]] = {}
    for key, spec in INDICATORS.items():
        global_ref = spec.series.get(GLOBAL)
        if global_ref is not None:
            if not global_ref.fetchable or global_ref.stale_on(
                when, staleness_allowance(global_ref, global_ref.frequency)
            ):
                out[key] = tuple(G10)
            continue
        bad = tuple(
            code
            for code in G10
            if (ref := spec.series.get(code)) is None
            or not ref.fetchable
            or ref.stale_on(when, staleness_allowance(ref, ref.frequency))
        )
        if bad:
            out[key] = bad
    return out
