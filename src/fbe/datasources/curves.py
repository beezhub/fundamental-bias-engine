"""Government yield curves, straight from the central banks and debt offices.

This module exists because of one number: the two-year government bond yield.

Why it earns its own module
---------------------------
The monetary pillar draws most of its sub-weight from the 2-year yield, and
monetary is the heaviest pillar in the composite. A currency without a 2y
therefore fails the pillar's component floor, loses the pillar entirely, and
takes a coverage demotion on top. The model still runs and still prints
numbers, which is worse than failing outright.

FRED carries no 2-year government yield for any non-US G10 issuer. Verified by
search, not assumed. So the front end has to come from the issuers themselves,
and every one of them publishes it free and without a key. The cost is that
there is no single API: six providers, six formats, six failure modes. That is
the trade this module encapsulates.

Each provider covers exactly one currency. None can substitute for another, so
losing one provider is losing a currency's heaviest pillar rather than
degrading a series. Fail loudly here.

The providers
-------------
**Bank of Canada Valet** (CAD). ``https://www.bankofcanada.ca/valet/``, JSON or
CSV, no key. Series ``BD.CDN.2YR.DQ.YLD`` in group ``bond_yields_benchmark``.
Observations endpoint takes ``recent=N`` or ``start_date``/``end_date``. The
JSON response nests each value as ``{"d": date, "<series>": {"v": value}}``,
which is the one shape here that is not a flat row. Verified live, current.

**ECB Data Portal** (EUR). ``https://data-api.ecb.europa.eu/service/data/``,
SDMX, no key. Key ``YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y``, the euro-area AAA
government spot curve at the 2-year point, Svensson fit. Add
``?format=csvdata`` for flat CSV and ``lastNObservations=N`` or
``startPeriod``. Note what this is: AAA issuers only, so it is close to the
Bund and carries no periphery spread. That is the right choice for a currency
bias, since the euro trades on the core, but it is not a GDP-weighted euro-area
yield. Verified live, current.

**Japan Ministry of Finance** (JPY). Two CSV files, both needed:
``.../interest_rate/jgbcme.csv`` holds the current month only, and
``.../interest_rate/historical/jgbcme_all.csv`` holds 1974 onward. Columns are
``Date,1Y,2Y,...,40Y``; take ``2Y``. Dates are ``YYYY/M/D`` with no
zero-padding. The files are Shift-JIS, not UTF-8, and contain a trailing
Japanese-language footer row that is not data. Missing tenors appear as ``-``.
Verified live, current.

**Bank of England** (GBP). Two different things, do not confuse them:

* The interactive database at ``/boeapps/iadb/fromshowcolumns.asp`` serves flat
  CSV for named series and is where `POLICY_RATE_CODES` comes from. It carries
  gilt yields only at 5, 10 and 20 years. There is no 2-year code; probed and
  confirmed absent. The endpoint returns a redirect, so a client must follow
  it, and answers an unknown code with an HTML page rather than an error.
* The yield curve archive is a ZIP of spreadsheets. The 2-year point lives in
  ``GLC Nominal daily data current month.xlsx``, sheet ``3. spot, short end``,
  in the column whose header row gives a maturity of 2.0 years. Column A is an
  Excel serial date counted from 1899-12-30. Do not hard-code the column
  letter: the header row is the contract, and the maturity grid has been
  changed before.

  This is the only source in the whole registry that needs a spreadsheet
  reader. ``openpyxl`` is not currently a project dependency, so wiring this up
  means adding one; see ``docs/data-sources.md``. Verified live by reading the
  2-year column out of the archive directly.

**Reserve Bank of Australia** (AUD). ``https://www.rba.gov.au/statistics/``
``tables/csv/f2-data.csv``, table F2, no key. Series ``FCMYGBAG2D``, the
2-year Australian Government bond, interpolated. The CSV carries about ten
metadata rows before the data, with the series IDs on the row labelled
``Series ID``, so a parser must find that row rather than assume a header
offset. Dates are ``DD-Mon-YYYY``. Verified live, current.

**Swiss National Bank** (CHF). ``https://data.snb.ch/api/cube/{cube}/data/csv/``
``en``, no key. Cube ``rendoblid`` is the daily Confederation spot curve, with
maturity in dimension ``D0`` where ``2J`` is the 2-year point. Semicolon
delimited, with two metadata lines before the header.

  Verified as an endpoint and **frozen as data**. The cube's own
  ``PublishingDate`` is 2025-09-01 and its last observation is 2025-07-31,
  while other SNB cubes on the same portal are current to 2026-09-01. This is
  not an outage on our side and not a URL error. The Swiss franc has no free
  current 2-year yield, and the registry routes it to the manual source.

**Reserve Bank of New Zealand** (NZD). Not wired in. ``rbnz.govt.nz``,
``nzdmo.govt.nz`` and ``debtmanagement.treasury.govt.nz`` all answer automated
requests with HTTP 403, with and without a browser user agent. The RBNZ does
publish 2-year government bond yields in statistical table B2, so the data
exists; it could not be retrieved. That may be an environment block rather than
a policy one, so it is worth one attempt from another network before accepting
the manual route permanently.

Terms
-----
All of these are public-sector statistical publications, free to use, and each
carries its own terms page. The Bank of Canada response embeds a ``terms`` link
in every payload; honour it. None of them promises an SLA, none is versioned,
and several serve spreadsheets whose layout has changed before, which is why
every parser here is told to key off labels rather than positions.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

from fbe.config import DataConfig
from fbe.datasources.base import (
    BaseDataSource,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.registry import CURVE_SOURCES, INDICATORS, SeriesRef
from fbe.types import Observation

__all__ = [
    "BOC_BASE_URL",
    "BOE_IADB_URL",
    "BOE_YIELD_CURVE_ZIP",
    "ECB_BASE_URL",
    "MOF_JP_CURRENT_URL",
    "MOF_JP_HISTORY_URL",
    "POLICY_RATE_CODES",
    "PROVIDER_FOR_CURRENCY",
    "RATE_LIMIT",
    "RBA_F2_URL",
    "SNB_CUBE_URL",
    "TWO_YEAR_REFS",
    "CurvesSource",
]


BOC_BASE_URL = "https://www.bankofcanada.ca/valet/"
"""Verified live. ``observations/{series}/json`` and ``.../csv``; also
``groups/bond_yields_benchmark/json`` to list the whole benchmark curve."""

ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data/"
"""Verified live. Append the SDMX key, then ``?format=csvdata``."""

MOF_JP_CURRENT_URL = (
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
)
"""Current month only. Verified live."""

MOF_JP_HISTORY_URL = (
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    "historical/jgbcme_all.csv"
)
"""1974 to the end of last month. Verified live. Both files are needed: neither
covers the full range on its own, and the current-month file is the only place
today's number appears."""

BOE_IADB_URL = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
"""Verified live. Query: ``csv.x=yes``, ``Datefrom``/``Dateto`` as
``DD/Mon/YYYY``, ``SeriesCodes`` comma separated, ``CSVF=TN``, ``UsingCodes=Y``,
``VPD=Y``, ``VFD=N``. Follows a redirect, so the client must allow one."""

BOE_YIELD_CURVE_ZIP = (
    "https://www.bankofengland.co.uk/-/media/boe/files/statistics/"
    "yield-curves/latest-yield-curve-data.zip"
)
"""Verified live. Members: ``GLC Nominal daily data current month.xlsx``,
``GLC Real daily data current month.xlsx``, ``GLC Inflation daily data current
month.xlsx``, ``OIS daily data current month.xlsx``. The nominal one holds the
2-year gilt spot rate."""

BOE_GLC_SHEET = "3. spot, short end"
BOE_GLC_MEMBER = "GLC Nominal daily data current month.xlsx"
BOE_GLC_DATE_EPOCH = date(1899, 12, 30)
"""Column A of the sheet is days since this date, the Excel serial convention."""

RBA_F2_URL = "https://www.rba.gov.au/statistics/tables/csv/f2-data.csv"
"""Verified live. Capital market yields, government bonds."""

RBA_SERIES_ID_ROW_LABEL = "Series ID"
"""The row that carries the machine-readable series IDs. Find it by label; the
number of metadata rows above it is not stable."""

SNB_CUBE_URL = "https://data.snb.ch/api/cube/{cube}/data/csv/en"
"""Verified live as an endpoint. See the module docstring on why the bond
cubes cannot currently be used."""

SNB_BOND_CUBE = "rendoblid"
SNB_TENOR_2Y = "2J"

POLICY_RATE_CODES: Mapping[str, str] = {
    "GBP": "IUDBEDR",
}
"""Bank Rate itself, from the Bank of England database, verified live and
current. This replaces the SONIA proxy the registry used before: SONIA tracks
Bank Rate closely but is not it, and there is no reason to use a proxy when the
real series is one request away.

Only GBP appears here. The other seven policy rates come from FRED (USD, EUR)
or the OECD API (the rest), which is fewer moving parts."""

TWO_YEAR_REFS: Mapping[str, tuple[str, str]] = {
    "EUR": ("ecb", "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"),
    "GBP": ("boe", "GLC_NOMINAL_SPOT_SHORT/2.0"),
    "JPY": ("mof_jp", "2Y"),
    "CAD": ("boc", "BD.CDN.2YR.DQ.YLD"),
    "AUD": ("rba", "FCMYGBAG2D"),
}
"""The five non-US 2-year yields this module can actually fetch, each verified
live against its provider. USD comes from FRED's ``DGS2``. CHF and NZD are
absent for the reasons in the module docstring, and the registry routes both to
the manual source rather than substituting something that is nearly right."""

PROVIDER_FOR_CURRENCY: Mapping[str, str] = {
    "EUR": "ecb",
    "GBP": "boe",
    "JPY": "mof_jp",
    "CHF": "snb",
    "CAD": "boc",
    "AUD": "rba",
}
"""Which provider owns which currency. CHF is listed because the provider is
correct even though its data is frozen; that distinction is what lets a health
check tell "the SNB stopped publishing" apart from "we lost the URL"."""

ECB_TIME_COLUMN = "TIME_PERIOD"
ECB_VALUE_COLUMN = "OBS_VALUE"
"""The two columns read out of the ECB csvdata body. Found by name: the capture
carries forty columns and their order is the ECB's to change."""

RBA_DATE_FORMAT = "%d-%b-%Y"
"""Column A of table F2, for example ``04-Sep-2026``."""

PROVIDER_FETCHERS: Mapping[str, str] = {
    "ecb": "fetch_ecb",
    "boc": "fetch_boc",
    "rba": "fetch_rba",
    "mof_jp": "fetch_jgb",
    "boe": "fetch_boe_curve",
    "snb": "fetch_snb",
}
"""Which method serves which provider key. A table rather than a chain of
conditionals so that adding a provider is adding a row, and so that a ref
naming a provider with no fetcher fails here by name instead of silently
falling through to no observations."""

SERVED_TRANSFORMS: frozenset[str] = frozenset({"level"})
"""Transforms this source can emit today.

``chg_1m`` and ``chg_3m`` reuse the same refs and are deliberately absent. The
registry defines them as a resample, a difference and a rescale into basis
points, and two things about that are unsettled: the differencing convention is
ambiguous as written, and the emitted value would be in basis points while the
ref it is built from says ``percent``. `fbe.datasources.fred` already refuses
them for the same reason. Raised on issue #56 and not yet answered, and
``yield_2y_chg_3m`` is documented as the heaviest sub-indicator in the model,
so guessing is the one thing not to do."""

RATE_LIMIT = RateLimit(requests=20, per_seconds=60.0, min_interval_seconds=1.0)
"""These are small public-sector servers, not commercial APIs. A daily run
touches six URLs. There is nothing to gain by going faster and a real
possibility of being blocked, which for a single-currency provider means losing
that currency's monetary pillar outright."""


class CurvesSource(BaseDataSource):
    """Fetches government bond yields from central banks and debt offices.

    Backs the front end of MONETARY, which is the heaviest weight in the model.
    Dispatches per currency to whichever provider publishes that curve, since
    no two of them share a format.

    Attributes:
        name: ``"curves"``. Note that individual `SeriesRef` entries carry the
            specific provider key (``"boc"``, ``"ecb"``, ``"mof_jp"``,
            ``"boe"``, ``"rba"``, ``"snb"``) rather than this name, so an
            `Observation` records which institution published it. A report that
            said only "curves" would lose the one fact a reader wants when a
            number looks wrong.

    """

    name = "curves"
    base_url = ""
    """Empty on purpose. This source reads from six institutions, so there is no
    one root to hang a path off; each provider method passes its whole URL as
    the path instead. That also means `fbe doctor` has no single base URL to
    probe for this source, which is the honest answer for a fan-out."""

    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=2.0)

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed; every
                provider here is open.

        """
        super().__init__(config)

    def _decode(self, body: bytes) -> object:
        """Check the body is one of the three shapes this module parses.

        Overridden because these providers serve JSON and two different CSV
        layouts, so there is no single parse to do here. What this can do, and
        the reason it is here rather than in each provider method, is refuse a
        body no parser could read **before** `_request` writes the cache. A
        maintenance page served at 200 and cached would come back for the whole
        TTL, and an offline run expires nothing, so the entry would outlive the
        outage that caused it.

        Args:
            body: Raw response bytes, exactly as cached.

        Returns:
            The same bytes, for the calling provider method to parse. Nothing
            is interpreted here: which shape is expected depends on which
            provider was asked, and this method does not know.

        Raises:
            SourceError: When the body matches none of the three known shapes.

        """
        text = body.decode("utf-8-sig", errors="replace")
        if text.lstrip().startswith("{"):
            return body
        lines = text.splitlines()
        header = lines[0] if lines else ""
        if ECB_TIME_COLUMN in header and ECB_VALUE_COLUMN in header:
            return body
        if any(line.startswith(RBA_SERIES_ID_ROW_LABEL) for line in lines):
            return body
        raise SourceError(
            f"{self.name} received a body that is neither Valet JSON, an ECB "
            f"csvdata table, nor an RBA table carrying a "
            f"{RBA_SERIES_ID_ROW_LABEL!r} row"
        )

    def _body(self, path: str, params: Mapping[str, str | int | float]) -> str:
        """Fetch one URL through the shared request path and return its text.

        Args:
            path: The whole URL. ``base_url`` is empty on this source, so the
                path carries the host as well.
            params: Query parameters.

        Returns:
            The body decoded as UTF-8, with a byte order mark stripped. The RBA
            serves one and `csv` would otherwise read it into the first cell,
            which is the cell the ``Series ID`` row is found by.

        Raises:
            SourceError: From `_request`, or when the decoded body is not bytes.

        """
        body = self._request(path, params)
        if not isinstance(body, bytes):
            raise SourceError(f"{self.name} decoded {path} into something unusable")
        return body.decode("utf-8-sig", errors="replace")

    def _reading(self, raw: str, provider: str, period: str) -> float | None:
        """Read one published yield, or report it absent.

        Args:
            raw: The cell or field as published.
            provider: Provider key, named in any error.
            period: The session the value belongs to, named in any error.

        Returns:
            The yield in percent per annum, or ``None`` when the provider
            published nothing for that session. A blank cell is a session the
            series does not cover, which is absence rather than a zero yield.

        Raises:
            SourceError: When a value is present and unreadable, or is not
                finite. ``float`` accepts ``"nan"`` and ``"inf"``: a nan
                reaching a cross-sectional pillar makes the mean and the
                standard deviation nan for all eight currencies at once.

        """
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as error:
            raise SourceError(
                f"{provider} published an unreadable value for {period}: {text!r}"
            ) from error
        if not math.isfinite(value):
            raise SourceError(
                f"{provider} published a non-finite value for {period}: {text!r}"
            )
        return value

    def _session(self, raw: str, provider: str, pattern: str) -> date:
        """Read one session date, or raise rather than let a ValueError escape.

        Args:
            raw: The date cell as published.
            provider: Provider key, named in any error.
            pattern: A `datetime.strptime` pattern, or ``"iso"`` for
                ``YYYY-MM-DD``.

        Returns:
            The session the reading belongs to.

        Raises:
            SourceError: When the cell does not match. Deliberately not the
                bare ``ValueError`` the parser would otherwise emit: the
                collector records a `SourceError` as a named coverage gap and
                keeps going, while an unexpected exception type aborts the
                whole refresh over one malformed row.

        """
        text = raw.strip()
        try:
            if pattern == "iso":
                return date.fromisoformat(text)
            return datetime.strptime(text, pattern).date()
        except ValueError as error:
            raise SourceError(
                f"{provider} published an unreadable session date: {text!r}"
            ) from error

    def available(self) -> bool:
        """Report whether at least one provider is reachable.

        Deliberately not all-or-nothing. Each provider covers one currency, so
        the ECB being down costs the euro's front end and nothing else, and
        refusing to run the whole engine over that would be the wrong trade.

        Returns:
            True when any provider answers, or when a warm cache exists.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.available is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch curve points for the requested currencies.

        Args:
            indicators: Canonical indicator keys. Serves ``yield_2y`` and, for
                GBP only, ``policy_rate``.
            currencies: ISO 4217 codes. CHF and NZD yield nothing here by
                design; the registry routes them to the manual source.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            Observations whose ``source`` is the specific provider key.

        Raises:
            SourceError: When a provider that should have data returns none.
                Silence from a single-currency provider is a lost pillar, not a
                lost series, so it must not be swallowed as an empty result.

        """
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        emitted: list[Observation] = []
        for (indicator, currency), ref in self.refs().items():
            if indicator not in wanted_indicators:
                continue
            if currency not in wanted_currencies:
                continue
            if ref.transform not in SERVED_TRANSFORMS:
                raise SourceError(
                    f"{self.name} cannot emit the {ref.transform!r} transform "
                    f"that {indicator} / {currency} asks for: its derivation "
                    "and the unit it would carry are both unsettled, so an "
                    "approximation here would be a wrong number wearing the "
                    "right label"
                )
            for period, value in self._from_provider(ref, currency, start, end):
                emitted.append(
                    self._observation(indicator, currency, ref, period, value)
                )
        return emitted

    def _from_provider(
        self, ref: SeriesRef, currency: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Dispatch one ref to the provider that publishes it.

        Args:
            ref: The registry entry, whose ``source`` names the provider.
            currency: ISO 4217 code, named in any error.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            ``(session, yield)`` pairs in percent per annum, oldest first.

        Raises:
            SourceError: When the provider fails, naming the provider and the
                currency. No other provider is tried: each publishes only its
                own country, so a fallback would be a different country's
                curve presented as this one's.
            NotImplementedError: For a provider still scaffolded. `fetch_jgb`,
                `fetch_boe_curve` and `fetch_snb` are issue #59's.

        """
        fetcher = getattr(self, PROVIDER_FETCHERS[ref.source])
        try:
            return fetcher(ref.series_id, start, end)
        except SourceError as error:
            raise SourceError(
                f"{ref.source} could not supply {currency}: {error}"
            ) from error

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is one of the curve providers.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source in CURVE_SOURCES
        }

    def fetch_boc(
        self, series_id: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one Bank of Canada Valet series.

        Args:
            series_id: Valet series name, e.g. ``"BD.CDN.2YR.DQ.YLD"``.
            start: Passed as ``start_date``.
            end: Passed as ``end_date``.

        Returns:
            ``(date, yield)`` pairs, oldest first.

        Raises:
            SourceError: On repeated request failure or an unreadable body.

        """
        text = self._body(
            f"{BOC_BASE_URL}observations/{series_id}/json",
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise SourceError(
                f"boc returned a body that is not JSON: {error}"
            ) from error
        rows = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SourceError(f"boc returned no observations list for {series_id}")
        parsed: list[tuple[date, float]] = []
        for row in rows:
            if not isinstance(row, dict) or "d" not in row:
                raise SourceError(f"boc returned a row with no date for {series_id}")
            if series_id not in row:
                raise SourceError(
                    f"boc returned a row carrying no {series_id} field; the "
                    "value hangs off a key named after the series, so this is "
                    "a different series or a changed shape"
                )
            cell = row[series_id]
            raw = cell.get("v", "") if isinstance(cell, dict) else ""
            session = self._session(str(row["d"]), "boc", "iso")
            value = self._reading(str(raw), "boc", str(row["d"]))
            if value is not None:
                parsed.append((session, value))
        parsed.sort()
        return parsed

    def fetch_ecb(
        self, key: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one ECB Data Portal series.

        Args:
            key: SDMX key including the dataflow, e.g.
                ``"YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"``.
            start: Passed as ``startPeriod``.
            end: Passed as ``endPeriod``.

        Returns:
            ``(date, yield)`` pairs, oldest first.

        Raises:
            SourceError: On repeated request failure or an unreadable body.

        """
        text = self._body(
            f"{ECB_BASE_URL}{key}",
            {
                "format": "csvdata",
                "startPeriod": start.isoformat(),
                "endPeriod": end.isoformat(),
            },
        )
        reader = csv.DictReader(io.StringIO(text))
        columns = reader.fieldnames or []
        missing = [
            name for name in (ECB_TIME_COLUMN, ECB_VALUE_COLUMN) if name not in columns
        ]
        if missing:
            raise SourceError(
                f"ecb returned a table with no {', '.join(missing)} column for {key}"
            )
        parsed: list[tuple[date, float]] = []
        for row in reader:
            period = (row.get(ECB_TIME_COLUMN) or "").strip()
            if not period:
                raise SourceError(f"ecb returned a row with no period for {key}")
            value = self._reading(row.get(ECB_VALUE_COLUMN) or "", "ecb", period)
            if value is not None:
                parsed.append((self._session(period, "ecb", "iso"), value))
        parsed.sort()
        return parsed

    def fetch_jgb(
        self, tenor: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one JGB tenor from the Ministry of Finance CSVs.

        Reads the history file and the current-month file and merges them, with
        the current-month file winning on overlap. Both are required: the
        history file stops at the end of last month.

        Args:
            tenor: Column header, e.g. ``"2Y"``.
            start: Earliest date wanted.
            end: Latest date wanted.

        Returns:
            ``(date, yield)`` pairs, oldest first, with ``-`` rows dropped.

        Raises:
            SourceError: On repeated request failure, or if the files cannot be
                decoded as Shift-JIS.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.fetch_jgb is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch_boe_iadb(
        self, codes: Sequence[str], start: date, end: date
    ) -> Mapping[str, Sequence[tuple[date, float]]]:
        """Fetch named series from the Bank of England interactive database.

        Args:
            codes: IADB series codes, e.g. ``["IUDBEDR"]``.
            start: Passed as ``Datefrom`` in ``DD/Mon/YYYY`` form.
            end: Passed as ``Dateto``.

        Returns:
            Per-code ``(date, value)`` pairs.

        Raises:
            SourceError: If the body is HTML rather than CSV, which is how this
                endpoint answers an unknown code. A status check will not catch
                it, so the parser must reject anything whose first line is not
                a ``DATE,`` header.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.fetch_boe_iadb is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch_boe_curve(
        self, maturity_years: float, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch a point off the Bank of England nominal spot curve.

        Downloads `BOE_YIELD_CURVE_ZIP`, opens `BOE_GLC_MEMBER`, reads sheet
        `BOE_GLC_SHEET`, and selects the column whose header maturity is
        closest to ``maturity_years``.

        Select the column from the header row every time. The maturity grid has
        been changed before, most recently when the curve was extended to 40
        years, and a hard-coded column letter would then silently return a
        different tenor. That is the failure this whole registry is built to
        avoid, in the one place it would be easiest to introduce.

        Args:
            maturity_years: Tenor wanted, e.g. ``2.0``.
            start: Earliest date wanted.
            end: Latest date wanted.

        Returns:
            ``(date, yield)`` pairs, oldest first.

        Raises:
            SourceError: On request failure, a missing member, a missing sheet,
                or when no header maturity is within tolerance of the request.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.fetch_boe_curve is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def fetch_rba(
        self, series_id: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one series from RBA statistical table F2.

        Args:
            series_id: RBA series ID, e.g. ``"FCMYGBAG2D"``.
            start: Earliest date wanted.
            end: Latest date wanted.

        Returns:
            ``(date, yield)`` pairs, oldest first.

        Raises:
            SourceError: On request failure, or if the row labelled
                `RBA_SERIES_ID_ROW_LABEL` is absent. Locate that row by its
                label rather than counting metadata rows, which vary by table
                and have changed between releases.

        """
        text = self._body(RBA_F2_URL, {})
        rows = list(csv.reader(io.StringIO(text)))
        header = next(
            (row for row in rows if row and row[0].strip() == RBA_SERIES_ID_ROW_LABEL),
            None,
        )
        if header is None:
            raise SourceError(
                f"rba table F2 carries no {RBA_SERIES_ID_ROW_LABEL!r} row, so "
                "there is no way to tell which column holds which series"
            )
        try:
            column = header.index(series_id)
        except ValueError as error:
            raise SourceError(
                f"rba table F2 does not carry {series_id}; it holds "
                f"{', '.join(cell for cell in header[1:] if cell)}"
            ) from error
        parsed: list[tuple[date, float]] = []
        for row in rows[rows.index(header) + 1 :]:
            if not row or not row[0].strip():
                continue
            if len(row) <= column:
                # The published table omits trailing empty cells, so a short
                # row is a session this series does not cover rather than a
                # malformed one.
                continue
            session = self._session(row[0], "rba", RBA_DATE_FORMAT)
            if not start <= session <= end:
                # F2 is served whole whatever window was asked for, so the
                # window is applied here. In a Phase 6 backtest ``end`` is the
                # as-of date of a bar, and a later session is a price the model
                # could not have had.
                continue
            value = self._reading(row[column], "rba", row[0].strip())
            if value is not None:
                parsed.append((session, value))
        parsed.sort()
        return parsed

    def fetch_snb(
        self, cube: str, tenor: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one tenor from an SNB data portal cube.

        Kept implemented-shaped although the registry does not currently use
        it: the endpoint is correct and the data is frozen, and if the SNB
        resumes publishing this becomes a one-line registry change rather than
        a rediscovery.

        Args:
            cube: Cube id, e.g. ``"rendoblid"``.
            tenor: Dimension ``D0`` value, e.g. ``"2J"``.
            start: Earliest date wanted.
            end: Latest date wanted.

        Returns:
            ``(date, yield)`` pairs, oldest first.

        Raises:
            SourceError: On repeated request failure or an unreadable body.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.fetch_snb is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )

    def provider_health(self) -> Mapping[str, date | None]:
        """Report each provider's newest observation.

        The check that separates "we broke it" from "they stopped publishing".
        Run it when a currency's monetary pillar goes missing, before touching
        any parsing code: the Swiss franc's front end disappeared because the
        SNB stopped, and no amount of debugging this module would have found
        that.

        Returns:
            Provider key to its newest observation date, or ``None`` when the
            provider could not be reached at all.

        Raises:
            SourceError: Never. A provider that fails reports ``None``; the
                point of this method is to survive the failure it is
                diagnosing.

        """
        raise NotImplementedError(
            "fbe.datasources.curves.CurvesSource.provider_health is scaffolded; "
            "see docs/roadmap.md Phase 1"
        )
