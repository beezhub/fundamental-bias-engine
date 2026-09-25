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
there is no single API: seven providers, seven formats, seven failure modes.
That is the trade this module encapsulates.

Each provider covers exactly one currency. None can substitute for another, so
losing one provider is losing a currency's heaviest pillar rather than
degrading a series. What fails loudly, and at what scope: a provider that
raises, or answers with no rows, is a raised `SourceError` for that provider,
and the collector records it as a failed source under the provider's own name.
It costs that provider's currencies and nothing else. Until #218 every
provider lived behind one ``curves`` source, so one central bank's website
being down cost every two-year yield in the run and MONETARY printed n/a for
six currencies; ADR 0013 rules that the loud failure belongs one level down.

The shape that follows
----------------------
`CurvesSource` is the shared base. It holds every parser and every fetcher,
because the seven formats are the knowledge worth keeping in one file. Seven
thin subclasses, `EcbSource` through `RbnzSource`, each carry one provider
key as their ``name``, that provider's root as their ``base_url``, and a
`refs()` filtered to that key, and those seven are what `ALL_SOURCES` lists.
The base itself is not in that tuple: with an empty ``base_url`` it can still
fetch from every provider, which `provider_health` and the tests use, but as a
source in a run it would be the fan-out this module no longer is.

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

**Reserve Bank of New Zealand** (NZD). Statistical table B2, daily wholesale
interest rates, as the workbook at `RBNZ_B2_URL`. Sheet ``Data`` carries five
header rows, group, tenor, notes, unit and series ID, then one row per
session with dates as real datetimes and values as floats. Series
``INM.DG102.NZZCF`` is the secondary market government bond closing yield at
2 years, in percent per annum. Locate the column by that ID: the 1, 2, 5 and
10 year columns sit side by side in the same unit, so one column out is a
plausible wrong yield. The 2-year column is blank for most of 2020, which is a
period with no bond near that point rather than a feed fault, and a blank is
absence rather than zero.

  Reachable from a residential or mobile connection only. From every
  data-centre or cloud egress, including every network an unattended run on
  this project can use, the RBNZ answers HTTP 403 with its own JavaScript
  challenge page, static file paths included. That is a vantage block and not
  a publisher policy: the owner retrieved the workbook over a mobile carrier
  on 2026-09-15, and it is the only vantage from which this provider was
  verified. It works where the engine runs live, which is the owner's machine,
  and a health check run from anywhere else reports it as unreachable. Do not
  read that ``None`` as a parsing fault.

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
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from types import MappingProxyType

import openpyxl

from fbe.config import DataConfig
from fbe.datasources.base import (
    BaseDataSource,
    ProbeRequest,
    RateLimit,
    RetryPolicy,
    SourceError,
)
from fbe.datasources.registry import CURVE_SOURCES, INDICATORS, SeriesRef
from fbe.types import Observation

__all__ = [
    "BOC_BASE_URL",
    "BOE_BASE_URL",
    "BOE_GLC_SERIES_PREFIX",
    "BOE_IADB_URL",
    "BOE_YIELD_CURVE_ZIP",
    "ECB_BASE_URL",
    "MOF_JP_BASE_URL",
    "MOF_JP_CURRENT_URL",
    "MOF_JP_HISTORY_URL",
    "POLICY_RATE_CODES",
    "PROVIDER_FOR_CURRENCY",
    "PROVIDER_SOURCES",
    "RATE_LIMIT",
    "RBA_BASE_URL",
    "RBA_F2_URL",
    "RBNZ_B2_URL",
    "RBNZ_BASE_URL",
    "RBNZ_DATA_SHEET",
    "RBNZ_SERIES_ID_ROW_LABEL",
    "RBNZ_UNIT",
    "RBNZ_UNIT_ROW_LABEL",
    "SNB_BASE_URL",
    "SNB_CUBE_URL",
    "TWO_YEAR_REFS",
    "USER_AGENT",
    "BocSource",
    "BoeSource",
    "CurvesSource",
    "EcbSource",
    "MofJpSource",
    "RbaSource",
    "RbnzSource",
    "SnbSource",
]


USER_AGENT = (
    "fundamental-bias-engine/0.1 (+https://github.com/beezhub/fundamental-bias-engine)"
)
"""Sent on every request this source makes. The Bank of England's edge refuses
any request whose ``User-Agent`` names a Python HTTP client, and does so on both
endpoints this source reads, the interactive database and the yield curve
archive, before a body is served. Probed live on 2026-09-24: ``python-httpx``
and ``python-requests`` strings answered HTTP 403, this string, curl's default
and a browser string all answered HTTP 200. Without it the 403 is classed as a
wrong request and not retried, so GBP loses its policy rate and every provider
here loses its two-year yield on every refresh (#273).

The string names this project and its repository rather than a browser, so an
operator on the other end can see who is asking. A browser string would also
pass and is not used: the header is a statement, and this one is true. The
other six providers accept either, so the same header goes to all seven."""

BOC_BASE_URL = "https://www.bankofcanada.ca/valet/"
"""Verified live. ``observations/{series}/json`` and ``.../csv``; also
``groups/bond_yields_benchmark/json`` to list the whole benchmark curve."""

ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data/"
"""Verified live. Append the SDMX key, then ``?format=csvdata``."""

MOF_JP_BASE_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
"""The directory both Ministry of Finance files sit under, and `MofJpSource`'s
``base_url``. Answers a bare GET with the index page, which is enough for
``fbe doctor`` to tell the site is up; the probe asks for the current-month file
so that a body is judged rather than a status."""

MOF_JP_CURRENT_URL = f"{MOF_JP_BASE_URL}jgbcme.csv"
"""Current month only. Verified live."""

MOF_JP_HISTORY_URL = f"{MOF_JP_BASE_URL}historical/jgbcme_all.csv"
"""1974 to the end of last month. Verified live. Both files are needed: neither
covers the full range on its own, and the current-month file is the only place
today's number appears."""

BOE_BASE_URL = "https://www.bankofengland.co.uk/"
"""`BoeSource`'s ``base_url``. The site root rather than either endpoint,
because the two paths this source reads share nothing below the host."""

BOE_IADB_URL = f"{BOE_BASE_URL}boeapps/iadb/fromshowcolumns.asp"
"""Verified live. Query: ``csv.x=yes``, ``Datefrom``/``Dateto`` as
``DD/Mon/YYYY``, ``SeriesCodes`` comma separated, ``CSVF=TN``, ``UsingCodes=Y``,
``VPD=Y``, ``VFD=N``. Follows a redirect, so the client must allow one."""

BOE_YIELD_CURVE_ZIP = (
    f"{BOE_BASE_URL}-/media/boe/files/statistics/"
    "yield-curves/latest-yield-curve-data.zip"
)
"""Verified live. Members: ``GLC Nominal daily data current month.xlsx``,
``GLC Real daily data current month.xlsx``, ``GLC Inflation daily data current
month.xlsx``, ``OIS daily data current month.xlsx``. The nominal one holds the
2-year gilt spot rate."""

BOE_PROBE_WINDOW_DAYS = 14
"""How far back `BoeSource.probe_request` asks the database for Bank Rate.
Two weeks always holds at least one business day, so a healthy database
answers with a row rather than an empty table, and the body is small."""

BOE_GLC_SHEET = "3. spot, short end"
BOE_GLC_MEMBER = "GLC Nominal daily data current month.xlsx"
BOE_GLC_DATE_EPOCH = date(1899, 12, 30)
"""Column A of the sheet is days since this date, the Excel serial convention."""

RBA_BASE_URL = "https://www.rba.gov.au/statistics/tables/csv/"
"""`RbaSource`'s ``base_url``. The directory itself answers a bare GET with
HTTP 403, so the probe names the table rather than the root."""

RBA_F2_URL = f"{RBA_BASE_URL}f2-data.csv"
"""Verified live. Capital market yields, government bonds."""

RBA_SERIES_ID_ROW_LABEL = "Series ID"
"""The row that carries the machine-readable series IDs. Find it by label; the
number of metadata rows above it is not stable."""

RBNZ_BASE_URL = "https://www.rbnz.govt.nz/"
"""`RbnzSource`'s ``base_url``. From a blocked vantage the root answers 403
exactly as the workbook does, so a doctor line here reads the same as a refresh
failure would, which is the point."""

RBNZ_B2_URL = (
    f"{RBNZ_BASE_URL}-/media/project/sites/rbnz/files/statistics/"
    "series/b/b2/hb2-daily-close.xlsx"
)
"""Table B2, daily wholesale interest rates, the daily close workbook from 2018.
Taken from the owner's browser download history on 2026-09-16: the filename is
``hb2-daily-close.xlsx``, and the two paths guessed before it were wrong on the
filename alone. Reachable from a residential or mobile connection only; see the
module docstring."""

RBNZ_DATA_SHEET = "Data"
RBNZ_SERIES_ID_ROW_LABEL = "Series Id"
RBNZ_UNIT_ROW_LABEL = "Unit"
RBNZ_UNIT = "%pa"
"""Percent per annum, which is the registry's ``percent`` with no conversion.
Checked on every read rather than assumed, because the neighbouring columns
carry the same unit and a similar range, so a unit change is the one thing
a plausibility check on the value would not catch."""

SNB_BASE_URL = "https://data.snb.ch/api/cube/"
"""`SnbSource`'s ``base_url``. The cube API root; every cube hangs off it."""

SNB_CUBE_URL = f"{SNB_BASE_URL}{{cube}}/data/csv/en"
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
    "NZD": ("rbnz", "INM.DG102.NZZCF"),
}
"""The six non-US 2-year yields this module can fetch. Five were verified live
against their provider; the RBNZ one was verified from the owner's own
connection, which is the only kind that can reach it. USD comes from FRED's
``DGS2``. CHF is absent for the reason in the module docstring, and the
registry routes it to the manual source rather than substituting something
that is nearly right."""

PROVIDER_FOR_CURRENCY: Mapping[str, str] = {
    "EUR": "ecb",
    "GBP": "boe",
    "JPY": "mof_jp",
    "CHF": "snb",
    "CAD": "boc",
    "AUD": "rba",
    "NZD": "rbnz",
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
    "rbnz": "fetch_rbnz",
}
"""Which method serves which provider key, for the providers whose fetcher
takes the registry ``series_id`` as its first argument, unchanged. A table
rather than a chain of conditionals so that adding such a provider is adding a
row, and so that a ref naming a provider with no fetcher fails by name instead
of silently falling through to no observations.

Two providers are deliberately absent because their fetchers do not take a
series id, and `CurvesSource._dispatch` is the one place that turns their
``series_id`` into arguments:

- ``boe``: ``GLC_NOMINAL_SPOT_SHORT/<maturity_years>`` goes to
  `fetch_boe_curve` with the maturity parsed as a float; any other id is an
  interactive database code and goes to `fetch_boe_iadb`.
- ``snb``: ``<cube>/<tenor>`` goes to `fetch_snb`.

Until #212 both rows were in this table and `_from_provider` handed every
fetcher the raw series id, so the first GBP request raised ``TypeError`` and,
because one provider failing costs the whole source, no two-year yield reached
the cache."""

BOE_GLC_SERIES_PREFIX = "GLC_NOMINAL_SPOT_SHORT"
"""What a Bank of England ``series_id`` starts with when it names a point on
the nominal spot curve rather than a database code. The registry writes the
GBP two-year as ``GLC_NOMINAL_SPOT_SHORT/2.0``, and `fetch_boe_curve` selects
the column by that maturity."""

SERVED_TRANSFORMS: frozenset[str] = frozenset({"level"})
"""Transforms this source can emit today.

``chg_1m`` and ``chg_3m`` reuse the same refs and are deliberately absent. ADR
0004 settles their derivation, a trailing window ending at the latest session,
and no source computes it yet; until one does the emitted value would be in
basis points while the ref it is built from says ``percent``.
`fbe.datasources.fred` passes them over for the same reason. ``yield_2y_chg_3m``
is documented as the heaviest sub-indicator in the model, so guessing is the one
thing not to do."""

JGB_MISSING_TENOR = "-"
"""How the Ministry of Finance publishes a tenor it did not price that day.
Read as a number it becomes a zero JGB yield, which is not implausible enough
for anyone to query."""

JGB_SESSION_PATTERN = re.compile(r"\d{4}/\d{1,2}/\d{1,2}")
"""Shape of a Ministry of Finance session date, used to tell a data row from
the footer notice that shares its column."""

JGB_DATE_FORMAT = "%Y/%m/%d"
"""``2026/9/1``. Month and day are not zero padded, which `strptime` accepts."""

JGB_ENCODING = "shift_jis"
"""Both Ministry of Finance files. The current-month one carries a Japanese
footer row and genuinely fails a UTF-8 decode."""

BOE_IADB_DATE_FORMAT = "%d/%b/%Y"
"""``Datefrom`` and ``Dateto`` in the query, for example ``01/Sep/2026``."""

BOE_IADB_ROW_DATE_FORMAT = "%d %b %Y"
"""Column one of the returned CSV, for example ``01 Sep 2026``. Not the same
shape as the one the query takes."""

BOE_IADB_HEADER_START = "DATE"
"""First field of the CSV header. An unknown series code answers with HTTP 200
and an HTML page, so the status proves nothing and the body must be checked."""

BOE_GLC_YEARS_ROW_LABEL = "years:"
"""Row of the spot sheet carrying each column's maturity in years. Found by
label, because the rows above it are free text and their number is not
promised."""

BOE_GLC_MATURITY_TOLERANCE = 0.01
"""How far a header maturity may sit from the one asked for. The real header
holds ``1.999999920000001`` for the two-year point, so an equality test finds
nothing; a tolerance this tight still cannot reach a neighbouring column, which
is one month away."""

SNB_DELIMITER = ";"
SNB_HEADER_FIRST_FIELD = "Date"
"""The cube opens with two metadata lines and a blank one before this header."""


def _is_jgb_session(cell: str) -> bool:
    """Say whether a Ministry of Finance first column holds a session date.

    Args:
        cell: The cell as published.

    Returns:
        True for ``YYYY/M/D``. False for the blank row and the Japanese footer
        notice that close the current-month file, which are the two rows this
        exists to drop.

    """
    return bool(JGB_SESSION_PATTERN.fullmatch(cell.strip()))


HEALTH_PROBES: Mapping[str, str] = {
    "ecb": TWO_YEAR_REFS["EUR"][1],
    "boc": TWO_YEAR_REFS["CAD"][1],
    "rba": TWO_YEAR_REFS["AUD"][1],
    "mof_jp": TWO_YEAR_REFS["JPY"][1],
    "boe": TWO_YEAR_REFS["GBP"][1],
    "snb": f"{SNB_BOND_CUBE}/{SNB_TENOR_2Y}",
    "rbnz": TWO_YEAR_REFS["NZD"][1],
}
"""One series per provider for `provider_health` to ask about, each in the
form `CurvesSource._dispatch` reads for that provider. The 2-year point in
each case, because that is the series the monetary pillar actually loses when
a provider stops, so a health check on anything else could report a provider
as alive while the number the engine needs is gone."""

HEALTH_PROBE_FROM_YEAR = 1990
"""How far back `provider_health` looks. Wide enough that a provider frozen
years ago still reports the date it froze on rather than an absence, which is
the distinction the method exists to draw."""

RATE_LIMIT = RateLimit(requests=20, per_seconds=60.0, min_interval_seconds=1.0)
"""These are small public-sector servers, not commercial APIs. A daily run
touches seven URLs. There is nothing to gain by going faster and a real
possibility of being blocked, which for a single-currency provider means losing
that currency's monetary pillar outright."""


class CurvesSource(BaseDataSource):
    """Fetches government bond yields from central banks and debt offices.

    Backs the front end of MONETARY, which is the heaviest weight in the model.
    Dispatches per currency to whichever provider publishes that curve, since
    no two of them share a format.

    This is the shared base and the fan-out. It is not in `ALL_SOURCES`: the
    seven subclasses below are, one per provider, and each is what a run
    constructs. Constructed directly, with its empty ``base_url``, it can still
    fetch from every provider, which is what `provider_health` needs and what
    the parser tests use; as a source in a run it would be the whole-source
    failure ADR 0013 rules out.

    Attributes:
        name: ``"curves"`` on the base; each subclass sets its provider key.
            Individual `SeriesRef` entries carry the provider key regardless,
            so an `Observation` records which institution published it. A
            report that said only "curves" would lose the one fact a reader
            wants when a number looks wrong.
        providers: The registry provider keys this source serves. The base
            serves all of `CURVE_SOURCES`; a subclass narrows it to its own
            key, and `refs()` is filtered by it, so the collector asks each
            provider only for the currencies it publishes.

    """

    name = "curves"
    providers: frozenset[str] = CURVE_SOURCES
    follow_redirects = True
    """The Bank of England's interactive database answers its documented URL
    with a 302 on every request, so here the redirect is the endpoint rather
    than a moved one. Opted into per source, which leaves the base's refusal in
    place for every source whose provider does not do this."""

    default_headers = MappingProxyType({"User-Agent": USER_AGENT})
    """One header, and it is the difference between HTTP 200 and HTTP 403 at the
    Bank of England. See `USER_AGENT` for the probe. Set on the class rather
    than built per request so it reaches the one client the base constructs,
    and therefore every provider this source talks to."""

    base_url = ""
    """Empty on the base, which reads from seven institutions and so has no one
    root. Each subclass sets its provider's root, and every fetcher expresses
    its URL through `_path`, which is the whole URL here and the part below the
    root there. That is also what gives `fbe doctor` a base URL per provider
    to probe, which the fan-out never had."""

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
        if body[:2] == b"PK":
            # A ZIP archive, which is how the Bank of England publishes its
            # yield curve and what an RBNZ workbook is underneath. Whether the
            # member and sheet inside are the right ones is the caller's to say.
            return body
        if header.startswith(BOE_IADB_HEADER_START):
            return body
        if lines and lines[0].split(SNB_DELIMITER)[0].strip('"') == "CubeId":
            return body
        try:
            decoded = body.decode(JGB_ENCODING)
        except UnicodeDecodeError:
            decoded = ""
        if decoded and "Interest Rate" in decoded.splitlines()[0]:
            return body
        raise SourceError(
            f"{self.name} received a body it cannot read. This source parses "
            f"Valet JSON, an ECB csvdata table, an RBA table carrying a "
            f"{RBA_SERIES_ID_ROW_LABEL!r} row, a Bank of England CSV whose "
            f"header starts {BOE_IADB_HEADER_START!r}, a ZIP archive or "
            f"workbook, an SNB "
            f"cube, or a Ministry of Finance Shift-JIS table. An HTML page is "
            f"how several of these report an unknown series code, at HTTP 200."
        )

    def _path(self, url: str) -> str:
        """Express a provider URL relative to this source's ``base_url``.

        Every fetcher is written against a whole URL, because the constants
        are what the docs, the tests and an operator's browser share. On the
        base, whose ``base_url`` is empty, the path is the whole URL and the
        request goes out unchanged. On a provider subclass the root is
        stripped, so `BaseDataSource._request` rebuilds the same URL from
        ``base_url`` plus this.

        Args:
            url: The whole URL a fetcher wants.

        Returns:
            What `_request` should be handed so that it asks for ``url``.

        Raises:
            SourceError: When this source names a root and ``url`` is not
                under it. Appending the whole URL to the root would produce a
                request to nowhere that the provider might still answer with
                a 404 page, and a fetcher on the wrong subclass is a defect
                worth naming rather than a slow way to fetch nothing.

        """
        if not self.base_url:
            return url
        if not url.startswith(self.base_url):
            raise SourceError(
                f"{self.name} was asked for {url}, which is not under its root "
                f"{self.base_url}; a fetcher is running on the wrong provider"
            )
        return url.removeprefix(self.base_url)

    def _body(self, path: str, params: Mapping[str, str | int | float]) -> str:
        """Fetch one URL through the shared request path and return its text.

        Args:
            path: What `_path` returned for the URL wanted: the whole URL on
                the base, the part below the root on a provider subclass.
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
        """Report availability, which is always true: no provider takes a key.

        Returns:
            Whether this source can be used on this run. Always ``True``: the
            seven institutions publish openly, so there is nothing about the
            configuration that could rule this source out, and there is no
            directory or credential to check.

        This answers about configuration rather than connectivity, which is the
        contract `BaseDataSource.available` sets and
        ``tests/test_datasource_base.py`` enforces across every source: no
        implementation here may make a request. The stub this replaced promised
        "true when any provider answers", which would have cost up to seven
        requests on every run to learn something `fetch` reports anyway: a
        provider that is down raises there, naming itself and its currency. An
        offline run with nothing cached is reported the same way, by `fetch`
        raising, which is the only place that distinction can be made without
        a network call.

        """
        return True

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
            currencies: ISO 4217 codes. CHF yields nothing here by design;
                the registry routes it to the manual source.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            Observations whose ``source`` is the specific provider key. A ref
            whose ``transform`` is not in `SERVED_TRANSFORMS` contributes
            nothing and no provider is asked for it.

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
                # Passed over rather than raised on. Raising here cost every
                # other curve in the run for a ref this source was never going
                # to serve (issue #169). The pillar represents the absence.
                continue
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
                curve presented as this one's. Also when the provider answers
                and the answer holds no session inside the window: silence
                from a single-currency provider is that currency's heaviest
                pillar gone, and an empty success would let it go quietly.
                ADR 0013's third rule, enforced here so that it holds for
                every provider source at once.

        """
        try:
            readings = self._dispatch(ref.source, ref.series_id, start, end)
        except SourceError as error:
            raise SourceError(
                f"{ref.source} could not supply {currency}: {error}"
            ) from error
        if not readings:
            raise SourceError(
                f"{ref.source} answered but served no session for {currency} "
                f"between {start} and {end}. That is a lost pillar for "
                f"{currency}, not an empty series, so it is reported as a "
                "failure of this provider."
            )
        return readings

    def _dispatch(
        self, provider: str, series_id: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Turn a provider key and a registry series id into a fetcher call.

        The one place that knows how each provider's ``series_id`` maps onto
        its fetcher's arguments. `_from_provider` and `provider_health` both
        come through here, so the registry and the health check cannot read
        the same id two different ways, which is how #212 happened: the
        health check special-cased the two providers below and the registry
        path did not.

        Args:
            provider: Provider key, one of `CURVE_SOURCES`.
            series_id: The registry's identifier for that provider. Rules per
                provider are on `PROVIDER_FETCHERS`.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            ``(session, value)`` pairs in percent per annum, oldest first,
            exactly as the fetcher returned them.

        Raises:
            SourceError: When the series id is not in the form the provider's
                fetcher needs, naming the provider and the id. Raised rather
                than guessed at, because a maturity read off a malformed id
                would select a different tenor and present it as this one.
                Also whatever the fetcher raises.

        """
        if provider == "boe":
            prefix, _, maturity = series_id.partition("/")
            if prefix != BOE_GLC_SERIES_PREFIX:
                return self.fetch_boe_iadb([series_id], start, end)[series_id]
            try:
                maturity_years = float(maturity)
            except ValueError as error:
                raise SourceError(
                    f"boe series id {series_id!r} does not end in a maturity in "
                    f"years after {BOE_GLC_SERIES_PREFIX}/"
                ) from error
            return self.fetch_boe_curve(maturity_years, start, end)
        if provider == "snb":
            cube, separator, tenor = series_id.partition("/")
            if not (cube and separator and tenor):
                raise SourceError(
                    f"snb series id {series_id!r} is not in the form cube/tenor"
                )
            return self.fetch_snb(cube, tenor, start, end)
        fetcher = getattr(self, PROVIDER_FETCHERS[provider])
        return fetcher(series_id, start, end)

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is one of this source's providers.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`. All
            seven providers' entries on the base; one provider's on a
            subclass, which is what makes the collector ask `EcbSource` for
            the euro and nothing else, and what makes one provider's failure
            cost its own currencies and no other's.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source in self.providers
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
            self._path(f"{BOC_BASE_URL}observations/{series_id}/json"),
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
            self._path(f"{ECB_BASE_URL}{key}"),
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
        merged: dict[date, float] = {}
        # History first, current month second: both can carry a day and the
        # current-month file is the fresher publication of it.
        for url in (MOF_JP_HISTORY_URL, MOF_JP_CURRENT_URL):
            merged.update(self._jgb_rows(url, tenor, start, end))
        return sorted(merged.items())

    def _jgb_rows(
        self, url: str, tenor: str, start: date, end: date
    ) -> Mapping[date, float]:
        """Read one Ministry of Finance file.

        Args:
            url: Either the current-month or the history file.
            tenor: Column header, e.g. ``"2Y"``.
            start: Earliest session wanted.
            end: Latest session wanted.

        Returns:
            Session to yield in percent per annum, for rows inside the window
            that priced this tenor.

        Raises:
            SourceError: When the body is not Shift-JIS, when the header names
                no such tenor, or when a value is present and unreadable.

        """
        raw = self._request(self._path(url), {})
        if not isinstance(raw, bytes):
            raise SourceError(f"{self.name} decoded {url} into something unusable")
        try:
            text = raw.decode(JGB_ENCODING)
        except UnicodeDecodeError as error:
            raise SourceError(
                f"mof_jp served a body that is not {JGB_ENCODING}: {error}"
            ) from error
        rows = list(csv.reader(io.StringIO(text)))
        header = next((row for row in rows if row and row[0].strip() == "Date"), None)
        if header is None:
            raise SourceError("mof_jp served a body with no Date header row")
        try:
            column = header.index(tenor)
        except ValueError as error:
            raise SourceError(
                f"mof_jp publishes no {tenor!r} tenor; it holds "
                f"{', '.join(cell for cell in header[1:] if cell)}"
            ) from error
        readings: dict[date, float] = {}
        for row in rows[rows.index(header) + 1 :]:
            if not row or not _is_jgb_session(row[0]):
                # The current-month file ends with a blank row and then a
                # Japanese-language notice about clearing the browser cache.
                # The notice sits in the first column, where a date goes, so an
                # emptiness check does not catch it. Every data row in both
                # files starts with a date, so anything else is not data.
                continue
            if len(row) <= column:
                continue
            cell = row[column].strip()
            if cell == JGB_MISSING_TENOR:
                continue
            session = self._parse_jgb_date(row[0])
            if not start <= session <= end:
                continue
            value = self._reading(cell, "mof_jp", row[0].strip())
            if value is not None:
                readings[session] = value
        return readings

    def _parse_jgb_date(self, raw: str) -> date:
        """Read a Ministry of Finance session date.

        Args:
            raw: ``YYYY/M/D``, with month and day not zero padded.

        Returns:
            The session.

        Raises:
            SourceError: When the cell does not parse, rather than the bare
                ``ValueError`` that would abort a whole refresh.

        """
        return self._session(raw, "mof_jp", JGB_DATE_FORMAT)

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
        raw = self._request(
            self._path(BOE_IADB_URL),
            {
                "csv.x": "yes",
                "Datefrom": start.strftime(BOE_IADB_DATE_FORMAT),
                "Dateto": end.strftime(BOE_IADB_DATE_FORMAT),
                "SeriesCodes": ",".join(codes),
                "CSVF": "TN",
                "UsingCodes": "Y",
                "VPD": "Y",
                "VFD": "N",
            },
        )
        if not isinstance(raw, bytes):
            raise SourceError(f"{self.name} decoded the IADB into something unusable")
        text = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows or not rows[0] or rows[0][0].strip() != BOE_IADB_HEADER_START:
            raise SourceError(
                "boe answered with a body whose first field is not "
                f"{BOE_IADB_HEADER_START!r}, which is how this endpoint reports "
                "an unknown series code: it serves an HTML page at HTTP 200, so "
                "the status cannot be trusted and the body is what is checked"
            )
        header = [cell.strip() for cell in rows[0]]
        collected: dict[str, list[tuple[date, float]]] = {code: [] for code in codes}
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            session = self._session(row[0], "boe", BOE_IADB_ROW_DATE_FORMAT)
            for code in codes:
                if code not in header:
                    continue
                index = header.index(code)
                if len(row) <= index:
                    continue
                value = self._reading(row[index], "boe", row[0].strip())
                if value is not None:
                    collected[code].append((session, value))
        for series in collected.values():
            series.sort()
        return collected

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
        raw = self._request(self._path(BOE_YIELD_CURVE_ZIP), {})
        if not isinstance(raw, bytes):
            raise SourceError(
                f"{self.name} decoded the archive into something unusable"
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as error:
            raise SourceError(
                f"boe served a body that is not an archive: {error}"
            ) from error
        if BOE_GLC_MEMBER not in archive.namelist():
            raise SourceError(
                f"boe archive holds no {BOE_GLC_MEMBER!r}; it holds "
                f"{', '.join(archive.namelist())}"
            )
        workbook = openpyxl.load_workbook(
            io.BytesIO(archive.read(BOE_GLC_MEMBER)), read_only=True, data_only=True
        )
        if BOE_GLC_SHEET not in workbook.sheetnames:
            raise SourceError(
                f"boe workbook holds no {BOE_GLC_SHEET!r} sheet; it holds "
                f"{', '.join(workbook.sheetnames)}"
            )
        rows = list(workbook[BOE_GLC_SHEET].iter_rows(values_only=True))
        maturities = next(
            (
                row
                for row in rows
                if row and str(row[0]).strip() == BOE_GLC_YEARS_ROW_LABEL
            ),
            None,
        )
        if maturities is None:
            raise SourceError(
                f"boe spot sheet carries no {BOE_GLC_YEARS_ROW_LABEL!r} row, so "
                "there is no way to tell which column holds which maturity"
            )
        column = self._nearest_maturity(maturities, maturity_years)
        parsed: list[tuple[date, float]] = []
        for row in rows[rows.index(maturities) + 1 :]:
            if not row or row[0] is None or len(row) <= column:
                continue
            session = self._curve_session(row[0])
            if session is None or not start <= session <= end:
                continue
            value = self._reading(str(row[column]), "boe", session.isoformat())
            if value is not None:
                parsed.append((session, value))
        parsed.sort()
        return parsed

    def _nearest_maturity(self, maturities: Sequence[object], wanted: float) -> int:
        """Find the column whose header maturity is the one asked for.

        Args:
            maturities: The ``years:`` row, as read.
            wanted: Tenor in years.

        Returns:
            Index of the matching column.

        Raises:
            SourceError: When no header maturity sits within
                `BOE_GLC_MATURITY_TOLERANCE`. Returning the nearest whatever
                the distance would answer a two-year request with a forty-year
                point the day the grid is re-cut, which is the failure this
                whole registry exists to avoid.

        """
        candidates = [
            (abs(float(value) - wanted), index)
            for index, value in enumerate(maturities)
            if isinstance(value, (int, float))
        ]
        if not candidates:
            raise SourceError("boe spot sheet carries no numeric maturities")
        distance, index = min(candidates)
        if distance > BOE_GLC_MATURITY_TOLERANCE:
            raise SourceError(
                f"boe spot sheet holds no maturity within "
                f"{BOE_GLC_MATURITY_TOLERANCE} years of {wanted}; the nearest is "
                f"{distance:.4f} away"
            )
        return index

    def _curve_session(self, cell: object) -> date | None:
        """Read a date out of column A of the spot sheet.

        Args:
            cell: The cell as ``openpyxl`` returned it.

        Returns:
            The session, or ``None`` for a cell that is neither a date nor a
            serial. The rows above the data carry free text in this column.

        Raises:
            SourceError: Never. An unreadable cell here is a header row rather
                than a broken reading.

        """
        if isinstance(cell, datetime):
            return cell.date()
        if isinstance(cell, date):
            return cell
        if isinstance(cell, (int, float)) and not isinstance(cell, bool):
            return self._excel_serial_to_date(cell)
        return None

    def _excel_serial_to_date(self, serial: float) -> date:
        """Convert a spreadsheet serial to a date.

        Args:
            serial: Days since `BOE_GLC_DATE_EPOCH`.

        Returns:
            The date it names. ``openpyxl`` converts date-formatted cells for
            us, so this is the path for a workbook written without that
            formatting rather than the usual one.

        """
        return BOE_GLC_DATE_EPOCH + timedelta(days=int(serial))

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
        text = self._body(self._path(RBA_F2_URL), {})
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
        raw = self._request(self._path(SNB_CUBE_URL.format(cube=cube)), {})
        if not isinstance(raw, bytes):
            raise SourceError(f"{self.name} decoded the cube into something unusable")
        text = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=SNB_DELIMITER))
        header = next(
            (row for row in rows if row and row[0].strip() == SNB_HEADER_FIRST_FIELD),
            None,
        )
        if header is None:
            raise SourceError(
                f"snb cube {cube} carries no {SNB_HEADER_FIRST_FIELD!r} header "
                "past its metadata lines"
            )
        parsed: list[tuple[date, float]] = []
        for row in rows[rows.index(header) + 1 :]:
            if len(row) < 3 or row[1].strip() != tenor:
                continue
            session = self._session(row[0], "snb", "iso")
            if not start <= session <= end:
                continue
            value = self._reading(row[2], "snb", row[0].strip())
            if value is not None:
                parsed.append((session, value))
        parsed.sort()
        return parsed

    def fetch_rbnz(
        self, series_id: str, start: date, end: date
    ) -> Sequence[tuple[date, float]]:
        """Fetch one series from RBNZ statistical table B2.

        Downloads `RBNZ_B2_URL`, opens sheet `RBNZ_DATA_SHEET`, finds the row
        labelled `RBNZ_SERIES_ID_ROW_LABEL` and reads the column that carries
        ``series_id`` in it. The workbook holds 48 series; the four government
        bond tenors differ only in the word before "year" and sit side by side
        in the same unit and a similar range, so the column is located by its
        ID and never by position. One column out is a plausible wrong yield.

        The unit row is checked against `RBNZ_UNIT` for that column. The
        registry promises ``percent`` and a value in basis points is a hundred
        times larger while still looking like a number a yield could take.

        Args:
            series_id: RBNZ series ID, e.g. ``"INM.DG102.NZZCF"``.
            start: Earliest session wanted.
            end: Latest session wanted. The file is served whole, so the
                window is applied here; in a backtest ``end`` is the as-of
                date of a bar and a later session is a price the model could
                not have had.

        Returns:
            ``(session, yield)`` pairs in percent per annum, oldest first. A
            session whose cell is blank is omitted rather than read as zero:
            the RBNZ left about a year of 2020 blank in the 2-year column, and
            a zero front end for that year would be a number, not a gap.

        Raises:
            SourceError: On request failure, which from any data-centre or
                cloud egress is HTTP 403 with the RBNZ's own challenge page;
                on a body that is not a workbook; on a missing data sheet or
                series-ID row; on an ID the sheet does not carry; or on a
                column published in a unit other than `RBNZ_UNIT`.

        """
        raw = self._request(self._path(RBNZ_B2_URL), {})
        if not isinstance(raw, bytes):
            raise SourceError(
                f"{self.name} decoded the workbook into something unusable"
            )
        try:
            workbook = openpyxl.load_workbook(
                io.BytesIO(raw), read_only=True, data_only=True
            )
        except (zipfile.BadZipFile, KeyError, ValueError) as error:
            raise SourceError(
                f"rbnz served a body that is not a workbook: {error}"
            ) from error
        if RBNZ_DATA_SHEET not in workbook.sheetnames:
            raise SourceError(
                f"rbnz workbook holds no {RBNZ_DATA_SHEET!r} sheet; it holds "
                f"{', '.join(workbook.sheetnames)}"
            )
        rows = list(workbook[RBNZ_DATA_SHEET].iter_rows(values_only=True))
        id_row = self._rbnz_labelled_row(rows, RBNZ_SERIES_ID_ROW_LABEL)
        ids = rows[id_row]
        try:
            column = ids.index(series_id)
        except ValueError as error:
            raise SourceError(
                f"rbnz table B2 does not carry {series_id}; it holds "
                f"{', '.join(str(cell) for cell in ids[1:] if cell)}"
            ) from error
        units = rows[self._rbnz_labelled_row(rows, RBNZ_UNIT_ROW_LABEL)]
        unit = units[column] if len(units) > column else None
        if unit != RBNZ_UNIT:
            raise SourceError(
                f"rbnz publishes {series_id} in {unit!r}, not {RBNZ_UNIT!r}; "
                "the registry promises percent and no conversion is applied"
            )
        parsed: list[tuple[date, float]] = []
        for row in rows[id_row + 1 :]:
            if not row or len(row) <= column:
                continue
            session = self._curve_session(row[0])
            if session is None or not start <= session <= end:
                continue
            cell = row[column]
            value = self._reading(
                "" if cell is None else str(cell), "rbnz", session.isoformat()
            )
            if value is not None:
                parsed.append((session, value))
        parsed.sort()
        return parsed

    def _rbnz_labelled_row(self, rows: Sequence[Sequence[object]], label: str) -> int:
        """Find the header row of the B2 sheet that starts with ``label``.

        Args:
            rows: The data sheet, as ``openpyxl`` returned it.
            label: The first cell's text, e.g. ``"Series Id"``.

        Returns:
            The index of that row. Found by label rather than by row number:
            the RBNZ publishes five header rows today and has not promised to
            keep it at five.

        Raises:
            SourceError: When no row starts with the label. Without the
                series-ID row there is no way to tell one tenor from another,
                and without the unit row there is no way to know what the
                number is.

        """
        for index, row in enumerate(rows):
            if row and str(row[0]).strip() == label:
                return index
        raise SourceError(
            f"rbnz table B2 carries no {label!r} row, so its columns cannot be "
            "told apart"
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
        newest: dict[str, date | None] = {}
        for provider, series_id in HEALTH_PROBES.items():
            try:
                readings = self._from_provider_series(provider, series_id)
            except (SourceError, NotImplementedError):
                # The point of this method is to survive the failure it is
                # diagnosing, so every provider reports and none propagates.
                newest[provider] = None
                continue
            newest[provider] = max((day for day, _ in readings), default=None)
        return newest

    def _from_provider_series(
        self, provider: str, series_id: str
    ) -> Sequence[tuple[date, float]]:
        """Fetch a provider's probe series over a wide window.

        Args:
            provider: Provider key.
            series_id: The identifier that provider takes.

        Returns:
            ``(session, value)`` pairs.

        Raises:
            SourceError: From the provider method, for `provider_health` to
                turn into a ``None``.

        """
        start = date(HEALTH_PROBE_FROM_YEAR, 1, 1)
        # Today, because the question this answers is how recently a provider
        # published. A fixed end date would report every provider as frozen on
        # it, which is the answer the method exists to distinguish.
        end = date.today()
        return self._dispatch(provider, series_id, start, end)

    def _probe_for(self, url: str, params: Mapping[str, str]) -> ProbeRequest:
        """Describe a doctor probe against one of this provider's real URLs.

        Args:
            url: The whole URL, as the fetchers write it.
            params: Query parameters, all strings, never a credential.

        Returns:
            A `ProbeRequest` whose body check is `_decode`: the same shape
            test every fetched body passes before it is cached. A 200 that
            carries an HTML page, which is how several of these providers
            report a block or an unknown series, is refused by it, so doctor
            cannot print a blocked provider as healthy with a latency.

        """
        return ProbeRequest(
            path=self._path(url), params=dict(params), verify=self._decode
        )


class EcbSource(CurvesSource):
    """The ECB Data Portal, which serves the euro two-year and nothing else here.

    One of the seven provider sources `ALL_SOURCES` lists in place of the
    fan-out; see the module docstring for why the split exists.
    """

    name = "ecb"
    base_url = ECB_BASE_URL
    providers = frozenset({"ecb"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the newest point of the series the monetary pillar reads."""
        return self._probe_for(
            f"{ECB_BASE_URL}{TWO_YEAR_REFS['EUR'][1]}",
            {"format": "csvdata", "lastNObservations": "1"},
        )


class BocSource(CurvesSource):
    """Bank of Canada Valet, which serves the Canadian two-year."""

    name = "boc"
    base_url = BOC_BASE_URL
    providers = frozenset({"boc"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the newest point of the series the monetary pillar reads."""
        return self._probe_for(
            f"{BOC_BASE_URL}observations/{TWO_YEAR_REFS['CAD'][1]}/json",
            {"recent": "1"},
        )


class MofJpSource(CurvesSource):
    """Japan's Ministry of Finance, which serves the JGB two-year."""

    name = "mof_jp"
    base_url = MOF_JP_BASE_URL
    providers = frozenset({"mof_jp"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the current-month file, the only one carrying today's number."""
        return self._probe_for(MOF_JP_CURRENT_URL, {})


class BoeSource(CurvesSource):
    """The Bank of England, the only provider here serving two indicators.

    Bank Rate comes from the interactive database and the gilt two-year from
    the yield curve archive, two paths that share nothing below the host.
    """

    name = "boe"
    base_url = BOE_BASE_URL
    providers = frozenset({"boe"})

    def probe_request(self) -> ProbeRequest:
        """Ask the database for two weeks of Bank Rate.

        The database rather than the archive, because the archive is a
        400-kilobyte ZIP and a probe should be cheap. Bank Rate rather than a
        gilt because the database carries no two-year, and this is the series
        the run actually reads from it.
        """
        end = date.today()
        start = end - timedelta(days=BOE_PROBE_WINDOW_DAYS)
        return self._probe_for(
            BOE_IADB_URL,
            {
                "csv.x": "yes",
                "Datefrom": start.strftime(BOE_IADB_DATE_FORMAT),
                "Dateto": end.strftime(BOE_IADB_DATE_FORMAT),
                "SeriesCodes": POLICY_RATE_CODES["GBP"],
                "CSVF": "TN",
                "UsingCodes": "Y",
                "VPD": "Y",
                "VFD": "N",
            },
        )


class RbaSource(CurvesSource):
    """The Reserve Bank of Australia's table F2, the Australian two-year."""

    name = "rba"
    base_url = RBA_BASE_URL
    providers = frozenset({"rba"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the table itself: the directory above it answers 403."""
        return self._probe_for(RBA_F2_URL, {})


class SnbSource(CurvesSource):
    """The Swiss National Bank's cube API.

    Listed although the registry routes no series to it today: the cube is
    frozen, so CHF's two-year comes from the manual source. The collector
    reports this source as "no series routed to it", which is true, and
    `provider_health` still asks it for the frozen cube so an operator can see
    the date it stopped on.
    """

    name = "snb"
    base_url = SNB_BASE_URL
    providers = frozenset({"snb"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the bond cube, frozen or not: reachable is the question."""
        return self._probe_for(SNB_CUBE_URL.format(cube=SNB_BOND_CUBE), {})


class RbnzSource(CurvesSource):
    """The Reserve Bank of New Zealand's table B2 workbook, the NZ two-year.

    Reachable from a residential or mobile connection only, and from some of
    those only by a browser or curl: the block keys on the client's TLS
    handshake rather than on any header. From a blocked vantage this source
    fails, is reported by name, and costs NZD its two-year and nothing else,
    which is the whole point of it being its own source.
    """

    name = "rbnz"
    base_url = RBNZ_BASE_URL
    providers = frozenset({"rbnz"})

    def probe_request(self) -> ProbeRequest:
        """Ask for the workbook: the root answers 403 from the same vantages."""
        return self._probe_for(RBNZ_B2_URL, {})


PROVIDER_SOURCES: tuple[type[CurvesSource], ...] = (
    EcbSource,
    BocSource,
    MofJpSource,
    BoeSource,
    RbaSource,
    SnbSource,
    RbnzSource,
)
"""The seven provider sources, in the order `ALL_SOURCES` lists them. One per
key in `CURVE_SOURCES`, and a test holds the two sets equal so a provider added
to the registry cannot be forgotten here, where forgetting it means its
currency is never fetched and nothing says so."""
