# Data sources

Where every number in the engine comes from, what it costs, what its licence
allows, how often it changes, and where the holes are.

Read the "Coverage reality" section before anything else. It is the shortest
route to understanding which parts of the model are actually load-bearing.

All identifiers below were checked against the live source by fetching real
data. Anything that could not be checked is marked unverified, here and in
`src/fbe/datasources/registry.py`. Verification date: 2026-09-09.

## The one thing to know about FRED

FRED republishes OECD statistics, and several of those mirrors have stopped
updating while continuing to answer requests perfectly normally.

FRED's entire OECD CPI complex ends in March or April 2025. Its Japanese CPI
ends in June 2021. Industrial production for most of Europe ends in 2023, and
every leg of the current account family ends at 2024Q4. Every one of those
series resolves, returns data, and looks healthy. Nothing in the API says
otherwise.

Two things follow, and both shape this whole document. Wherever the OECD
publishes the same material itself, this project reads it from the OECD's own
API instead, where it is current. And `coverage_report()` is freshness-aware:
a series counts only if its newest observation falls inside the indicator's
staleness allowance, so a frozen mirror shows up as a gap rather than as a
score.
---

## The eight sources

| Source | Backs | Key | Cost | Cadence |
| --- | --- | --- | --- | --- |
| FRED | MONETARY, GROWTH, EMPLOYMENT, EXTERNAL, US and euro-area INFLATION | yes, free | free | daily to quarterly by series |
| OECD SDMX | INFLATION for six currencies, plus rates and equity indices | no | free | monthly, ~1 month behind |
| Eurostat | EMPLOYMENT for the euro area | no | free | quarterly |
| Central banks and debt offices | the 2-year yields at the heart of MONETARY | no | free | daily |
| CFTC COT | POSITIONING | no | free | weekly, Friday 15:30 ET, 3-day lag |
| Stooq | RISK price proxies, daily | no | free | daily |
| Forex Factory | news blackout, no pillar | no | free | weekly, current week only |
| Manual | everything the others cannot supply | no | your time | when you type it |

Only FRED needs a credential. Everything else is open, which also means anyone
reading this can check any claim in it without asking for access first.

**What one failure costs, per source.** Every source has a failure scope, and
the refresh output names the scope's unit when it fails (ADR 0013, ADR 0015).

| Source | Scope | One failure costs |
| --- | --- | --- |
| FRED | series | that one `(indicator, currency)`, named on its own line; the other series are served and the source reads `partial`. A ref FRED cannot serve, today the two-year change transforms, is declined before it is routed and appears only as a registry gap (ADR 0016) |
| OECD SDMX | series | that one `(indicator, currency)`, named on its own line; the other series are served and the source reads `partial`. A series that answers with nothing is a failure only where the window could judge it, see below |
| Eurostat | series | that one `(indicator, currency)`; it serves the euro's employment change and level, each its own request |
| Central banks and debt offices | source, one per provider | that provider's currency; each institution is its own source since #218 |
| CFTC COT | source | every contract; the dollar is derived from the other seven, so the series are not independent and series scope is not ruled on |
| Stooq | source | every price proxy; not ruled on |
| Forex Factory | source | the blackout feed, which reports unknown coverage rather than clear |
| Manual | source | every hand-keyed entry, which is right: one malformed file is one file to fix |
---

## FRED

Federal Reserve Bank of St. Louis economic data. The backbone of the engine and
the only free source here with real cross-country reach.

**Base URL** `https://api.stlouisfed.org/fred/`

**Endpoints used** `series/observations`, `series`, `series/vintagedates`,
`series/search`. All four verified live.

### Getting a key

1. Create an account at <https://fredaccount.stlouisfed.org/apikeys>.
2. Request a key. The form asks you to describe the application you intend to
   write. A paragraph describing a personal macro research tool that pulls a
   few dozen series daily is accepted, and the key is issued on submission
   with no manual review. FRED's terms permit personal and commercial use of
   the data; the restrictions are on redistribution and on implying
   endorsement.
3. It is a 32-character lowercase alphanumeric string.
4. Export it. Bash: `export FRED_API_KEY=your_key_here`. PowerShell:
   `$env:FRED_API_KEY = "your_key_here"`, or
   `[Environment]::SetEnvironmentVariable("FRED_API_KEY", "your_key_here", "User")`
   to persist it across sessions.

`fbe.config.default_config` reads `FRED_API_KEY` from the environment, so the
key never has to appear in a config file or a commit. `.env.example` exists as
a template for a local, git-ignored record of the key, but nothing in the
package loads `.env`. A key that lives only there is not seen by the engine.

The key was obtained and verified on 2026-09-11: `series/observations` for
`FEDFUNDS` with `file_type=json` returned 200 and the expected observations.
Before that date the endpoints in this document were verified only by their
400 responses to unauthenticated calls.

### Parameters worth knowing

| Parameter | Notes |
| --- | --- |
| `series_id` | Required. |
| `api_key` | Required. |
| `file_type` | Set `json`. The default is XML. |
| `observation_start` / `observation_end` | `YYYY-MM-DD`. |
| `units` | `lin` as published, `pc1` percent change from a year ago, `pch`, `chg`, `ch1`, `pca`, `cch`, `cca`, `log`. |
| `frequency` + `aggregation_method` | Downsampling, `avg` / `sum` / `eop`. |
| `realtime_start` / `realtime_end` | ALFRED vintages. Default today. |
| `vintage_dates` | Comma-separated. |
| `limit` / `offset` / `sort_order` | Limit defaults to and caps at 100000. |

`units=pc1` computes year-on-year server-side, which is exactly the `yoy`
transform the registry asks for on index series. Use it rather than doing the
arithmetic locally.

Missing observations come back as the string `"."`, not null and not omitted. A
parser that does not handle that reads gaps in a series as zeros.

### ALFRED and look-ahead bias

Macro data is revised. FRED serves the latest vintage by default, so a backtest
that scores January 2024 using today's FRED data is using numbers nobody could
have seen until 2025. The model will look excellent and will be measuring
nothing.

ALFRED is the vintage archive, reached through the same endpoints by setting
`realtime_start` and `realtime_end` to a past date. `series/vintagedates` lists
every date a series was revised, which is what a backtest walks.

Live scoring does not need this. Any backtest does. `Observation` carries
`released_at` and `revision` for exactly this purpose.

No fetching source sets `released_at` today, so a historical run decides what
it could have seen from an assumed publication lag instead. That lag is per
leg, and the next section says how it is measured.

The lag dates an original print and cannot date a revision, so an observation
carrying `revision` above zero must carry `released_at` too. The rule is
enforced where observations are built, in
`fbe.datasources.base.checked_vintage`, and again where they are read, in
`BasePillar._visible`. ADR 0007 records why, and the manual-entry section below
says what it means for a typed correction.

### Publication lag, per leg

Without a `released_at`, `BasePillar._visible` admits an observation on
`period + lag`, where `period` is the first day of the span the figure
describes. The lag comes from `registry.publication_lag`: the leg's own
`SeriesRef.publication_lag_days` where one has been measured, and otherwise
`DEFAULT_PUBLICATION_LAG_DAYS` for the observation's frequency, 45 days
monthly and 120 quarterly. Both live in `fbe.datasources.registry`, because
the registry is where the release calendar is known and it cannot import from
the pillars.

One number per cadence was wrong for about a third of the registry. FRED's
mirror of the OECD tables carries a monthly figure 75 to 165 days after its
month starts, and irregularly: the same series arrives at 75 one month and 134
the next. Under a 45-day assumption a backtest dated August scored a June
trade balance that FRED did not hold until September, which flatters. #222
measured the slow legs and keyed each one.

**How a lag is measured.** FRED's archive answers `series/observations` with
`output_type=4` (initial releases only) and a `realtime_start` on each
observation, which is the day the figure first appeared on FRED. The lag is
that date minus the period start, taken over the twelve newest observations,
and the value keyed is the largest of the twelve. The largest rather than the
median because a lag that is too short flatters a backtest and one that is
too long only delays a figure the engine would have seen a little sooner. The
measurement and its date go in the leg's `note`, so the next verification can
tell a number that was measured from one that was typed.

**The bound the registry walk enforces.** On `VERIFIED_ON`, every verified
leg's newest print must be no older than its lag plus one cycle, where the
cycle is `CYCLE_DAYS`: the longest calendar gap between consecutive periods
of a punctual series, 31 days monthly, 92 quarterly, 366 annual, 7 weekly and
4 daily (a Friday close is the newest print until Tuesday over a long
weekend). A leg that cannot meet that with an honestly measured lag is not
late, it is dead, and it loses `verified`. `tests/test_publication_lag.py`
walks the registry. On 2026-09-21 it retired `current_account_gdp` for all
eight currencies, `indpro_yoy` EUR and `retail_sales_yoy` AUD.

The same two numbers, lag and cycle, are what the staleness ramp is built from
once #126 lands (decision record 0014): full weight until the next print is
due, zero one cycle after that.

### Rate limits

The published terms state no number. They reserve the right to limit bandwidth
and transaction volume, and 429 responses do occur. The commonly cited
operational ceiling is 120 requests per minute per key; **that figure is not in
the documentation and is treated here as unverified**. The client is configured
at 60 per minute with a 200ms floor between calls. A full G10 refresh is under
150 requests and runs once a day, so there is nothing to gain by pushing.

### Terms of use

<https://fred.stlouisfed.org/docs/api/terms_of_use.html>

Two obligations bind this project:

- **Attribution.** Any user-facing surface must display: "This product uses the
  FRED(R) API but is not endorsed or certified by the Federal Reserve Bank of
  St. Louis." The report template must carry it.
- **Third-party data.** Many series here originate with the OECD, Eurostat and
  the IMF and carry their own copyright. Personal use is fine. Redistributing
  the numbers is not, without asking the data owner.

Also prohibited: building something that replicates or replaces the essential
FRED user experience.
---

## OECD SDMX API

The fix for FRED's frozen mirrors, and the source that closed the inflation gap.

**Base URL** `https://sdmx.oecd.org/public/rest/`

No key, no registration. Data requests are
`data/{agency},{dataflow},{version}/{key}`, where the key is dot-separated
dimension values and an empty segment is a wildcard. Ask for flat CSV with an
`Accept: application/vnd.sdmx.data+csv` header, which works more reliably here
than the `format=` query parameter.

### Key arity, and why it matters

The key must carry exactly as many segments as the dataflow has dimensions.
The two failure modes read very differently:

- **Too few** returns HTTP 422 with a helpful body: `Not enough key values in
  query, expecting 8 got 7`.
- **Too many** returns HTTP 404 with the body `NoRecordsFound`, which is
  indistinguishable from a valid query that matched nothing.

Count the dimensions from the datastructure rather than guessing. Prices
(`DSD_PRICES`) has 8: `REF_AREA`, `FREQ`, `METHODOLOGY`, `MEASURE`,
`UNIT_MEASURE`, `EXPENDITURE`, `ADJUSTMENT`, `TRANSFORMATION`. Short-term
statistics (`DSD_STES`) has 9, adding `ACTIVITY` and `TIME_HORIZ`.

Versions are part of the URL and are not interchangeable: `DF_FINMARK` answers
at `4.0` and 404s at `4.1` and `1.0`.

### Rate limits: real, undocumented, enforced

A broad wildcard query triggered HTTP 429 within a minute, with a plain-text
body beginning "You have exceeded the number of requests for data downloads or
very large data ranges permitted in the OECD Data API".

Two consequences:

- Narrow every request to the exact key and period wanted. Pulling a whole
  country's dataflow and filtering locally is the natural thing to write and is
  what gets a client blocked.
- Treat a 429 as an error. The body is prose, not SDMX, so a parser that does
  not check will read it as zero observations and report a live currency as
  uncovered when it is merely throttled.

The client is configured at 10 requests per minute with a 3-second floor, set
from observed behaviour rather than documentation because there is none. A full
G10 refresh runs in under a minute at that pace and has not been throttled.

### The two price dataflows

Countries are split across two dataflows by classification vintage, and the
split does not follow anything you would guess.

| Dataflow | Holds |
| --- | --- |
| `DSD_PRICES@DF_PRICES_ALL` (COICOP 1999) | USA, GBR, CAN, DEU, AUS, NZL |
| `DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL` (COICOP 2018) | JPN, CHE, and also CAN, AUS |

Neither covers all eight. A currency queried against the wrong flow returns
`NoRecordsFound`, which looks exactly like a dead series, so
`oecd.CPI_FLOW` and `oecd.CORE_CPI_FLOW` are load-bearing rather than a
convenience. Both were built by querying one currency at a time.

Switzerland is stranger still: its core CPI is in neither general flow and
lives only in the dedicated core dataflow
`DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG`. That is the only free
Swiss core inflation series found anywhere.

Headline CPI year-on-year is `{AREA}.{M|Q}.N.CPI.PA._T.N.GY`; core swaps `_T`
for `_TXCP01_NRG`. `PA` with `GY` means the growth rate is already computed, so
the registry's transform hint is `level`, not `yoy`.

`REF_AREA` for the euro is **`DEU`, not the euro area**. `EA20` exists and
answers for the financial market flow but not for national CPI, where the OECD
serves member states rather than the bloc. Euro CPI therefore comes from
Eurostat through FRED, not from here.

### An empty answer, and when it means the series is dead

A well-formed CSV with no rows is the answer to two different questions and the
body does not say which. The series can be dead or mis-keyed, which ADR 0015
rule 3 raises on and names. Or the window can be too narrow to hold a print, in
which case a live series answers the same way.

`fbe refresh --since` is where the second one appears, because the window is
then the operator's and reaches the wire as `startPeriod` unaltered. Asked for
five days, no series of any cadence can answer: five days less even the 45-day
monthly lag is short of a 31-day cycle, so all 38 come back with no rows, and
before #298 each one was named as dead or mis-keyed.

`registry.empty_is_judgeable` decides which is which, from the leg's own
frequency and lag:

```
judgeable  iff  (end - publication_lag) - start  >=  CYCLE_DAYS[frequency]
```

Where it says yes, the source raises `SourceError` and the series is a named
failure, as before. Where it says no, the source raises `WindowTooNarrow` and
the refresh line says the series was not judged, which is neither a reading nor
a fault. The default five-year lookback is judgeable for every frequency, so
nothing about a normal run changes. The arithmetic and the reasoning are in
ADR 0015's 2026-09-28 amendment.

The request is still made, so a narrow refresh still spends one request per
series against a provider that rate-limits. Only the false cause is fixed.

### Financial market flow

`DSD_STES@DF_FINMARK` at version `4.0`, keyed
`{AREA}.M.{MEASURE}.PA......`, carries all eight currencies current to about
one month behind, which is two months ahead of FRED.

| Measure | What |
| --- | --- |
| `IRSTCI` | overnight / call money rate, the policy rate proxy |
| `IRLT` | 10-year government bond yield |
| `IR3TIB` | 3-month interbank rate |
| `SHARE` | share price index (use `IX` not `PA` for `UNIT_MEASURE`) |
| `CC` | exchange rate against USD |
| `CCRE` | real effective exchange rate |

`IRSTCI` is the only current free short rate for the Swiss franc and the New
Zealand dollar: FRED's equivalents stopped at 2024-03 and 2024-12.

### Terms

<https://www.oecd.org/termsandconditions/>. Free for personal and
non-commercial use with attribution to the OECD. As with FRED, redistributing
the numbers is a different question from using them.
---

## Eurostat

`https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/`, no key,
JSON-stat 2.0. Added for one series (#354): euro-area total employment from the
quarterly national accounts, `namq_10_pe?geo=EA20&unit=THS_PER&na_item=EMP_DC&s_adj=SCA`, which both EMPLOYMENT keys read,
the change under `diff` and the level under `level`.

- **`EA20`, not `EA`.** `EA` is the euro area at its composition on each date,
  so Bulgaria joining on 1 January 2026 adds about 3.6 million people at
  2026-Q1 (176,250 thousand against `EA20`'s 172,640, read live on
  2026-10-07). Differenced, that is a hiring boom that never happened. `EA20` is
  the twenty members of 2023-2025 throughout.
- **A filter that matches nothing answers HTTP 200** with an empty `value` and a
  zero in `size`. The source treats that as a dead or mis-keyed series over a
  window wide enough to judge, never as no data.
- **The change skips holes.** A quarter missing from the level leaves the
  change for it and the next quarter missing, rather than a change over two
  quarters.
- **Lag:** 189 days, the 2026-Q2 print's age on 2026-10-07 when nothing newer
  was out. The API gives no release date.

## Central banks and debt offices

The 2-year government bond yield, from the institutions that issue the bonds.

### Why this exists

FRED carries no 2-year government yield for any non-US G10 issuer. Verified by
search, not assumed. That matters more than a single missing series normally
would: the monetary pillar draws most of its sub-weight from the 2-year, and
monetary is the heaviest pillar in the composite. A currency without a 2y fails
the pillar's component floor, loses the pillar entirely, and takes a coverage
demotion on top. The model still runs and still prints numbers, which is worse
than failing outright.

Every issuer publishes the number free and without a key. The cost is that
there is no single API: seven providers, seven formats, seven failure modes.
Each covers exactly one currency, so none can substitute for another and losing one
provider means losing a currency's heaviest pillar rather than degrading a
series.

**What fails loudly, and at what scope.** Each provider is its own source:
`EcbSource`, `BocSource`, `MofJpSource`, `BoeSource`, `RbaSource`, `SnbSource`
and `RbnzSource`, sharing `CurvesSource` as their base, which holds the parsers
and is not itself a source in a run. A provider that raises, or that answers
with no session inside the window, is a failed source under its own name on the
refresh output, and its currency reaches the score as an explicit absence: the
monetary pillar prints n/a for that currency and coverage is demoted. The other
six providers' yields still land. `fbe refresh -s ecb` selects one provider;
there is no `-s curves`. Until #218 the seven sat behind one `curves` source,
and one central bank's website being down cost every two-year yield in the run.
ADR 0013 records the ruling and why the split was preferred over a partial
status on one source.

### The providers

| Currency | Provider | Identifier | Format | Status |
| --- | --- | --- | --- | --- |
| USD | FRED | `DGS2` | JSON | current |
| EUR | ECB Data Portal | `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` | SDMX / CSV | current |
| GBP | Bank of England | yield curve archive, 2.0y column | ZIP of XLSX | current |
| JPY | Japan Ministry of Finance | `2Y` column | CSV, Shift-JIS | current |
| CAD | Bank of Canada Valet | `BD.CDN.2YR.DQ.YLD` | JSON / CSV | current |
| AUD | Reserve Bank of Australia | `FCMYGBAG2D`, table F2 | CSV | current |
| CHF | Swiss National Bank | cube `rendeiduebd`, `CHF` / `2J` | CSV | current, published monthly |
| NZD | Reserve Bank of New Zealand | `INM.DG102.NZZCF`, table B2 | XLSX | current, **owner's connection only** |

**Bank of Canada Valet.** `https://www.bankofcanada.ca/valet/`, JSON or CSV, no
key. `observations/{series}/json?recent=N` or `start_date`/`end_date`. Group
`bond_yields_benchmark` lists the whole curve. The JSON nests each value as
`{"d": date, "<series>": {"v": value}}`, the one shape here that is not a flat
row. Every payload embeds a `terms` link; honour it.

**ECB Data Portal.** `https://data-api.ecb.europa.eu/service/data/` plus the
SDMX key, then `?format=csvdata` and `lastNObservations=N` or `startPeriod`.
Note what this series is: the euro-area **AAA** government spot curve, Svensson
fit, so it is close to the Bund and carries no periphery spread. That is the
right choice for a currency bias, since the euro trades on the core, but it is
not a GDP-weighted euro-area yield.

**Japan Ministry of Finance.** Two CSVs, both needed. The current month is at
`.../interest_rate/jgbcme.csv` and the history from 1974 at
`.../interest_rate/historical/jgbcme_all.csv`; the history file stops at the end
of last month, so today's number only appears in the first. Columns are
`Date,1Y,2Y,...,40Y`. Dates are `YYYY/M/D` without zero-padding. The files are
**Shift-JIS**, not UTF-8, and carry a Japanese-language footer row that is not
data. Missing tenors appear as `-`.

**Bank of England.** Two different things, do not confuse them, and one thing
they share: **both refuse a Python client's default `User-Agent` with HTTP
403**, before any body is served. Probed live on 2026-09-24: `python-httpx` and
`python-requests` strings answered 403 on the database and on the archive, and
a string naming this project answered 200 on both. `CurvesSource` therefore
sends `fundamental-bias-engine/0.1 (+repository URL)` on every request, set as
the class's `default_headers` so it reaches all seven providers. If a 403 from
this host reappears, check that header first; without it the base class reads
the 403 as a wrong request and does not retry, so GBP loses Bank Rate and every
provider here loses its 2-year yield for the run (#273).

The interactive database at `/boeapps/iadb/fromshowcolumns.asp` serves flat CSV
for named series. Query: `csv.x=yes`, `Datefrom`/`Dateto` as `DD/Mon/YYYY`,
`SeriesCodes`, `CSVF=TN`, `UsingCodes=Y`, `VPD=Y`, `VFD=N`. It **returns a
redirect**, so the client must follow one, and it answers an unknown code with
an HTML page rather than an error. Verified codes: `IUDBEDR` (Bank Rate),
`IUDSOIA` (SONIA), `IUDSNZC` (5y nominal zero coupon), `IUDMNZC` (10y),
`IUDLNZC` (20y). There is no 2-year code; probed and confirmed absent.

The 2-year therefore comes from the yield curve archive at
`/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip`,
member `GLC Nominal daily data current month.xlsx`, sheet `3. spot, short end`.
Column A is an Excel serial date counted from 1899-12-30. **Select the 2-year
column from the header row, never by letter**: the maturity grid has been
changed before, most recently when the curve was extended to 40 years, and a
hard-coded letter would then silently return a different tenor.

This is the only source in the whole registry that needs a spreadsheet reader.
See "For the integrator" below.

**Reserve Bank of Australia.** `https://www.rba.gov.au/statistics/tables/csv/`
`f2-data.csv`. The CSV carries about ten metadata rows before the data, with
the series IDs on the row labelled `Series ID`. Find that row by its label; the
number of rows above it is not stable across tables or releases. Dates are
`DD-Mon-YYYY`.

**Swiss National Bank: current, published once a month (#351).**
`https://data.snb.ch/api/cube/rendeiduebd/data/csv/en?dimSel=D0(CHF),D1(2J)&fromDate=...`,
semicolon delimited, two metadata lines before the header `Date;D0;D1;Value`.
`D0` is the bond category, where `CHF` is the Swiss Confederation, and `D1` is
the maturity, where `2J` is the 2-year point. Nine other categories,
euro-area governments, cantons and banks, quote the same `2J`, so the parser
matches both columns even though the request already selects them. Without `fromDate` the cube answers only
about the last month. A blank value is a Swiss holiday and is left out, never
read as zero.

The SNB publishes this cube once a month, the whole month at once on the first
working day of the next: `PublishingDate` 2026-10-01 carried 2026-09-01 to
2026-09-30. The registry gives the ref a 35-day publication lag so a backtest
cannot read a session before it was published. The cost is that a live run
reads the Swiss two-year as it stood about five weeks earlier. The older cube
`rendoblid` stopped at 2025-07-31, and until this one was found CHF had no
free two-year at all.

**Reserve Bank of New Zealand: current, from a workbook the owner downloads by
hand, or from the wire where the RBNZ allows it.**

*The drop-in file.* Download
`https://www.rbnz.govt.nz/-/media/project/sites/rbnz/files/statistics/series/b/b2/hb2-daily-close.xlsx`
in a browser and save it in `data/manual/`, either under the browser's own name,
`hb2-daily-close.xlsx`, or as `rbnz-hb2-daily-close.xlsx`, which wins when both
are there (#350). While either file exists `RbnzSource` reads it on every run,
offline or not, and makes no request. It is git-ignored: public data, reproducible by one download, not
the audit trail. Refresh it weekly, or whenever you want a fresher number. A
file whose newest session is older, at the run's as-of date, than the
registry's staleness allowance for the NZD two-year (`staleness_allowance`,
9 days at the time of writing) is refused with a `SourceError` naming the
file, its newest session and this URL, so the refresh line reads `rbnz failed`
and NZD is an explicit absence rather than a quiet month-old yield. Delete the
file to go back to fetching from the wire. Approved in #282, built in #283.

*The wire.*
`https://www.rbnz.govt.nz/-/media/project/sites/rbnz/files/statistics/series/b/b2/hb2-daily-close.xlsx`,
statistical table B2, daily wholesale interest rates, the daily close workbook
from 2018. Sheet `Data` carries five header rows (group, tenor, notes, unit,
series ID) and then one row per session, with dates as real datetimes and
values as floats. Series `INM.DG102.NZZCF` is the secondary market government
bond closing yield at 2 years, in `%pa`, which is the registry's `percent` with
no conversion. The parser locates the column by that ID and checks the unit
row, because the 1, 2, 5 and 10 year columns sit side by side in the same unit
and a similar range, so one column out is a plausible wrong yield. 2178
sessions from 2018-01-03, current to the previous business day.

The 2-year column is blank for 286 of those sessions, 224 of them in 2020, the
longest run 255 consecutive sessions ending 2020-11-19, and none after 2021.
That reads as a period with no bond near the 2-year point rather than a feed
fault. For live scoring it is a non-issue. For a Phase 6 backtest it is a real
hole: roughly a year of 2020 has no NZ 2-year, and it is the year a backtest
of a macro model would most want to see. A blank is absence, never zero.

*Vantage.* The RBNZ answers every data-centre or cloud egress with HTTP 403
and its own "Website unavailable" page: "Enable JavaScript and cookies to
continue. Your access to the Reserve Bank website has been restricted." A
whole-domain JavaScript challenge, static `-/media/` file paths included.
That covers every network an unattended run on this project can use. From a
residential or mobile connection the same URL serves the workbook to a
browser or to curl, and not always to this engine: probed on 2026-09-24 from
the owner's home connection, curl with the engine's exact headers and HTTP
version was served and the engine's HTTP client was refused, so the block
keys on the TLS handshake rather than on anything in the request. No header
clears it, and none is tried. That is why the drop-in file above exists. Nothing
automated in this project fetches live data: the seven scheduled runs are
repository maintenance, CI runs the four checks and never fetches, and tests
never touch the network. The only place the engine runs against the live
internet is the owner's machine, which is the one vantage the RBNZ answers.
So the source works where the engine runs and nowhere else. If the engine is
ever run from a server or a scheduled box, NZD's `yield_2y` drops, the
monetary pillar falls below `MIN_COMPONENT_WEIGHT` for NZD again, and
`fbe doctor` reports the RBNZ as unreachable. That report is the block, not a
parsing fault, and it is the diagnosis rather than something to debug.

The registry's `verified` flag and `last_observed` on this ref therefore rest
on a check made from the owner's connection on 2026-09-15, not on a check a
run can repeat. That is a departure from how every other ref in the table was
verified, and it is recorded on the ref's note rather than left to look the
same.

*Attempt history.* Recorded so that nobody retries it a third time from a
vantage that has already answered. A further attempt counts as new evidence
only if it is made from an **unproxied residential or mobile connection**,
not from a data-centre or cloud egress. An attempt that does not meet that
condition is not a new row; it is a restatement of row 1.

| Date | Vantage | Paths | Status |
| --- | --- | --- | --- |
| 2026-09-09 | Session container, egress proxy, with and without a browser user agent | `rbnz.govt.nz` statistics pages, `nzdmo.govt.nz`, `debtmanagement.treasury.govt.nz` | 403 |
| 2026-09-10 | Session container, egress proxy | `rbnz.govt.nz`, every path tried including the direct statistics page | 403 |
| 2026-09-13 | Routine container, local egress proxy, browser user agent | `/statistics/series/exchange-and-interest-rates`, `/` | 403 |
| 2026-09-15 | Session container, egress proxy | the statistics page, the wholesale rates page, and two guessed `-/media/` file paths | 403 |
| 2026-09-15 | **Phone, LTE mobile carrier** | `/statistics/series/exchange-and-interest-rates`, then the B2 daily close download | **200, workbook retrieved** |

Verdict: a vantage block, not a publisher policy. The condition above was
written before the mobile attempt and the mobile attempt met it, which is what
settled the question. The block is documented here and in the registry note;
no further attempt from a container is warranted, because it proves nothing
the rows above have not already proved.

### Terms

All public-sector statistical publications, free to use, each with its own
terms page. None promises an SLA, none is versioned, and several serve
spreadsheets whose layout has changed before, which is why every parser here is
told to key off labels rather than positions.
---

## CFTC Commitments of Traders

Weekly futures positioning, and the only free read retail has on how crowded a
trade already is.

**Base URL** `https://publicreporting.cftc.gov/resource/`

No key required. Socrata query parameters are dollar-prefixed: `$select`,
`$where`, `$order`, `$limit` (default 1000, raise it explicitly), `$offset`,
`$group`.

### Datasets, all verified live

| Dataset | Identifier | Use |
| --- | --- | --- |
| TFF, futures only | `gpe5-46if` | **the one to use** |
| TFF, futures + options | `yw9f-hn96` | options positions are delta-ambiguous |
| Legacy, futures only | `6dca-aqww` | holds the ICE Dollar Index |
| Legacy, futures + options | `jun7-fc8e` | |
| Disaggregated, futures only | `72hh-3qpy` | commodities, not currencies |

Traders in Financial Futures is the right report for currencies. Legacy splits
traders into "commercial" and "non-commercial", a classification built for grain
markets in the 1920s that puts every financial participant in one bucket. TFF
splits the same open interest into dealer, asset manager, leveraged funds and
other reportables. Leveraged funds are the hedge funds and CTAs whose crowding
mean-reverts, which is the signal the positioning pillar wants.

### Contract codes, all verified live

| Currency | Code | Contract name | Exchange |
| --- | --- | --- | --- |
| EUR | `099741` | EURO FX | CME |
| GBP | `096742` | BRITISH POUND | CME |
| JPY | `097741` | JAPANESE YEN | CME |
| CHF | `092741` | SWISS FRANC | CME |
| CAD | `090741` | CANADIAN DOLLAR | CME |
| AUD | `232741` | AUSTRALIAN DOLLAR | CME |
| NZD | `112741` | NZ DOLLAR | CME |
| USD | `098662` | USD INDEX | ICE, Legacy report only |

Two contracts were renamed in early 2022: GBP appears as both "BRITISH POUND"
and "BRITISH POUND STERLING", NZD as both "NZ DOLLAR" and "NEW ZEALAND DOLLAR".
Match on the code, never on the name.

Column names in TFF, verified against a live row: `report_date_as_yyyy_mm_dd`,
`cftc_contract_market_code`, `open_interest_all`, `dealer_positions_long_all`,
`dealer_positions_short_all`, `asset_mgr_positions_long`,
`asset_mgr_positions_short`, `lev_money_positions_long`,
`lev_money_positions_short`, `other_rept_positions_long`,
`other_rept_positions_short`, `nonrept_positions_long_all`,
`nonrept_positions_short_all`. Socrata returns every numeric column as a string.

### Release timing: stale by design

The CFTC's own wording: released "each Friday at 3:30 pm Eastern Time (US),
using the data from the immediately preceding Tuesday of that week". Positions
are snapped at Tuesday's close and take three days to process.

When the Tuesday is a US federal holiday the snapshot moves to the Monday. The
TFF dataset carries twelve Monday report dates since 2007, each the day before
Christmas Day, New Year's Day, Independence Day or Veterans Day fell on a
Tuesday, and no weekday other than Monday and Tuesday ever appears (queried
live 2026-09-24, #274). The source accepts both and stamps the release as the
Friday of the report week; any other weekday is refused as a misread column.
The CFTC adds that federal holidays "may delay release by one or two days",
which the dataset does not record, so a holiday week's reading can carry a
Friday stamp a day or two before anyone could read it. That is the cost ADR
0011 already names for a suspended publication, and it is accepted here for
the same reason: closing it needs a release calendar this dataset lacks.

So on any Wednesday the freshest number is eight days old, and by the next
Friday morning it is ten. Nothing fixes that. What matters is that the engine
never treats a COT reading as current: the `Observation` period is the snapshot
day, `released_at` is the Friday, and the staleness penalty sees the real age.

### Deriving a dollar position

There is no liquid CME dollar contract in TFF. The ICE Dollar Index carries
roughly 50,000 contracts of open interest against the euro contract's 800,000,
so it is a cross-check, not a signal.

The dollar read comes from the complement. Every one of the seven contracts is
long the foreign currency and therefore short dollars. Net the seven, flip the
sign, and that is the implied speculative dollar position. The legs must be
normalised first: raw contract counts are not comparable across contracts with
different notionals, and open interest differs by more than an order of
magnitude between EUR and NZD. Each leg is normalised by its own open interest
before the sum, in the source, because `cot_net_pct_oi` is a percent of open
interest and sources emit canonical keys. Only `fetch_contract` hands back raw
rows, which is what `derive_usd_position` reads.

A week that one of the seven did not publish has no dollar reading at all. A sum
cannot tell an absent leg from a leg at zero, so summing the six that did
publish would report the dollar as less exposed than they imply, with nothing
marking it: on the 2026-09-08 capture, losing the Canadian leg alone moves the
dollar from +30.64 to +14.08. One contract answering with nothing for a whole
window is the other case and stops the run, because the dollar series would
otherwise just stop, and that reads as a dollar with no positioning.

### Rate limits and terms

Anonymous callers are throttled by IP. A free `X-App-Token` header from the
Socrata developer portal moves you onto a higher shared budget; a weekly refresh
of eight contracts does not need one. The data is US government work product and
is public domain.
---

## Stooq

Daily price history for the equity indices FRED only carries monthly.

**Endpoint**
`https://stooq.com/q/d/l/?s={symbol}&d1={YYYYMMDD}&d2={YYYYMMDD}&i=d`

Returns CSV with header `Date,Open,High,Low,Close,Volume`, one row per session,
oldest first. Symbol conventions: FX pairs are six lower-case letters
(`eurusd`), indices carry a leading caret (`^spx`), US equities take `.us`,
futures take `.f`. `i` is `d`, `w` or `m`.

### Verification status: unverified

The URL shape and symbol conventions above are confirmed from Stooq's own
download pages and from several independent client implementations. They could
**not** be confirmed against a live response.

Stooq now sits behind a JavaScript proof-of-work challenge. Every request from
an automated client returned either the challenge page (HTTP 200, HTML body) or
a reset connection. Every symbol in `prices.STOOQ_SYMBOLS` is therefore marked
unverified. Fetch one by hand in a browser and confirm the CSV before relying on
any of them.

This is also why FRED stays the primary price path. A source that can start
serving an anti-bot page instead of data will fail on a Monday morning, and the
engine should degrade to a monthly OECD share price index rather than to
nothing. Note the specific trap: the challenge page returns HTTP 200, so a
status-code health check reports a broken source as healthy and the parser reads
an empty price series. Check the response shape, not the status.

### Yahoo Finance fallback

`https://query1.finance.yahoo.com/v8/finance/chart/{symbol}` returns JSON quotes
and is widely used. There is no public API and no licence. Yahoo's terms permit
personal use through their own site and prohibit redistribution and automated
scraping; the endpoint is undocumented, unversioned and has broken without
notice more than once.

Treat it as a manual escape hatch for one missing symbol on a personal account.
It is deliberately not wired into the code. A dependency with no licence does
not belong in a scheduled job.
---

## Forex Factory calendar

Feeds the news blackout the trading plan requires: "Avoid trading during
high-impact news events to minimize exposure to excessive volatility and
unpredictable price movements."

**Feed** `https://nfs.faireconomy.media/ff_calendar_thisweek.json`

Verified live: HTTP 200, `application/json`, an array of objects with exactly
six keys. The same week is also at `.xml` and `.csv`, both verified. Siblings
`ff_calendar_nextweek.json`, `ff_calendar_lastweek.json` and
`ff_calendar_thismonth.json` all return 404, verified.

### Schema

```json
{
  "title": "ANZ Job Advertisements m/m",
  "country": "AUD",
  "date": "2026-09-06T21:30:00-04:00",
  "impact": "Low",
  "forecast": "",
  "previous": "0.8%"
}
```

| Field | Notes |
| --- | --- |
| `title` | Display string. No event type field exists, so classification is substring matching on this. |
| `country` | An ISO 4217 **currency** code despite the name. G10 set plus `CNY` and the literal `"All"`. |
| `date` | ISO 8601 with explicit offset, `-04:00` in the payload checked, which is US Eastern. The offset tracks US daylight saving, so parse it rather than assuming. Convert to UTC before comparing. |
| `impact` | `High`, `Medium`, `Low` or `Holiday`, capitalised exactly so. |
| `forecast`, `previous`, `actual` | Display strings, not numbers: `"0.8%"`, `"-1.2K"`, `"3.75%"`, or empty. `actual` is absent until the release lands. |

A week carries roughly 100 rows across all currencies. The payload captured for
the tests holds 105.

`Holiday` is not an impact level, it is a market closure. Treating it as low
impact means trading into a thin book. It belongs in a liquidity check, not a
volatility blackout.

Only `High` triggers a blackout by default. Widening to `Medium` blacks out most
of the London session most days, which for an account running one to three
positions means never trading.

Currency mapping: the seven G10 codes map to themselves, `"All"` maps to
`"GLOBAL"`, everything else is dropped. `CNY` appears regularly and matters for
AUD and NZD through the commodity channel, but a Chinese release is not on the
plan's list and is not a reason to stand aside from a G10 pair.

### The plan's ten events to avoid

`calendar.AVOID_PATTERNS` maps each to substrings drawn from real feed titles.

| # | Plan's item | Match approach |
| --- | --- | --- |
| 1 | Non-Farm Payrolls | "non-farm employment change", "employment change" |
| 2 | Interest rate decisions | "federal funds rate", "main refinancing rate", "official bank rate", "official cash rate", "monetary policy statement" |
| 3 | GDP reports | "gdp m/m", "gdp q/q", "gdp y/y", "prelim gdp", "final gdp" |
| 4 | CPI and PPI | "cpi ", "core cpi", "ppi ", "core ppi", "trimmed mean cpi" |
| 5 | Retail sales | "retail sales", "core retail sales" |
| 6 | UK and Canada employment | "claimant count change", "average earnings index", "unemployment rate", "unemployment claims" |
| 7 | Trade balance | "trade balance" |
| 8 | Central bank speeches | "speaks", "press conference", "testimony", "monetary policy report hearings" |
| 9 | FOMC minutes, ECB accounts | "fomc meeting minutes", "monetary policy meeting accounts" |
| 10 | Geopolitical events | "election", "referendum", "summit", "budget" |

Two honest caveats. Item 8 matches the bare word "speaks", which also catches
heads of government; the plan's item 10 arguably wants those anyway. Item 10 is
close to useless, because scheduled political events are what this feed covers
worst. **Item 10 stays a human judgement.** The report should say so rather than
implying it is handled.

**This map tags a row. It does not block one.** The blackout decision is
`fbe.calendar_guard.HIGH_IMPACT_KEYWORDS` with `BLACKOUT_IMPACTS`, and the two
are deliberately separate: a merge would make this map stricter for reasons
that have nothing to do with blackouts. Both carry the same ten class names so
a report can join a tag to a blackout, but a title can land under different
keys in each, so a join goes on the event rather than on the key (#228).

**One row is tagged here and deliberately not blocked there.** "Unemployment
Claims" prints weekly, so blocking it would open a blackout on all seven dollar
pairs one day in five, permanently. It is tagged as an employment row and the
morning is not called untradeable, so a report showing the class can carry it.
No renderer shows the class yet, so that is what this map makes possible rather
than what a page prints today. Ruled on #228, and
`tests/test_blackout_windows.py` pins every row of the committed capture the
two maps read differently, with the reason beside each.

### Terms and limits

The feed is unofficial. It is not affiliated with, endorsed by, or sponsored by
Forex Factory or Fair Economy, Inc.

The publisher rate-limits calendar exports by IP and returns a "Request Denied"
page when a caller exceeds it. Their guidance is to download once a week and
work from the copy. Fetch at most once a day, cache for hours, never poll on a
timer. The parser must treat "Request Denied" as an error, not as an empty week:
an empty calendar reads as "nothing scheduled" and would clear every blackout at
once.

Because the feed covers only the current week, a Friday run cannot see Monday's
events. Fetch early in the week and keep the cached copy.

`CalendarSource.horizon` reports that limit as a number rather than leaving it
to be remembered: the latest instant the week it holds can vouch for, or `None`
when it holds nothing usable. It is drawn from the events actually present, not
from the calendar week they came out of, because the feed carries no field
saying how far it runs and claiming a full week from a payload ending on Tuesday
would assert coverage of three days nobody has seen. `fbe.calendar_guard` reads
it alongside the events to build a `CalendarCoverage`. The source does not build
one itself: `CalendarCoverage` lives in the guard, which is downstream, and the
import edge between the two stays absent in both directions.

`CalendarSource.available` answers on configuration rather than reachability,
which is the contract every source here holds and which
`tests/test_datasource_base.py` enforces by asserting no request is made. Online
it is always `True`, since the feed needs no credential. Offline it is `True`
only when the cache holds a week that still decodes, so a rate-limit page left
behind by an older run answers `False` rather than promising a week that
`events` will refuse. An unreachable feed is not reported here at all; it is
reported by `events` raising, which is the only place a failed fetch can be told
apart from a quiet week.
---

## Manual entries

The escape hatch, and a permanent part of the design rather than a temporary
workaround. Some of these numbers will never be free.

**Location** `data/manual/*.yaml`, pointed at by `DataConfig.manual_dir`. Files
are read in sorted filename order; later files override earlier ones for the
same `(indicator, currency, period)`.

### Schema

```yaml
meta:
  source: "S&P Global via investing.com"
  updated: 2026-09-08
  updated_by: "operator"
  notes: >
    Manufacturing PMI headline prints. Flash where marked, otherwise final.

observations:
  - indicator: pmi_composite
    currency: EUR
    value: 49.8
    period: 2026-08-01
    unit: index
    frequency: monthly
    released_at: 2026-09-01T08:00:00Z
    meta:
      release: flash
      source_url: "https://..."
```

Required: `indicator`, `currency`, `value`, `period`.
Optional: `unit`, `frequency`, `released_at`, `revision`, `meta`.

- `indicator` must be a key in the registry. An unknown key is an error, not a
  warning: a typo that silently creates a new indicator is invisible until a
  pillar quietly reports missing data.
- `currency` must be in `G10`, or `"GLOBAL"`.
- `period` is the first day of the span the number describes, not the day you
  typed it: `2026-08-01` for August 2026, `2026-04-01` for 2026Q2. That matches
  FRED and the OECD, so manual and fetched observations sort together, and it
  is the convention every module that ages an observation reads.
- `unit` and `frequency` should match the registry's `SeriesRef`. A mismatch is
  refused rather than coerced: a PMI entered as a percentage instead of an index
  will score, and score wrongly.
- `released_at` is optional but strongly wanted. Without it, staleness falls
  back to `period`, which overstates the age of a quarterly series by up to
  three months.
- **`revision` above zero must carry `released_at`.** A row with a revision and
  no release date is refused on load, naming the file and the row, and the run
  reports it as that source's failure rather than skipping the row. The reason
  is the one thing the assumed publication lag cannot do: it dates an original
  print, and a correction describes the same period as the print it corrects, so
  both get the same date. A June figure corrected in September, typed without a
  date, is then read by a run dated 16 July, which is a number that did not
  exist for another two months. Type the date the correction was published, or
  record the figure at revision 0 as what was published at the time. ADR 0007.
- `meta` is the right place for the URL the number came from. That provenance is
  the only audit trail a hand-typed number has.

### Suggested files

| File | Contents |
| --- | --- |
| `pmi.yaml` | Manufacturing and services PMIs for all eight. **Optional:** no pillar reads `pmi_composite` since ADR 0005, so nothing typed here changes a score. `ManualSource.missing()` still lists the eight gaps because the key stays registered; skip them. |
| `zz-overrides.yaml` | Ad-hoc corrections and the one-off gaps: AUD retail sales, EUR employment change, NZD dairy. Named to sort last, so it wins: precedence is filename order and nothing else, and `overrides.yaml` would sort ahead of `pmi.yaml` and be overridden by the file it exists to override. |

An `inflation.yaml` used to be needed for six currencies. It no longer is: the
OECD API supplies headline and core CPI for all eight. If you have one from an
earlier run, delete it rather than leaving it to override live data.

`cb_guidance_tone` is not in the registry, because it maps to no external
series. It is a judgement, and recording it as one, with the meeting date and
the reasoning in `meta`, beats pretending it falls out of the data.

**There is nowhere to put it today.** `ManualSource.load_file` validates every
row against the registry, which is what makes a typo an error rather than a
silent new indicator, and a key deliberately outside the registry fails that
same check. A `guidance.yaml` written as described refuses the whole manual
directory, not just that file. Carving the key out needs a unit and a frequency
for a series the registry does not describe, and inventing those is what this
source exists to prevent, so nothing does it yet. Tracked on #60.
---

## Cache

**Layout** under `DataConfig.cache_dir`, default `data/cache`:

```
data/cache/
  fred/
    a3f21c94e08b1d77.json
    a3f21c94e08b1d77.meta.json
  cftc/
  stooq/
  forexfactory/
```

One subdirectory per source, so a bad FRED response can be cleared without
costing the week's calendar. Each entry is the raw body plus a sidecar holding
the fetch time, the request and the source's own freshness stamp.

Raw bodies are stored rather than parsed observations. When a parser turns out
to be wrong, and one will, the cache still holds what the server actually sent,
so the fix is a re-parse rather than a refetch of history that may no longer be
available.

**Key derivation** SHA-256 over the source name, the endpoint path and every
request parameter, sorted, truncated to 16 hex characters. The API key is
excluded, so rotating a key does not invalidate the cache and no credential
reaches a filename. Date ranges are part of the key, so callers should request a
stable window (five years back to today) rather than one that shifts daily and
misses every time.

**TTL** `DataConfig.cache_ttl_hours`, default 12, applied uniformly. That is a
simplification with a cost, since a COT file is valid for a week and a VIX close
for a few hours. What each source actually wants is recorded in
`cache.SUGGESTED_TTL_HOURS`: fred 12, cftc 72, stooq 6, forexfactory 24, manual
0. TTL is measured from the fetch time in the sidecar, not file mtime, which a
backup or a checkout can change.

**Offline mode** `DataConfig.offline = True` makes every source read the cache
and never the network. This is what makes a run reproducible: identical inputs
regardless of when it runs, so yesterday's report can be re-derived, pillars can
be tested against fixed data, and a bad call can be debugged without the data
shifting underfoot. Offline serves expired entries rather than refusing them,
with the age attached, because a stale number with an honest staleness figure
beats a hole. A missing entry offline is an error, not an empty result: silently
returning nothing would show up as a coverage gap and hide a misconfiguration.
---

## Coverage reality

This is the section to read twice.

Two numbers are published for every indicator, and the difference between them
is the whole point.

- `registry.coverage_report()` counts a currency only when its identifier is
  verified, is not manual, **and** its newest observation falls inside the
  indicator's staleness allowance. This is the number that decides whether a
  pillar can score.
- `registry.identifier_coverage()` counts identifiers and ignores freshness. It
  is good for exactly one thing: telling a wiring problem apart from a data
  problem. If coverage is 0.0 and identifiers are 1.0, every identifier is
  right and the source stopped publishing.

`registry.stale_refs()` gives the registry's form: which currencies have a
ref whose hand-checked `last_observed` date has run past its allowance. It
reads metadata, not data, so it says when the registry is due a re-check.
What a run actually holds is `fbe.datasources.collect.observed_gaps`, which is
the list `fbe refresh` prints.

### Staleness allowances

There is no allowance table any more. Since #126 both of the ramp's ages are
derived from the leg, from the same two numbers the visibility rule reads:

    s0 = lag + cycle     the oldest a punctual newest print gets, full weight
    S  = lag + 2 * cycle one full cycle late, and worth nothing

`registry.full_weight_age` and `registry.staleness_allowance` return them, and
both the scorer and `coverage_report` read the second, so the registry and the
scoring side cannot give two answers about one series.

`IndicatorSpec.max_staleness_days` used to hold `S`, hand-keyed per indicator.
It was derived from the publication cadence, which was right, but it was one
number for the whole universe while the cadence is per leg, and its companion
`s0` came from a global ratio. A quarterly print was therefore 120 days old on
the day it was first visible against an `s0` of 60 to 90, so it began life on
the falling part of its own ramp and never reached full weight. Nine
indicators reached zero weight before their next print was due. Decision
record 0014 carries the ruling and the measurements.

**What replaces "do not widen the allowance".** The old warning was that
setting an allowance higher than the cadence justifies converts a visible gap
into an invisible one. The equivalent mistake now is overstating a leg's
`publication_lag_days`, which widens both bounds together. The protection is
the registry walk in `tests/test_publication_lag.py`: on `VERIFIED_ON` every
verified leg's newest print must sit inside `lag + cycle`, so a lag inflated to
carry a dead series fails on the next verification rather than passing quietly.

### Position as at 2026-09-09

| Indicator | Fresh | Identifiers | Verdict |
| --- | --- | --- | --- |
| `policy_rate` | 8/8 | 8/8 | good |
| `yield_2y` | 7/8 | 7/8 | CHF manual on this date, from the SNB since #351 (2026-10-07); NZD from the owner's connection only |
| `yield_2y_chg_1m` | 0/8 | 0/8 | derived from `yield_2y`; 0/8 on this date, 6/8 since #322 (2026-10-01), see below |
| `yield_2y_chg_3m` | 0/8 | 0/8 | derived from `yield_2y`; 0/8 on this date, 6/8 since #322 (2026-10-01), see below |
| `yield_10y` | 8/8 | 8/8 | good, but **consumed by no pillar**; see below |
| `cpi_yoy` | 8/8 | 8/8 | good, was 2/8 before the OECD API |
| `core_cpi_yoy` | 8/8 | 8/8 | good, was 2/8 |
| `gdp_yoy` | 8/8 | 8/8 | good |
| `unemployment_rate` | 8/8 | 8/8 | good |
| `employment_chg` | 7/8 | 7/8 | EUR manual on this date; from Eurostat since #354 (2026-10-07) |
| `employment_level` | 7/8 | 7/8 | EUR absent on this date, from Eurostat since #354; derived from `employment_chg`'s refs |
| `retail_sales_yoy` | 7/8 | 7/8 | AUD frozen at 2025Q2, unverified since #222; EUR, JPY and NZD from the OECD since #352 (2026-10-07) |
| `indpro_yoy` | 4/8 | 4/8 | 4/8 on this date; 8/8 since #352 (2026-10-07), EUR, CHF, AUD and NZD from the OECD |
| `pmi_composite` | 0/8 | 0/8 | licensed, entirely manual; **consumed by no pillar** since ADR 0005, see below |
| `business_confidence_mfg` | 8/8 | 8/8 | free; carries 0.30 of GROWTH since ADR 0005, see below |
| `trade_balance` | 8/8 | 8/8 | good |
| `current_account_gdp` | 0/8 | 0/8 | all eight frozen at 2024Q4 on this date; 8/8 from the OECD since #353 (2026-10-07) |
| `gdp_nominal_usd` | 8/8 | 8/8 | annual; one year behind on every leg by design |
| `cot_net_pct_oi` | 8/8 | 8/8 | good, 8-10 days stale by design |
| `equity_index` | 8/8 | 8/8 | USD and JPY daily, rest monthly; **consumed by no pillar**, see below |
| `world_equity_index` | global | global | good; the equity half of the risk regime |
| `vol_index` | global | global | good |
| `commodity_price` | global + CAD, AUD | | NZD dairy manual |

### The gaps that remain, and what each costs

**1. The CHF two-year yield. Closed by #351 (2026-10-07).** The cube this
section once called a discontinuation, `rendoblid`, did stop at 2025-07-31.
The SNB carries the same Confederation curve in cube `rendeiduebd`, current
and published monthly, and the registry now reads the 2-year from it. See the
Swiss National Bank entry above for the five-week publication lag this costs.

The New Zealand dollar's was an access problem, not an availability one, and
it is closed by the drop-in file: the RBNZ entry above reads the 2-year from
`data/manual/rbnz-hb2-daily-close.xlsx` when the owner has downloaded it, and
from the wire where the RBNZ allows that. It reopens only if the file goes
stale, which the source refuses loudly rather than scores quietly, and the
entry above says what that looks like.

Why the 2-year matters this much: it carries three of MONETARY's five
components, `yield_2y` at 0.25 and the two change series at 0.20 and 0.25.
Losing it leaves 0.30 of the sub-weight, below `MIN_COMPONENT_WEIGHT` (0.50),
so the whole pillar drops, and MONETARY is 0.30 of the composite. There is no
10-year fallback.

**2. PMIs, all eight.** S&P Global and ISM license these and no free API
carries them. Permanently manual unless a licence is bought. Since ADR 0005 no
pillar reads `pmi_composite`: GROWTH's leading component is
`business_confidence_mfg` instead, so this gap costs no score and entering
PMIs is optional. The key stays registered so re-adopting it is a one-line
change if a licence is ever bought.

**3. Current account, all eight. Closed by #353 (2026-10-07).** Every leg of
the FRED `B6BLTT02` family stopped at 2024Q4. This section used to say the
OECD's balance-of-payments flows carried no current account balance; that was
wrong. `OECD.SDD.TPS,DSD_BOP@DF_BOP,1.0` carries it as a share of GDP, key
`{AREA}.WXD.CA.B.T.Q.PT_B1GQ.Y`, for all eight areas through 2026-Q2, with
`EA20` for the euro. Two things to know:

- In this structure `FREQ` is the sixth key segment, not the second as in every
  other OECD flow here. `fbe.datasources.oecd.frequency_position` reads it from
  `DIMENSIONS`.
- The Swiss leg swings with gold trade through Swiss refiners: 0.14% of GDP in
  2025-Q4, then 10.02% in 2026-Q1. It is served as published. Whether to smooth
  it is a scoring question and is not taken here.

**4. Industrial production. Closed by #352 (2026-10-07).** The euro area,
Switzerland, Australia and New Zealand now come from the OECD's key short-term
indicators flow, `DSD_KEI@DF_KEI`, as total industry excluding construction
(`BTE`), volume, growth on a year earlier. The euro leg is the bloc (`EA20`),
replacing the German proxy that stopped at 2023-12. The Swiss, Australian and
New Zealand legs are quarterly, so they move less often than the monthly ones.

**5. AUD retail sales**, frozen at 2025Q2. Australia has no live retail series
on FRED, and the OECD's (`AUS.Q.TOVM.GR.G47.Y.GY` in the same flow) stops at
2025-Q2 too: the Bureau of Statistics replaced its retail survey with a
household spending indicator, which needs a source of its own (#355).

**6. EUR employment change. Closed by #354 (2026-10-07).** FRED's euro-area
level stopped at 2022-10. Eurostat's quarterly national accounts carry the bloc
itself, so the euro has both the change and the level, and its EMPLOYMENT
pillar scores. See the Eurostat section above.

### The euro-area substitution

Eurostat feeds FRED current HICP and current euro-area GDP, both true bloc
aggregates. But the euro-area aggregates for unemployment, employment, retail
sales, industrial production, trade and the current account all stopped between
2022 and 2023. Where that happens the registry falls back to the German
national series and says so in the note.

Germany is roughly a third of euro-area GDP. This is a real approximation, not
a free lunch, and it is biased in a knowable direction on at least one
indicator: Germany runs a structural trade surplus larger than the bloc's, so
the proxy flatters the euro on `trade_balance`.

Affected: `unemployment_rate`, `trade_balance`, `current_account_gdp`,
`equity_index`, and `yield_10y` where the Bund stands in for the euro curve.
`retail_sales_yoy` and `indpro_yoy` were on this list until #352 read the bloc
itself (`EA20`) from the OECD's key short-term indicators flow.

### Manual fallback per gap

| Gap | File | Where the number comes from |
| --- | --- | --- |
| PMIs, all eight (optional, read by no pillar) | `pmi.yaml` | S&P Global releases, ISM for the US |
| Retail sales AUD | `zz-overrides.yaml` | ABS monthly retail turnover |
| Industrial production CHF, AUD, NZD | `pmi.yaml` or its own file | the registry carries manual refs for all three, so `ManualSource.missing()` lists them. Entering them is optional and leaving them out is a visible gap rather than a broken run; what is not acceptable is inventing a figure. |
| Current account, all eight | none for now | leave the gap visible; find a live source |
| NZD commodity link | `zz-overrides.yaml` | GlobalDairyTrade index, fortnightly, globaldairytrade.info |
| Guidance tone, all eight | nowhere yet, see above | your own reading of the last statement |

### For the integrator

Two things outside this package need attention.

**`openpyxl` is a project dependency and the workbook reader is wired up.** The
GBP 2-year yield lives in the Bank of England's yield curve archive, which is a
ZIP of XLSX files and the only source in the registry that needs a spreadsheet
reader. `pyproject.toml` declares `openpyxl`, `CurvesSource.fetch_boe_curve`
reads the archive, and the pound therefore keeps its 2-year rather than falling
back to the reduced monetary pillar CHF is on. This paragraph said the
opposite until #59: the dependency was already declared and the claim had gone
stale.

One thing the workbook does not promise is its column layout. The maturity grid
has been re-cut before, and the two-year header is published as
`1.999999920000001` rather than `2.0`, so the column is located by nearest
header maturity within a tolerance and never by position or by equality.

**Sub-weight floors should account for the 2-year gap.** `MIN_COMPONENT_WEIGHT`
scores a pillar missing when a currency holds at or below half its sub-weight.
CHF holds 0.30 of the monetary pillar without a 2-year. Whether that
should drop the pillar entirely or score it on what remains is a scoring
decision, not a data one, but the data layer cannot close it and the scoring
layer should know it is there.
---

## Indicator registry

Generated from `src/fbe/datasources/registry.py`, which is the source of
truth. Every identifier was checked against its live source on 2026-09-09.

Two columns, and the difference between them is the point:

- **Verified** means the identifier was confirmed to exist and return
  observations. It says nothing about whether the series still updates.
- **Last obs** is the newest observation the source actually held on the
  verification date. This is what `coverage_report()` ages, and it is what
  stops a frozen series from counting as coverage.

A `manual` row is never verified, because that flag records the absence of
a free machine-readable source, not the operator's typing.

### Summary

| Indicator | Pillar | Unit | Freq | Allowance | Fresh | Identifiers |
| --- | --- | --- | --- | --- | --- | --- |
| `policy_rate` | monetary | `percent` | daily | 75d | 100% | 100% |
| `yield_2y` | monetary | `percent` | daily | 10d | 88% | 88% |
| `yield_2y_chg_1m` | monetary | `basis_points` | daily | 10d | 0% | 0% |
| `yield_2y_chg_3m` | monetary | `basis_points` | daily | 10d | 0% | 0% |
| `yield_10y` | monetary (unconsumed) | `percent` | monthly | 75d | 100% | 100% |
| `cpi_yoy` | inflation | `percent` | monthly | 200d | 100% | 100% |
| `core_cpi_yoy` | inflation | `percent` | monthly | 200d | 100% | 100% |
| `gdp_yoy` | growth | `percent` | quarterly | 270d | 100% | 100% |
| `unemployment_rate` | employment | `percent` | monthly | 270d | 100% | 100% |
| `employment_chg` | employment | `persons` | monthly | 270d | 88% | 88% |
| `employment_level` | employment | `persons` | monthly | 270d | 88% | 88% |
| `retail_sales_yoy` | growth | `percent` | monthly | 270d | 88% | 100% |
| `indpro_yoy` | growth | `percent` | monthly | 180d | 50% | 62% |
| `pmi_composite` | growth (unconsumed) | `index` | monthly | 75d | 0% | 0% |
| `business_confidence_mfg` | growth | `percentage_balance` | monthly | 270d | 100% | 100% |
| `trade_balance` | external | `usd` | monthly | 150d | 100% | 100% |
| `current_account_gdp` | external | `percent_of_gdp` | quarterly | 210d | 0% | 100% |
| `gdp_nominal_usd` | external | `usd` | annual | 916d | 100% | 100% |
| `cot_net_pct_oi` | positioning | `percent_of_open_interest` | weekly | 21d | 100% | 100% |
| `equity_index` | risk (unconsumed) | `index` | daily | 75d | 100% | 100% |
| `world_equity_index` | risk | `index` | daily | 7d | 100% | 100% |
| `vol_index` | risk | `index` | daily | 7d | 100% | 100% |
| `commodity_price` | external | `index` | monthly | 90d | 100% | 100% |

`Allowance` is the derived `S`, `lag + 2 * cycle`. `Fresh` is `coverage_report()`,
`Identifiers` is `identifier_coverage()`. Where the two differ, every
identifier is right and the source has stopped publishing.

### Full registry

#### `policy_rate`

The central bank's target rate, or the overnight rate that tracks it. The level matters less than where it sits relative to the rest of the G10, which is the whole premise of a relative-value framework.

Pillar: **monetary**. Canonical unit: `percent`. Staleness allowance: 75 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DFEDTARU` | percent | daily | level | yes | 2026-09-09 | fed funds target range upper limit, published daily |
| EUR | fred | `ECBDFR` | percent | daily | level | yes | 2026-09-09 | ECB deposit facility rate, the effective policy rate |
| GBP | boe | `IUDBEDR` | percent | daily | level | yes | 2026-09-01 | Bank Rate itself from the Bank of England database, which replaces the earlier SONIA proxy |
| JPY | oecd | `DSD_STES@DF_FINMARK/JPN.M.IRSTCI.PA......` | percent | monthly | level | yes | 2026-08-01 | uncollateralised overnight call rate; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| CHF | oecd | `DSD_STES@DF_FINMARK/CHE.M.IRSTCI.PA......` | percent | monthly | level | yes | 2026-08-01 | SARON-area overnight rate. Closes a real gap: FRED's IRSTCI01CHM156N stopped at 2024-03. |
| CAD | oecd | `DSD_STES@DF_FINMARK/CAN.M.IRSTCI.PA......` | percent | monthly | level | yes | 2026-08-01 | overnight money market rate; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| AUD | oecd | `DSD_STES@DF_FINMARK/AUS.M.IRSTCI.PA......` | percent | monthly | level | yes | 2026-08-01 | interbank overnight cash rate; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| NZD | oecd | `DSD_STES@DF_FINMARK/NZL.M.IRSTCI.PA......` | percent | monthly | level | yes | 2026-08-01 | overnight interbank rate. Closes a real gap: FRED's IRSTCI01NZM156N stopped at 2024-12. |

#### `yield_2y`

Two-year government bond yield, the market's own forecast of where policy goes next. The 2y differential is the strongest single fundamental driver of a G10 pair over a multi-week horizon, and the monetary pillar derives most of its sub-weight from it, so a missing leg here costs a currency the heaviest pillar in the model.

Pillar: **monetary**. Canonical unit: `percent`. Staleness allowance: 10 days. Fresh coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DGS2` | percent | daily | level | yes | 2026-09-08 | Treasury constant maturity |
| EUR | ecb | `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` | percent | daily | level | yes | 2026-09-08 | euro-area AAA government spot curve, 2-year point, Svensson fit. AAA issuers only, so this is close to the Bund and does not carry periphery spread. |
| GBP | boe | `GLC_NOMINAL_SPOT_SHORT/2.0` | percent | daily | level | yes | 2026-09-08 | UK nominal spot curve, 2-year point, from the yield curve archive. Read from sheet '3. spot, short end', the column whose header maturity is 2.0 years. Needs a spreadsheet reader, unlike every other source here. Two archives, both needed, the way the JGB row has two files: the current-month workbook alone held 22 sessions, too few for a one-month change, so the history archive `glcnominalddata.zip` is read beside it for the years the window covers (#323). The history holds completed months only and is republished once a month, on the second business day in October 2026 and the third in September, so for two or three business days after the 1st the previous month is in neither archive. The seven-day tolerance on the change window absorbs that for the first few sessions of a month; past it, the one-month change drops out and appears in the refresh gap list until the Bank republishes. |
| JPY | mof_jp | `2Y` | percent | daily | level | yes | 2026-09-08 | JGB par yield, 2-year column of the Ministry of Finance CSV. The current-month file and the full history are separate downloads; both are needed. |
| CHF | snb | `rendeiduebd/CHF.2J` | percent | daily | level | yes | 2026-08-31 | Swiss Confederation spot curve, 2-year point: SNB cube 'rendeiduebd', D0 'CHF' (the Confederation), D1 '2J' (#351). Published monthly, so the ref carries a 35-day publication lag; see the Swiss National Bank entry. |
| CAD | boc | `BD.CDN.2YR.DQ.YLD` | percent | daily | level | yes | 2026-09-08 | Government of Canada 2-year benchmark bond yield |
| AUD | rba | `FCMYGBAG2D` | percent | daily | level | yes | 2026-09-02 | Australian Government 2-year bond, interpolated, from RBA statistical table F2 |
| NZD | rbnz | `INM.DG102.NZZCF` | percent | daily | level | yes, from the owner's connection | 2026-09-08 | secondary market government bond closing yield, 2 year, from RBNZ table B2 (hb2-daily-close.xlsx), column located by this series ID. Verified by the owner from a residential or mobile connection on 2026-09-15, not from a run: the RBNZ answers every data-centre or cloud egress with HTTP 403 and a JavaScript challenge, so no unattended run can repeat the check or the fetch. The 2-year column is blank for most of 2020, which a backtest over that year must know. |

#### `yield_2y_chg_1m` and `yield_2y_chg_3m`

One-month and three-month trailing changes in the two-year government bond yield, in basis points, with the window ending at the latest session per ADR 0004. Together they carry 0.45 of the monetary pillar, more than the level itself, because the direction of repricing typically leads the level in FX.

Neither is a separately published series. No source in this registry, or found by search, publishes a pre-differenced government bond yield change for any G10 issuer. Both reuse `yield_2y`'s own source, series ID, unit and `last_observed` for every currency, under the registry's `chg_1m` and `chg_3m` transforms, which is why their source table is not repeated here: it is `yield_2y`'s table above, currency for currency, source for source.

**Derived in the collector, not fetched.** Since #322, `fbe.datasources.yield_changes` computes both from the `yield_2y` sessions every source returns, once, so the window is the same whichever source served the level. `fbe.datasources.fred` and `fbe.datasources.curves` still decline the two refs, because a provider's own "change" unit is a different quantity (FRED's `chg` is one day), and the registry keeps them unverified for that reason: no provider serves them. A run asking for either key fetches the level behind it.

The window is ADR 0004's: the later endpoint is a session, the earlier endpoint is the last session on or before the same calendar day one or three months back (the month's last day when that day does not exist), and the value is the difference times 100, in basis points. When the earlier endpoint is more than `registry.YIELD_CHANGE_TOLERANCE_DAYS`, 7, before its target, no value is emitted for that session. The 7 was ruled on #322 from five years of sessions: the longest holiday run in any G10 market was the Japanese New Year at 7 days, and the one longer gap, NZD's 16 days in October 2021, was missing data.

Coverage measured on 2026-10-01: 6 of 8 for both keys. **GBP holds none**, because the Bank of England sheet the level comes from carries the current month only, 22 sessions, and a one-month change needs a session a month back. **CHF holds none**, because its level is typed by hand and ADR 0004 accepts that a hand-typed level rarely has a session within 7 days of a month back.

Pillar: **monetary**. Canonical unit: `basis_points`. Staleness allowance: the level's, since the refs are `yield_2y`'s and a change is as old as the session it ends on. Fresh coverage: 6/8 (both), GBP and CHF missing.

#### `yield_10y`

Ten-year benchmark government bond yield. Registered and verified across all eight, with clean current coverage, and **consumed by no pillar today**.

MONETARY is attributed to it in the registry but does not ask for it: section 3.1 of `docs/scoring-spec.md` and `MonetaryPillar.component_weights` both name five components and none is a 10-year series. Read its 100% coverage as a series held ready, not as a live input, and do not read it as covering the CHF front-end gap above.

It is specifically **not** a fallback for `yield_2y`, and adding it as one would be a substitution error rather than a repair. Section 3.1 values the 2-year because it is the market's expectation of the policy path, already priced. A 10-year yield is dominated by term premium and by long-run growth and inflation expectations, so it answers a different question. Whether MONETARY should ever gain a 10-year term is a scoring decision for `quant-analyst` and is not settled here.

Pillar: **monetary**, unconsumed. Canonical unit: `percent`. Staleness allowance: 75 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DGS10` | percent | daily | level | yes | 2026-09-08 | Treasury constant maturity, daily |
| EUR | oecd | `DSD_STES@DF_FINMARK/DEU.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | 10y Bund, the euro-area benchmark; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| GBP | oecd | `DSD_STES@DF_FINMARK/GBR.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| JPY | oecd | `DSD_STES@DF_FINMARK/JPN.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| CHF | oecd | `DSD_STES@DF_FINMARK/CHE.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| CAD | oecd | `DSD_STES@DF_FINMARK/CAN.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| AUD | oecd | `DSD_STES@DF_FINMARK/AUS.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| NZD | oecd | `DSD_STES@DF_FINMARK/NZL.M.IRLT.PA......` | percent | monthly | level | yes | 2026-08-01 | taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |

#### `cpi_yoy`

Headline consumer price inflation, year on year. The inflation pillar scores the gap to each central bank's target rather than the raw print, so `CurrencyMeta.inflation_target` is the other half of this input. Coverage here was two of eight until the OECD's own API replaced FRED's frozen mirror.

Pillar: **inflation**. Canonical unit: `percent`. Staleness allowance: 200 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `CPIAUCSL` | index | monthly | yoy | yes | 2026-07-01 | CPI-U all items, seasonally adjusted index, from the BLS |
| EUR | fred | `CP0000EZ19M086NEST` | index | monthly | yoy | yes | 2026-07-01 | Eurostat HICP all items, euro area 19, a true bloc aggregate |
| GBP | oecd | `DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._T.N.GY` | percent | monthly | level | yes | 2026-07-01 | national CPI, growth over one year. FRED's mirror froze at 2025-03. |
| JPY | oecd | `DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._T.N.GY` | percent | monthly | level | yes | 2026-07-01 | Japan sits in the COICOP 2018 dataflow, not the 1999 one. FRED's mirror froze at 2021-06, a five-year hole. |
| CHF | oecd | `DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CHE.M.N.CPI.PA._T.N.GY` | percent | monthly | level | yes | 2026-08-01 | COICOP 2018 dataflow. FRED's mirror froze at 2025-04. |
| CAD | oecd | `DSD_PRICES@DF_PRICES_ALL/CAN.M.N.CPI.PA._T.N.GY` | percent | monthly | level | yes | 2026-07-01 | national CPI. FRED's mirror froze at 2025-03. |
| AUD | oecd | `DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._T.N.GY` | percent | quarterly | level | yes | 2026-04-01 | quarterly by publication, not by choice; 2026Q2 |
| NZD | oecd | `DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._T.N.GY` | percent | quarterly | level | yes | 2026-04-01 | quarterly; 2026Q2. FRED's mirror froze at 2023Q3. |

#### `core_cpi_yoy`

Consumer prices excluding food and energy, year on year. Central banks react to this more than to the headline, so it leads policy and therefore leads the currency.

Pillar: **inflation**. Canonical unit: `percent`. Staleness allowance: 200 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `CPILFESL` | index | monthly | yoy | yes | 2026-07-01 | CPI-U less food and energy, seasonally adjusted index |
| EUR | fred | `00XEFDEZ19M086NEST` | index | monthly | yoy | yes | 2026-07-01 | Eurostat HICP excluding energy, food, alcohol and tobacco |
| GBP | oecd | `DSD_PRICES@DF_PRICES_ALL/GBR.M.N.CPI.PA._TXCP01_NRG.N.GY` | percent | monthly | level | yes | 2026-07-01 | all items less food and energy |
| JPY | oecd | `DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/JPN.M.N.CPI.PA._TXCP01_NRG.N.GY` | percent | monthly | level | yes | 2026-07-01 | COICOP 2018 dataflow |
| CHF | oecd | `DSD_PRICES_COICOP2018@DF_PRICES_C2018_N_TXCP01_NRG/CHE.M.N.CPI.PA._TXCP01_NRG.N.GY` | percent | monthly | level | yes | 2026-08-01 | the only Swiss core series found anywhere free: it is absent from both general price dataflows and lives in the dedicated core flow |
| CAD | oecd | `DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL/CAN.M.N.CPI.PA._TXCP01_NRG.N.GY` | percent | monthly | level | yes | 2026-07-01 | COICOP 2018 dataflow; the 1999 flow has no Canadian core |
| AUD | oecd | `DSD_PRICES@DF_PRICES_ALL/AUS.Q.N.CPI.PA._TXCP01_NRG.N.GY` | percent | quarterly | level | yes | 2026-04-01 | 2026Q2. Not the RBA's trimmed mean, which is its preferred cut. |
| NZD | oecd | `DSD_PRICES@DF_PRICES_ALL/NZL.Q.N.CPI.PA._TXCP01_NRG.N.GY` | percent | quarterly | level | yes | 2026-04-01 | 2026Q2. Not the RBNZ sectoral factor model estimate. |

#### `gdp_yoy`

Real GDP growth, year on year. Slow and heavily revised, so it anchors the growth pillar rather than driving it. Full G10 coverage, which is rare enough in this registry to be worth stating.

Pillar: **growth**. Canonical unit: `percent`. Staleness allowance: 270 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `GDPC1` | billions_chained_usd | quarterly | yoy | yes | 2026-04-01 | real GDP, SAAR chained 2017 dollars; 2026Q2 |
| EUR | fred | `CLVMNACSCAB1GQEA19` | millions_chained_eur | quarterly | yoy | yes | 2026-04-01 | Eurostat real GDP, euro area 19, a true bloc aggregate; 2026Q2 |
| GBP | fred | `NGDPRSAXDCGBQ` | millions_chained_gbp | quarterly | yoy | yes | 2026-04-01 | 2026Q2 |
| JPY | fred | `JPNRGDPEXP` | billions_chained_jpy | quarterly | yoy | yes | 2026-04-01 | real GDP by expenditure; 2026Q2 |
| CHF | fred | `CLVMNACSCAB1GQCH` | millions_chained_chf | quarterly | yoy | yes | 2026-04-01 | 2026Q2 |
| CAD | fred | `NGDPRSAXDCCAQ` | millions_chained_cad | quarterly | yoy | yes | 2026-04-01 | 2026Q2 |
| AUD | fred | `NGDPRSAXDCAUQ` | millions_chained_aud | quarterly | yoy | yes | 2026-04-01 | 2026Q2 |
| NZD | fred | `NZLGDPRQPSMEI` | percent | quarterly | level | yes | 2026-01-01 | already published as a year-on-year growth rate, so no transform; 2026Q1 |

#### `unemployment_rate`

Harmonised unemployment rate. Compared cross-sectionally against the rest of the G10 and against its own recent trend, since the level that counts as full employment differs by country.

Pillar: **employment**. Canonical unit: `percent`. Staleness allowance: 270 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `UNRATE` | percent | monthly | level | yes | 2026-08-01 | BLS headline U-3, roughly one month behind |
| EUR | fred | `LRHUTTTTDEM156S` | percent | monthly | level | yes | 2026-06-01 | German harmonised rate. euro-area aggregate stopped updating; German national series used as the euro-area proxy: LRHUTTTTEZM156S stopped at 2023-01. |
| GBP | fred | `LRHUTTTTGBM156S` | percent | monthly | level | yes | 2026-04-01 | - |
| JPY | fred | `LRHUTTTTJPM156S` | percent | monthly | level | yes | 2026-06-01 | - |
| CHF | fred | `LRUN64TTCHQ156S` | percent | quarterly | level | yes | 2026-01-01 | quarterly ILO rate, aged 15-64; Switzerland publishes no monthly harmonised rate on FRED. 2026Q1. |
| CAD | fred | `LRHUTTTTCAM156S` | percent | monthly | level | yes | 2026-07-01 | - |
| AUD | fred | `LRHUTTTTAUM156S` | percent | monthly | level | yes | 2026-06-01 | - |
| NZD | fred | `LRHUTTTTNZQ156S` | percent | quarterly | level | yes | 2026-04-01 | quarterly by publication; 2026Q2 |

#### `employment_chg`

Change in the number of people employed. The flow, not the stock: a falling unemployment rate driven by people leaving the labour force is a different signal from one driven by hiring, and this indicator is what separates them.

Pillar: **employment**. Canonical unit: `persons`. Staleness allowance: 270 days. Fresh coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `PAYEMS` | thousands_of_persons | monthly | diff | yes | 2026-08-01 | total nonfarm payrolls; the differenced level is the NFP headline |
| EUR | eurostat | `namq_10_pe?geo=EA20&unit=THS_PER&na_item=EMP_DC&s_adj=SCA` | thousands_of_persons | quarterly | diff | yes | 2026-04-01 | euro-area total employment, fixed EA20 composition (#354) |
| GBP | fred | `LFEMTTTTGBQ647S` | persons | quarterly | diff | yes | 2026-01-01 | 2026Q1 |
| JPY | fred | `LFEMTTTTJPM647S` | persons | monthly | diff | yes | 2026-06-01 | - |
| CHF | fred | `LFEMTTTTCHQ647S` | persons | quarterly | diff | yes | 2026-01-01 | 2026Q1 |
| CAD | fred | `LFEMTTTTCAM647S` | persons | monthly | diff | yes | 2026-07-01 | - |
| AUD | fred | `LFEMTTTTAUM647S` | persons | monthly | diff | yes | 2026-06-01 | - |
| NZD | fred | `LFEMTTTTNZQ647S` | persons | quarterly | diff | yes | 2026-04-01 | 2026Q2 |

#### `employment_level`

Number of people employed. The stock that `employment_chg` is the flow of, and it exists for one purpose: `employment_trend` is specified as an annualised percent of the employment level, and without a denominator the component would score a raw count. A US payrolls print is in the hundreds of thousands and a New Zealand quarterly change is in the thousands, so a cross-sectional z-score of the count ranks the size of the economies. Section 3.4 of `docs/scoring-spec.md` rejects that explicitly.

Not sourced separately. The eight refs are `employment_chg`'s own, under `level` instead of `diff`; see `_employment_level_series` in the registry. Coverage, freshness and verification are therefore identical to `employment_chg`'s currency by currency, and the two cannot drift apart, which matters because a count divided by a different population's level is a plausible number that is wrong by whatever the two populations differ by.

**EUR has had an entry since #354.** Before it, the euro's flow was a manual figure with no published level behind it, so EUR had no stock and its EMPLOYMENT pillar was absent. A German level was current on FRED and was deliberately not used, because dividing a euro-area flow by one member state's workforce would overstate hiring by roughly a factor of four. Eurostat now publishes the bloc's level, and both keys read it.

USD arrives in `thousands_of_persons` while the canonical unit is `persons`, inherited from `PAYEMS` through `employment_chg`. The only component that reads this key is a ratio of two numbers from that same series, so the scale cancels and the mismatch cannot reach a score. It is recorded because a reader comparing this level with another currency's would otherwise find the United States a thousand times smaller than Japan.

Pillar: **employment**. Canonical unit: `persons`. Staleness allowance: 270 days. Fresh coverage: 88% on 2026-09-09, before #354.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `PAYEMS` | thousands_of_persons | monthly | level | yes | 2026-08-01 | total nonfarm payrolls; the differenced level is the NFP headline; the level `employment_chg` is differenced from |
| EUR | eurostat | `namq_10_pe?geo=EA20&unit=THS_PER&na_item=EMP_DC&s_adj=SCA` | thousands_of_persons | quarterly | level | yes | 2026-04-01 | the level `employment_chg` is differenced from (#354) |
| GBP | fred | `LFEMTTTTGBQ647S` | persons | quarterly | level | yes | 2026-01-01 | 2026Q1; the level `employment_chg` is differenced from |
| JPY | fred | `LFEMTTTTJPM647S` | persons | monthly | level | yes | 2026-06-01 | the level `employment_chg` is differenced from |
| CHF | fred | `LFEMTTTTCHQ647S` | persons | quarterly | level | yes | 2026-01-01 | 2026Q1; the level `employment_chg` is differenced from |
| CAD | fred | `LFEMTTTTCAM647S` | persons | monthly | level | yes | 2026-07-01 | the level `employment_chg` is differenced from |
| AUD | fred | `LFEMTTTTAUM647S` | persons | monthly | level | yes | 2026-06-01 | the level `employment_chg` is differenced from |
| NZD | fred | `LFEMTTTTNZQ647S` | persons | quarterly | level | yes | 2026-04-01 | 2026Q2; the level `employment_chg` is differenced from |

#### `retail_sales_yoy`

Retail trade volume, year on year. The fastest read on household demand, and the growth pillar's main monthly input given that GDP arrives quarterly and late.

Pillar: **growth**. Canonical unit: `percent`. Staleness allowance: 270 days. Fresh coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USASLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | OECD retail volume growth, chosen over the fresher US-only RSAFS so the eight legs are measured the same way |
| EUR | oecd | `DSD_KEI@DF_KEI/EA20.M.TOVM.GR.G47.Y.GY` | percent | monthly | level | yes | 2026-07-01 | euro-area retail trade volume, the bloc itself; OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352) |
| GBP | fred | `GBRSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-06-01 | - |
| JPY | oecd | `DSD_KEI@DF_KEI/JPN.M.TOVM.GR.G47.Y.GY` | percent | monthly | level | yes | 2026-06-01 | OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352), no key needed |
| CHF | fred | `CHESLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| CAD | fred | `CANSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-04-01 | - |
| AUD | fred | `SLRTTO01AUQ659S` | percent | quarterly | level | yes | **2025-04-01** (stale) | DISCONTINUED at 2025Q2. Australia has no live retail series on FRED and none was found on the OECD API either. |
| NZD | oecd | `DSD_KEI@DF_KEI/NZL.Q.TOVM.GR.G47.Y.GY` | percent | quarterly | level | yes | 2026-04-01 | OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352); FRED's mirror lagged 284 days |

#### `indpro_yoy`

Industrial production, year on year. Four of eight were live until #352 read the euro area, Switzerland, Australia and New Zealand from the OECD short-term indicators flow; all eight are now fetched. Switzerland, Australia and New Zealand are quarterly, so their legs move less often than the monthly ones.

Pillar: **growth**. Canonical unit: `percent`. Staleness allowance: 180 days. Fresh coverage: 50%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USAPRINTO01GYSAM` | percent | monthly | level | yes | 2026-06-01 | OECD basis for cross-country comparability; INDPRO is the fresher US-only alternative |
| EUR | oecd | `DSD_KEI@DF_KEI/EA20.M.PRVM.GR.BTE.Y.GY` | percent | monthly | level | yes | 2026-07-01 | euro-area industrial production, the bloc itself, replacing the German proxy that stopped at 2023-12; OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352) |
| GBP | fred | `GBRPRINTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| JPY | fred | `JPNPRINTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| CHF | oecd | `DSD_KEI@DF_KEI/CHE.Q.PRVM.GR.BTE.Y.GY` | percent | quarterly | level | yes | 2026-04-01 | quarterly, as the Federal Statistical Office publishes it; OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352) |
| CAD | fred | `CANPRINTO01GYSAM` | percent | monthly | level | yes | 2026-04-01 | - |
| AUD | oecd | `DSD_KEI@DF_KEI/AUS.Q.PRVM.GR.BTE.Y.GY` | percent | quarterly | level | yes | 2026-04-01 | OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352) |
| NZD | oecd | `DSD_KEI@DF_KEI/NZL.Q.PRVM.GR.BTE.Y.GY` | percent | quarterly | level | yes | 2026-04-01 | OECD key short-term indicators, `DSD_KEI@DF_KEI` (#352) |

#### `pmi_composite`

Composite purchasing managers' index, manufacturing and services blended, 50 being the expansion line. Licensed, with zero free coverage, and since ADR 0005 consumed by no pillar: GROWTH takes `business_confidence_mfg` for its leading component instead, and this key is listed in `UNCONSUMED_INDICATORS` so re-adopting it is a one-line change if a licence is ever bought. Until an operator has a services print to blend in, entering the manufacturing headline alone under this key is a stated approximation: note it as such in the manual entry's `meta` rather than presenting it as the real composite. The free OECD business-confidence series found in "Question 5" of `docs/answers/data.md` is a different, real thing (a percentage balance, not a 50-centred diffusion index) and now lives under its own key, `business_confidence_mfg`, below. The two must not be blended under one key. The allowance is 75 rather than 45 because 45 is the age of a punctual monthly print under first-day period stamping, so the series was expiring on the day it published. 75 is this table's own rule: a month elapsing, the survey's own lag, and one more month before the next print is due.

Pillar: **growth**. Canonical unit: `index`. Staleness allowance: 75 days. Fresh coverage: 0%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| EUR | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| GBP | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| JPY | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| CHF | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| CAD | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| AUD | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| NZD | manual | `pmi_composite` | index | monthly | level | **no** | **unknown** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |

#### `business_confidence_mfg`

OECD composite business confidence for manufacturing, from the Business Tendency Surveys the OECD harmonises out of each country's own national survey. A net percentage: respondents answering positively minus those answering negatively.

**Neutral is zero, not 50.** This is not a purchasing managers' index and must never be blended with `pmi_composite` under one key. A PMI is a diffusion index whose expansion line is 50; a percentage balance sits either side of zero and is routinely negative in a healthy economy. German manufacturing confidence was -11.0 in August 2026 while German manufacturing was not contracting by eleven of anything. Code written against distance from 50 and pointed here would shift every currency in the universe the same way, the cross-sectional z-score would absorb the offset, and the ranking would still come out looking orderly with nothing raising anywhere. That is the reason for a separate key rather than a second source behind the PMI one. See "Question 5" in `docs/answers/data.md` and issue #6.

**GROWTH consumes this at a sub-weight of 0.30.** It fills the growth pillar's leading component, which `pmi_composite` cannot fill because it is licensed and 0/8 on free coverage. The scoring decision was taken in ADR 0005 and #119, so the key is no longer in `UNCONSUMED_INDICATORS` and its coverage figure above is a live input.

**The mixed cadence is a real cost, not a rounding error.** Four currencies survey monthly and four quarterly, because the OECD republishes each country's own survey at the cadence that country runs it: Japan's is the quarterly Tankan, and the Australian and New Zealand series are the equally quarterly NAB and ANZ business outlooks. So half the universe would be compared against a number up to a quarter older than the other half. The component-level freshness discount in `fbe.pillars.base.BasePillar.component_freshness` is what makes that visible rather than silent, and it is a cost ADR 0005 accepted rather than one it removed.

**What it is not.** Each leg is the country's own national survey as the OECD compiles it, which is a family relationship to the ifo, KOF, Tankan, NAB and ANZ headline figures rather than an identity. None was verified to match its national headline number for number. Anything describing this series should say what it is rather than calling it a PMI proxy.

The allowance is 270 and is derived from the quarterly half, which is the binding one. It is this table's own published figure for a quarterly series stamped on its period's first day, and what `gdp_yoy` uses, the other first-day-stamped quarterly input to this pillar. The derivation runs on the real calendar rather than on nominal 90-day quarters: a print is the newest one until its successor publishes, which is one further quarter end plus the survey's own lag. That lag is bounded by observation and not known exactly, because the 2026-Q2 print was still the newest on the verification date, which puts it at no more than 71 days past the quarter end. Taking that bound, the worst case across the four stamp positions is 253 days, reached by a Q3 print. So 270 covers a punctual print in every quarter with no day on which a current series reads stale, while a leg that misses a whole release reaches 253 + 90 and expires. An allowance sized from the monthly half would fail four currencies on day one: the quarterly legs were 161 days old on the verification date while entirely current.

Pillar: **growth** (unconsumed). Canonical unit: `percentage_balance`. Staleness allowance: 270 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | oecd | `DSD_STES@DF_BTS/USA.M.BCICP.PB.C.Y...` | percentage_balance | monthly | level | yes | 2026-08-01 | US manufacturing business tendency survey as the OECD compiles it; not the ISM headline, which is licensed |
| EUR | oecd | `DSD_STES@DF_BTS/DEU.M.BCICP.PB.C.Y...` | percentage_balance | monthly | level | yes | 2026-08-01 | Germany standing in for the euro area, matching the `REF_AREA` convention; same survey family as the ifo, not verified to be the ifo headline number for number |
| GBP | oecd | `DSD_STES@DF_BTS/GBR.M.BCICP.PB.C.Y...` | percentage_balance | monthly | level | yes | 2026-08-01 | UK manufacturing business tendency survey |
| JPY | oecd | `DSD_STES@DF_BTS/JPN.Q.BCICP.PB.C.Y...` | percentage_balance | quarterly | level | yes | 2026-04-01 | quarterly because Japan's survey is the Tankan; 2026-Q2 stamped on its first day per #27 |
| CHF | oecd | `DSD_STES@DF_BTS/CHE.M.BCICP.PB.C.Y...` | percentage_balance | monthly | level | yes | 2026-08-01 | Swiss manufacturing business tendency survey, the KOF family |
| CAD | oecd | `DSD_STES@DF_BTS/CAN.Q.BCICP.PB.C.Y...` | percentage_balance | quarterly | level | yes | 2026-04-01 | quarterly: the Bank of Canada Business Outlook Survey family |
| AUD | oecd | `DSD_STES@DF_BTS/AUS.Q.BCICP.PB.C.Y...` | percentage_balance | quarterly | level | yes | 2026-04-01 | quarterly: the NAB business survey family |
| NZD | oecd | `DSD_STES@DF_BTS/NZL.Q.BCICP.PB.C.Y...` | percentage_balance | quarterly | level | yes | 2026-04-01 | quarterly: the ANZ business outlook family |

#### `trade_balance`

Merchandise trade balance in US dollars, seasonally adjusted. Already currency-converted by the source, so the eight legs are directly comparable without an FX step. Full, current G10 coverage.

Pillar: **external**. Canonical unit: `usd`. Staleness allowance: 150 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `XTNTVA01USM667S` | usd | monthly | level | yes | 2026-06-01 | OECD basis for comparability; BOPGSTB is the fresher US-only goods and services balance |
| EUR | fred | `XTNTVA01DEM667S` | usd | monthly | level | yes | 2026-05-01 | euro-area aggregate stopped updating; German national series used as the euro-area proxy: XTNTVA01EZM667S stopped at 2022-12. Germany runs a structural surplus larger than the bloc's, so this proxy flatters the euro. |
| GBP | fred | `XTNTVA01GBM667S` | usd | monthly | level | yes | 2026-06-01 | - |
| JPY | fred | `XTNTVA01JPM667S` | usd | monthly | level | yes | 2026-06-01 | - |
| CHF | fred | `XTNTVA01CHM667S` | usd | monthly | level | yes | 2026-06-01 | - |
| CAD | fred | `XTNTVA01CAM667S` | usd | monthly | level | yes | 2026-06-01 | - |
| AUD | fred | `XTNTVA01AUM667S` | usd | monthly | level | yes | 2026-06-01 | - |
| NZD | fred | `XTNTVA01NZM667S` | usd | monthly | level | yes | 2026-06-01 | - |

#### `current_account_gdp`

Current account balance as a share of GDP. A structural measure of whether a currency is financed by the world or financing it.

Pillar: **external**. Canonical unit: `percent_of_gdp`. Staleness allowance: 210 days. Fresh coverage: 0%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | oecd | `DSD_BOP@DF_BOP/USA.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |
| EUR | oecd | `DSD_BOP@DF_BOP/EA20.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353); the euro area itself |
| GBP | oecd | `DSD_BOP@DF_BOP/GBR.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |
| JPY | oecd | `DSD_BOP@DF_BOP/JPN.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |
| CHF | oecd | `DSD_BOP@DF_BOP/CHE.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353); swings with gold trade, served unsmoothed |
| CAD | oecd | `DSD_BOP@DF_BOP/CAN.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |
| AUD | oecd | `DSD_BOP@DF_BOP/AUS.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |
| NZD | oecd | `DSD_BOP@DF_BOP/NZL.WXD.CA.B.T.Q.PT_B1GQ.Y` | percent_of_gdp | quarterly | level | yes | 2026-04-01 | OECD balance of payments, current account with the world, share of GDP, seasonally adjusted (#353) |

#### `gdp_nominal_usd`

Nominal gross domestic product at market prices, in actual US dollars, from the World Bank's national accounts through FRED. The denominator `trade_trend` is taken over, and nothing else consumes it.

**Actual dollars, not millions or billions.** `trade_balance` is published on the same scale, so the ratio is taken between two quantities in one unit with no conversion step. A series filed here in national currency, or in millions, would give a plausible number for every currency and raise nothing, because the cross-sectional z-score absorbs a common factor exactly. That is why issue #158 chose this family over a quarterly one.

Annual rather than quarterly, and that was measured rather than preferred. The OECD quarterly family `*GDPNQDSMEI` resolves for all eight, is denominated in national currency, and stopped publishing at 2023-07-01. Converting it would buy an FX step, a date convention and a series three years stale, so there is nothing to convert.

The allowance of 916 days followed the hand-keyed rule from a measured lag rather than an assumed one: the 2025 reference year was published on 2026-07-07, 188 days after the year ended, so the newest print is 916 days old on the day before its successor is due. On a 2026-09 run that leaves `trade_trend` entering at about half of its declared 0.30. Whether ageing a scaling constant like a signal is right at all is argued on #126 and is not settled here.

Pillar: **external**. Canonical unit: `usd`. Staleness allowance: 916 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `MKTGDPUSA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| EUR | fred | `MKTGDPDEA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| GBP | fred | `MKTGDPGBA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| JPY | fred | `MKTGDPJPA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| CHF | fred | `MKTGDPCHA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| CAD | fred | `MKTGDPCAA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| AUD | fred | `MKTGDPAUA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |
| NZD | fred | `MKTGDPNZA646NWDB` | usd | annual | level | yes | 2025-01-01 | - |

#### `cot_net_pct_oi`

Net speculative position in CME currency futures from the CFTC Commitments of Traders report. A crowded position is a reason to fade a fundamental view, not to add to it, so this pillar usually works against the others by design.

`CotSource` emits `(lev_money_positions_long - lev_money_positions_short) / open_interest_all * 100` from the TFF futures-only dataset `gpe5-46if`. Leveraged funds rather than the Legacy report's non-commercial bucket, and a percent rather than the bare ratio, both for the reasons ADR 0011 records. The division and the scale happen here, in the source, because sources emit canonical keys; `fetch_contract` returns the raw Socrata rows and `derive_usd_position` is the only caller that wants them.

The key was renamed from `cot_net_position` when the division was still unimplemented, and the paragraph that used to sit here said so. That gap closed in #174, which is why `Canonical unit` below now reads `percent_of_open_interest`.

**The dollar row below is not fetched.** There is no liquid dollar contract in TFF, so the dollar reading is the sign-flipped net of the seven other legs, each taken as a percent of its own open interest before they are added. A raw sum of contract counts would be a euro reading wearing a dollar label: open interest differs by more than an order of magnitude between EUR and NZD. The ICE contract `098662` stays registered as the cross-check it is described as, and `CotSource` does not query it today.

Pillar: **positioning**. Canonical unit: `percent_of_open_interest`. Staleness allowance: 21 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | cftc | `098662` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The primary dollar read is the sign-flipped complement of the other seven; this contract is a small, thinly held cross-check. |
| EUR | cftc | `099741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | EURO FX, CME, TFF gpe5-46if |
| GBP | cftc | `096742` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | BRITISH POUND, CME |
| JPY | cftc | `097741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | JAPANESE YEN, CME |
| CHF | cftc | `092741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | SWISS FRANC, CME |
| CAD | cftc | `090741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | CANADIAN DOLLAR, CME |
| AUD | cftc | `232741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | AUSTRALIAN DOLLAR, CME |
| NZD | cftc | `112741` | percent_of_open_interest | weekly | net_position | yes | 2026-09-01 | NZ DOLLAR, CME |

#### `equity_index`

Benchmark equity index for each economy. Registered and verified across all eight, with clean current coverage, and **consumed by no pillar today**.

RISK is attributed to it in the registry but does not ask for it: `RiskPillar.requires` names `world_equity_index` and `vol_index`, and neither of those is this key. Read its 100% coverage as a series held ready, not as a live input.

The pillar wants one regime number that all eight currencies share, which the betas in `CurrencyMeta` then re-sign per currency. Eight local indices would give eight regimes and leave the betas with nothing to act on. Six of the eight refs below are monthly OECD share price indices in any case, which cannot date a drawdown. Whether a per-currency equity term belongs in GROWTH, where a local index reads as an earnings and growth signal rather than as a regime one, is a scoring decision for `quant-analyst` and is not settled here.

Pillar: **risk**, unconsumed. Canonical unit: `index`. Staleness allowance: 75 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `SP500` | index | daily | level | yes | 2026-09-08 | daily close; FRED holds a rolling ten-year window only |
| EUR | oecd | `DSD_STES@DF_FINMARK/DEU.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index, monthly average. taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind. Monthly is slow for a risk pillar: prefer the Stooq daily feed where it can be made to work, and keep this as the dependable fallback. |
| GBP | oecd | `DSD_STES@DF_FINMARK/GBR.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| JPY | fred | `NIKKEI225` | index | daily | level | yes | 2026-09-09 | daily close |
| CHF | oecd | `DSD_STES@DF_FINMARK/CHE.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| CAD | oecd | `DSD_STES@DF_FINMARK/CAN.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| AUD | oecd | `DSD_STES@DF_FINMARK/AUS.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |
| NZD | oecd | `DSD_STES@DF_FINMARK/NZL.M.SHARE.IX......` | index | monthly | level | yes | 2026-08-01 | OECD share price index; taken from the OECD API rather than FRED's mirror of the same OECD material, which runs two months behind |

#### `world_equity_index`

A single global equity benchmark, currently the S&P 500, and the equity half of the risk regime. A single global number, not a per-currency one: it sets the regime, and the currencies then sort themselves by `CurrencyMeta.risk_beta`.

**A United States index is standing in for the world here, and the substitution is the risk pillar's largest known weakness.** What the pillar therefore measures is US risk appetite, so a European or Japanese shock registers only once it crosses the Atlantic. The stand-in is recorded at the point of use rather than left implicit in a ticker, because a reader who does not recognise `SP500` would otherwise take a global reading at face value. Replace the ref with a genuine free daily world index when one can be verified; nothing else has to change, which is the point of naming the key for what it measures.

Separate from `equity_index` above, and deliberately so. A key whose refs are GLOBAL is a global reading and a key whose refs are per-currency is a per-currency reading; one key never holds both. Putting a GLOBAL ref on `equity_index` instead would have silently changed what `coverage_report()` and `identifier_coverage()` describe, because both short-circuit on a GLOBAL ref and report the key on that ref alone, so the eight would have stopped being counted with nothing raising.

The 7-day allowance is `vol_index`'s, and for the same reason: both are daily closes of the same market, so a gap wider than a long weekend means the feed has stopped rather than that the exchange was shut.

Pillar: **risk**. Canonical unit: `index`. Staleness allowance: 7 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GLOBAL | fred | `SP500` | index | daily | level | yes | 2026-09-08 | daily close; FRED holds a rolling ten-year window only. A US index used as the world proxy, per this key's description |

#### `vol_index`

A global volatility benchmark, currently the CBOE's implied volatility on the S&P 500 (VIX). A single global number, not a per-currency one: it sets the risk regime, and the currencies then sort themselves by `CurrencyMeta.risk_beta`. Named for what it measures rather than for the vendor's ticker, so a future addition or replacement stays under this same key.

Pillar: **risk**. Canonical unit: `index`. Staleness allowance: 7 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GLOBAL | fred | `VIXCLS` | index | daily | level | yes | 2026-09-08 | daily close |

#### `commodity_price`

Terms-of-trade proxy for the commodity currencies, plus a global benchmark. Only the three currencies with a `commodity_link` in `CurrencyMeta` carry a specific ref; the others take the global index or nothing.

Pillar: **external**. Canonical unit: `index`. Staleness allowance: 90 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GLOBAL | fred | `PALLFNFINDEXM` | index | monthly | level | yes | 2026-07-01 | IMF all commodity price index, 2016=100 |
| CAD | fred | `DCOILWTICO` | usd_per_barrel | daily | level | yes | 2026-09-01 | WTI spot, the standard Canadian dollar terms-of-trade proxy |
| AUD | fred | `PIORECRUSDM` | index | monthly | level | yes | 2026-07-01 | IMF iron ore price index |
| NZD | manual | `commodity_price` | index | irregular | level | **no** | **unknown** | no dairy price index on FRED, verified by search. The GlobalDairyTrade auction index is the right series and is published fortnightly on globaldairytrade.info. PFOODINDEXM is a poor but free substitute. |
---

## Refresh cadence

What is worth refetching, and how often.

| Data | Source | Real cadence | Fetch |
| --- | --- | --- | --- |
| 2y yields | central banks | daily | daily, before the session |
| Spot FX, VIX, equity closes | FRED, Stooq | daily | daily |
| Policy rates, 10y yields, equity indices | OECD | monthly, ~1 month behind | weekly |
| CPI, core CPI | OECD, FRED | monthly or quarterly | weekly |
| Unemployment, retail sales, IP, trade balance | FRED | monthly | weekly |
| Euro-area employment | Eurostat | quarterly | weekly |
| GDP | FRED | quarterly | weekly |
| COT positioning | CFTC | weekly, Friday 15:30 ET | Saturday, or Monday morning |
| Economic calendar | Forex Factory | weekly, current week only | Monday, then cached all week |
| PMIs, manual entries (optional, read by no pillar) | you | monthly | first business day of the month |

Note the split in the first two rows. The 2-year yields are the only macro
input that genuinely needs a daily fetch, because they are the only daily macro
series in the model and the pillar they feed is the heaviest one. Everything
else in the monetary pillar is monthly.

The engine is built for a daily morning run, matching the plan's daily routine.
Nothing here needs intraday polling, and every source in the stack punishes it.
---

## Troubleshooting

**"FRED source unavailable"**
`FRED_API_KEY` is not in the environment. Check with `echo $FRED_API_KEY` in
bash or `$env:FRED_API_KEY` in PowerShell. Get one at
<https://fredaccount.stlouisfed.org/apikeys>. Or run with `offline: true` if
there is a warm cache.

**The key is in `.env` but the source is still unavailable**
Nothing loads `.env`. The config reads the environment only, so export the key
in the shell that runs the engine. See "Getting a key" above.

**FRED returns 400 "Variable api_key is not set"**
The key was not attached to the request. Note that this is exactly the response
a valid endpoint gives an unauthenticated caller, which is how the endpoints in
this document were verified in the first place.

**FRED returns 429**
Rate limited. The client sits at 60 requests per minute against a commonly cited
ceiling of 120, so a 429 usually means something else on the machine shares the
key. Back off and retry; the client already does, three attempts with doubling
backoff.

**A FRED series returns data but the numbers look years old**
The signature failure of this whole project. Almost certainly one of the
discontinued OECD mirrors. Check the `last_observed` on its `SeriesRef`, then
confirm live with `FredSource.last_updated`. If the OECD publishes the same
series itself, move the ref to `OecdSource`, which is what closed the inflation
gap. Do not work around it by overstating the leg's publication lag: that converts a
visible gap into an invisible one, which is worse than the original problem.

**A series returns `.` for some periods**
That is FRED's null, not a zero. Drop those rows. If they are reaching a pillar,
the parser is wrong.

**Stooq returns HTML instead of CSV**
The anti-bot challenge. It comes back as HTTP 200, so a status check will not
catch it. Reject any body whose first line is not
`Date,Open,High,Low,Close,Volume`. There is no fix from an automated client;
fall back to the FRED equity refs, which are monthly.

**Calendar returns "Request Denied"**
The export rate limit. Stop refetching, use the cached copy, and check nothing
is polling on a timer. Treat this as an error, never as an empty week: an empty
calendar clears every blackout. `calendar_guard.is_blacked_out` reports this as
unknown, not clear: `CalendarCoverage.fetch_ok = False`, and if a cached week is
available underneath it, `CoverageGap.STALE_CACHE`; if there is nothing cached
at all, `CoverageGap.FETCH_FAILED`. `apply_filters` records `event:unknown`
naming the failure and leaves the pair tradeable, per issue #43. It does not
block on its own; whether it should is open on issue #24.

**Calendar shows nothing for Monday on a Friday run**
Expected. The feed covers the current week only and there is no next-week feed.
Fetch early in the week. This is `CoverageGap.BEYOND_HORIZON`, not a failure:
the fetch succeeded and `CalendarCoverage.covers_through` simply ends before
Monday. `is_blacked_out` reports unknown for a Monday query made from this
data, the same as a failed fetch would, because a pair being genuinely clear
and a pair nobody could check for must not read the same on the page. The
difference is in the reason string, not in whether the pair is marked
tradeable.

**COT data is over a week old**
Expected and structural. Positions are snapped Tuesday, or Monday when the
Tuesday is a US holiday, and published Friday 15:30 ET. If it is more than two
weeks old, check `CotSource.latest_report_date`; publication has been
interrupted before.

**cftc failed with a report date "rather than a Tuesday"**
The source refuses a report date on any weekday but Monday or Tuesday, because
the CFTC has never snapped on another day and a new weekday means the date
column has been renamed or misread. A Monday is accepted and is a holiday
week, not a fault. Check the dataset's `report_date_as_yyyy_mm_dd` column by
hand before changing the accepted weekdays.

**A currency scores with low coverage**
`CurrencyScore.coverage` below 1.0 means part of the pillar weight had no usable
data. Run `registry.stale_refs()` for the actionable list. If it is CHF, it
is almost certainly the missing 2-year yield taking the monetary pillar with
it; see "The gaps that remain" above. If it is NZD, the engine is probably
running from a network the RBNZ blocks; see the RBNZ entry under "Central
banks and debt offices". It is not PMI: no pillar reads `pmi_composite`, so
a missing PMI cannot lower coverage.

**`coverage_report()` and `identifier_coverage()` disagree**
That is them working. `identifier_coverage()` at 1.0 with `coverage_report()` at
0.0 means every identifier is correct and the source stopped publishing, which
is a data problem to solve at the source. Both at 0.0 means the registry has no
source at all, which is a wiring problem. `current_account_gdp` was the standing
example of the first until #353 moved it to the OECD.

**A currency's monetary pillar is missing entirely**
Check `yield_2y` first. The monetary pillar draws most of its sub-weight from
the 2-year, so a currency without one falls below the component floor and loses
the whole pillar rather than degrading gracefully. NZD is in this state
whenever the engine runs from a data-centre or cloud network, because the RBNZ
blocks those. Everything else has a fetched 2-year; CHF's has come from the SNB
since #351.

**A central bank source returns nothing**
Run `CurvesSource.provider_health()` before touching parsing code. It reports
each provider's newest observation and separates "we broke it" from "they
stopped publishing". The Swiss franc's front end disappeared in 2025 because
the SNB stopped that cube, and no amount of debugging the client would have
found that; the fix was a different cube (#351).

**OECD API returns prose instead of data**
HTTP 429, the throttle. The body begins "You have exceeded the number of
requests". Narrow the query to the exact key and period rather than pulling a
whole dataflow, and slow down. A parser that does not check for this reads a
throttle as zero observations and reports a live currency as uncovered.

**OECD API returns `NoRecordsFound`**
Either the key has too many segments, or the currency is in the other price
dataflow. Check the segment count against `oecd.DIMENSIONS`, then check
`oecd.CPI_FLOW`: Japan and Switzerland are in the COICOP 2018 flow and the rest
are in COICOP 1999. Too few segments would have given a helpful 422 instead.

**An offline run raises on a missing cache entry**
Correct behaviour. Offline means the cache is the source of truth, and a missing
entry is a misconfiguration, not an empty result. Run once online to warm the
cache, then switch back.

**Two runs on the same day give different biases**
Either offline is off and a source revised something between runs, or the config
changed. `BiasReport.config_digest` tells you which. If the digest matches and
the bias moved, a data source revised a print, which is exactly what ALFRED
exists to make visible.
