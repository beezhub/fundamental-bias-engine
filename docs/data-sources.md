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

## The seven sources

| Source | Backs | Key | Cost | Cadence |
| --- | --- | --- | --- | --- |
| FRED | MONETARY, GROWTH, EMPLOYMENT, EXTERNAL, US and euro-area INFLATION | yes, free | free | daily to quarterly by series |
| OECD SDMX | INFLATION for six currencies, plus rates and equity indices | no | free | monthly, ~1 month behind |
| Central banks and debt offices | the 2-year yields at the heart of MONETARY | no | free | daily |
| CFTC COT | POSITIONING | no | free | weekly, Friday 15:30 ET, 3-day lag |
| Stooq | RISK price proxies, daily | no | free | daily |
| Forex Factory | news blackout, no pillar | no | free | weekly, current week only |
| Manual | everything the others cannot supply | no | your time | when you type it |

Only FRED needs a credential. Everything else is open, which also means anyone
reading this can check any claim in it without asking for access first.
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
there is no single API: six providers, six formats, six failure modes. Each
covers exactly one currency, so none can substitute for another and losing one
provider means losing a currency's heaviest pillar rather than degrading a
series. Fail loudly.

### The providers

| Currency | Provider | Identifier | Format | Status |
| --- | --- | --- | --- | --- |
| USD | FRED | `DGS2` | JSON | current |
| EUR | ECB Data Portal | `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` | SDMX / CSV | current |
| GBP | Bank of England | yield curve archive, 2.0y column | ZIP of XLSX | current |
| JPY | Japan Ministry of Finance | `2Y` column | CSV, Shift-JIS | current |
| CAD | Bank of Canada Valet | `BD.CDN.2YR.DQ.YLD` | JSON / CSV | current |
| AUD | Reserve Bank of Australia | `FCMYGBAG2D`, table F2 | CSV | current |
| CHF | Swiss National Bank | cube `rendoblid`, tenor `2J` | CSV | **frozen** |
| NZD | Reserve Bank of New Zealand | table B2 | XLSX | **blocked** |

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

**Bank of England.** Two different things, do not confuse them.

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

**Swiss National Bank: verified endpoint, frozen data.**
`https://data.snb.ch/api/cube/rendoblid/data/csv/en`, semicolon delimited, two
metadata lines before the header, maturity in dimension `D0` where `2J` is the
2-year point. The cube's own `PublishingDate` is 2025-09-01 and its last
observation is 2025-07-31, while other cubes on the same portal are current to
2026-09-01. This is not an outage on our side and not a URL error: the SNB
stopped publishing this particular series. The Swiss franc has no free current
2-year yield.

**Reserve Bank of New Zealand: not reachable.** `rbnz.govt.nz`,
`nzdmo.govt.nz` and `debtmanagement.treasury.govt.nz` all answer automated
requests with HTTP 403, with and without a browser user agent, and the RBNZ
serves its own "Website unavailable" page rather than a proxy error. The RBNZ
does publish 2-year government bond yields in statistical table B2, so the data
exists; it could not be retrieved. This may be an environment block rather than
a policy one, so it is **worth one attempt from another network** before
accepting the manual route permanently.

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

So on any Wednesday the freshest number is eight days old, and by the next
Friday morning it is ten. Nothing fixes that. What matters is that the engine
never treats a COT reading as current: the `Observation` period is the Tuesday,
`released_at` is the Friday, and the staleness penalty sees the real age.

### Deriving a dollar position

There is no liquid CME dollar contract in TFF. The ICE Dollar Index carries
roughly 50,000 contracts of open interest against the euro contract's 800,000,
so it is a cross-check, not a signal.

The dollar read comes from the complement. Every one of the seven contracts is
long the foreign currency and therefore short dollars. Net the seven, flip the
sign, and that is the implied speculative dollar position. The legs must be
normalised first: raw contract counts are not comparable across contracts with
different notionals, and open interest differs by more than an order of
magnitude between EUR and NZD. Normalise each leg by its own open interest or by
its own multi-year percentile before summing. That work belongs to the scoring
layer; the source returns raw counts.

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

A week carries 80 to 100 rows across all currencies.

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
- `period` is the period the number describes, not the day you typed it. Use the
  first of the month for monthly series, matching FRED, so manual and fetched
  observations sort together.
- `unit` and `frequency` should match the registry's `SeriesRef`. A mismatch is
  refused rather than coerced: a PMI entered as a percentage instead of an index
  will score, and score wrongly.
- `released_at` is optional but strongly wanted. Without it, staleness falls
  back to `period`, which overstates the age of a quarterly series by up to
  three months.
- `meta` is the right place for the URL the number came from. That provenance is
  the only audit trail a hand-typed number has.

### Suggested files

| File | Contents |
| --- | --- |
| `pmi.yaml` | Manufacturing and services PMIs for all eight. The largest manual burden, and a recurring monthly one. |
| `yields.yaml` | 2y government yields for CHF and NZD only. The other six are fetched. |
| `guidance.yaml` | Central bank guidance tone per currency, `-1..+1`, dovish to hawkish. |
| `overrides.yaml` | Ad-hoc corrections and the one-off gaps: AUD retail sales, EUR employment change, NZD dairy. Read last, so it wins. |

An `inflation.yaml` used to be needed for six currencies. It no longer is: the
OECD API supplies headline and core CPI for all eight. If you have one from an
earlier run, delete it rather than leaving it to override live data.

`cb_guidance_tone` is not in the registry, because it maps to no external
series. It is a judgement. Record it as one, with the meeting date and a
sentence of reasoning in `meta`.
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

`registry.stale_refs()` gives the actionable form: which currencies are
unusable, per indicator.

### Staleness allowances

`max_staleness_days` lives on each `IndicatorSpec` because the registry is
where the release calendar is known. A 2-year yield stale by a week means the
feed broke; a quarterly balance-of-payments figure is routinely five months old
on the day it is most current.

Every allowance is derived from the publication cadence, never from what the
data happens to need. The rule: the age of the newest print on the day before
the next one is due, which is the period length, plus the statistics office's
lag, plus one more period. A quarterly series stamped at the start of its
quarter therefore earns about 270 days, a lagging monthly series about 180, a
daily market rate about 10.

Setting an allowance higher than the cadence justifies, so a frozen series
passes, converts a visible gap into an invisible one. It is the single easiest
way to make this whole registry lie, and `current_account_gdp` below is the case
where it would have been tempting.

### Position as at 2026-09-09

| Indicator | Fresh | Identifiers | Verdict |
| --- | --- | --- | --- |
| `policy_rate` | 8/8 | 8/8 | good |
| `yield_2y` | 6/8 | 6/8 | CHF and NZD manual, see below |
| `yield_2y_chg_1m` | 6/8 | 6/8 | derived from `yield_2y`, not separately published; same 6/8 |
| `yield_2y_chg_3m` | 6/8 | 6/8 | derived from `yield_2y`, not separately published; same 6/8 |
| `yield_10y` | 8/8 | 8/8 | good |
| `cpi_yoy` | 8/8 | 8/8 | good, was 2/8 before the OECD API |
| `core_cpi_yoy` | 8/8 | 8/8 | good, was 2/8 |
| `gdp_yoy` | 8/8 | 8/8 | good |
| `unemployment_rate` | 8/8 | 8/8 | good |
| `employment_chg` | 7/8 | 7/8 | EUR manual |
| `retail_sales_yoy` | 7/8 | 8/8 | AUD frozen at 2025Q2 |
| `indpro_yoy` | 4/8 | 5/8 | worst of the growth inputs |
| `pmi_composite` | 0/8 | 0/8 | licensed, entirely manual |
| `trade_balance` | 8/8 | 8/8 | good |
| `current_account_gdp` | 0/8 | 8/8 | all eight frozen at 2024Q4 |
| `cot_net_pct_oi` | 8/8 | 8/8 | good, 8-10 days stale by design |
| `equity_index` | 8/8 | 8/8 | USD and JPY daily, rest monthly |
| `vol_index` | global | global | good |
| `commodity_price` | global + CAD, AUD | | NZD dairy manual |

### The gaps that remain, and what each costs

**1. CHF and NZD two-year yields.** The two that could not be closed.

The Swiss franc's is a genuine discontinuation: the SNB publishes a
Confederation spot curve, the endpoint is verified, and the cube stopped at
2025-07-31 while the rest of the SNB portal stayed current. The New Zealand
dollar's is an access problem, not an availability one: the RBNZ publishes the
number in table B2 and refuses automated requests with HTTP 403 across every
domain it owns.

The cost is specific and it is the largest single risk in the data layer. The
monetary pillar draws most of its sub-weight from the 2-year, and monetary is
the heaviest pillar in the composite. Without it, those two currencies fail the
pillar's component floor, lose the pillar outright, and take a conviction
demotion for the reduced coverage. In practice that means CHF and NZD scores
are built from policy rate and 10-year yield alone on the monetary side, which
is a slower and less policy-sensitive read than the other six get.

Three options, in order of preference: retry the RBNZ from another network,
since a 403 from one runner is not proof of a policy; enter both by hand in
`data/manual/yields.yaml`, which is two numbers a day; or accept the reduced
coverage and let the conviction demotion do its job, which is at least honest.

**2. PMIs, all eight.** S&P Global and ISM license these and no free API
carries them. Permanently manual unless a licence is bought. This is the one
gap where the manual burden is a recurring monthly chore rather than a one-off.

**3. Current account, all eight.** Every leg of the FRED `B6BLTT02` family
stopped at 2024Q4 and no free replacement was found; the OECD API's balance of
payments dataflows cover trade in services and merchandise, not the quarterly
current account balance.

The allowance is set to 210 days, which is what a quarterly
balance-of-payments release honestly justifies. It is deliberately not set high
enough to let two-year-old data through. The indicator therefore reports zero
coverage, correctly, and the external pillar leans on `trade_balance`, which is
current for all eight. Finding a live source for this is a good follow-up; the
IMF and national central banks both publish it.

**4. Industrial production** for CHF, AUD and NZD, and the euro-area proxy,
which stopped at 2023-12. Four of eight live. Weight it accordingly, or the
growth pillar scores the countries that happen to publish rather than the
countries that happen to be growing.

**5. AUD retail sales**, frozen at 2025Q2. Australia has no live retail series
on FRED and none was found on the OECD API either.

**6. EUR employment change.** No live euro-area or German employment level; the
FRED series stopped at 2022-10.

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

Affected: `unemployment_rate`, `retail_sales_yoy`,
`indpro_yoy`, `trade_balance`, `current_account_gdp`,
`equity_index`, and `yield_10y` where the Bund stands in for the euro curve.

### Manual fallback per gap

| Gap | File | Where the number comes from |
| --- | --- | --- |
| 2y yields, CHF and NZD | `yields.yaml` | SNB and RBNZ publications, or a broker terminal |
| PMIs, all eight | `pmi.yaml` | S&P Global releases, ISM for the US |
| Retail sales AUD | `overrides.yaml` | ABS monthly retail turnover |
| Employment change EUR | `overrides.yaml` | Eurostat quarterly employment release |
| Industrial production CHF, AUD, NZD | none needed | drop the indicator for these; do not fake it |
| Current account, all eight | none for now | leave the gap visible; find a live source |
| NZD commodity link | `overrides.yaml` | GlobalDairyTrade index, fortnightly, globaldairytrade.info |
| Guidance tone, all eight | `guidance.yaml` | your own reading of the last statement |

### For the integrator

Two things outside this package need attention.

**`openpyxl` is not a project dependency.** The GBP 2-year yield lives in the
Bank of England's yield curve archive, which is a ZIP of XLSX files and the
only source in the registry that needs a spreadsheet reader. `pyproject.toml`
is not owned by the data layer, so the dependency has not been added. Until it
is, `CurvesSource.fetch_boe_curve` cannot run and the pound falls back to the
same reduced monetary pillar as CHF and NZD. The workbook was parsed directly
from its XML during verification, so a reader is a convenience rather than a
strict requirement, but it is the sane way to do it.

**Sub-weight floors should account for the 2-year gap.** `MIN_COMPONENT_WEIGHT`
scores a pillar missing when a currency holds at or below half its sub-weight.
CHF and NZD hold 0.30 of the monetary pillar without a 2-year. Whether that
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
| `yield_2y` | monetary | `percent` | daily | 10d | 75% | 75% |
| `yield_2y_chg_1m` | monetary | `basis_points` | daily | 10d | 75% | 75% |
| `yield_2y_chg_3m` | monetary | `basis_points` | daily | 10d | 75% | 75% |
| `yield_10y` | monetary | `percent` | monthly | 75d | 100% | 100% |
| `cpi_yoy` | inflation | `percent` | monthly | 200d | 100% | 100% |
| `core_cpi_yoy` | inflation | `percent` | monthly | 200d | 100% | 100% |
| `gdp_yoy` | growth | `percent` | quarterly | 270d | 100% | 100% |
| `unemployment_rate` | employment | `percent` | monthly | 270d | 100% | 100% |
| `employment_chg` | employment | `persons` | monthly | 270d | 88% | 88% |
| `retail_sales_yoy` | growth | `percent` | monthly | 270d | 88% | 100% |
| `indpro_yoy` | growth | `percent` | monthly | 180d | 50% | 62% |
| `pmi_composite` | growth | `index` | monthly | 45d | 0% | 0% |
| `trade_balance` | external | `usd` | monthly | 150d | 100% | 100% |
| `current_account_gdp` | external | `percent_of_gdp` | quarterly | 210d | 0% | 100% |
| `cot_net_pct_oi` | positioning | `contracts` | weekly | 21d | 100% | 100% |
| `equity_index` | risk | `index` | daily | 75d | 100% | 100% |
| `vol_index` | risk | `index` | daily | 7d | 100% | 100% |
| `commodity_price` | external | `index` | monthly | 90d | 100% | 100% |

`Allowance` is `max_staleness_days`. `Fresh` is `coverage_report()`,
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

Pillar: **monetary**. Canonical unit: `percent`. Staleness allowance: 10 days. Fresh coverage: 75%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DGS2` | percent | daily | level | yes | 2026-09-08 | Treasury constant maturity |
| EUR | ecb | `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` | percent | daily | level | yes | 2026-09-08 | euro-area AAA government spot curve, 2-year point, Svensson fit. AAA issuers only, so this is close to the Bund and does not carry periphery spread. |
| GBP | boe | `GLC_NOMINAL_SPOT_SHORT/2.0` | percent | daily | level | yes | 2026-09-08 | UK nominal spot curve, 2-year point, from the yield curve archive. Read from sheet '3. spot, short end', the column whose header maturity is 2.0 years. Needs a spreadsheet reader, unlike every other source here. |
| JPY | mof_jp | `2Y` | percent | daily | level | yes | 2026-09-08 | JGB par yield, 2-year column of the Ministry of Finance CSV. The current-month file and the full history are separate downloads; both are needed. |
| CHF | manual | `yield_2y` | percent | daily | level | **no** | **unknown** | the SNB publishes a Confederation spot curve and the endpoint is verified (cube 'rendoblid', dimension '2J'), but it stopped at 2025-07-31 while the rest of the SNB portal stayed current. No free replacement found. Enter by hand or accept that the Swiss franc runs the monetary pillar without a front end. |
| CAD | boc | `BD.CDN.2YR.DQ.YLD` | percent | daily | level | yes | 2026-09-08 | Government of Canada 2-year benchmark bond yield |
| AUD | rba | `FCMYGBAG2D` | percent | daily | level | yes | 2026-09-02 | Australian Government 2-year bond, interpolated, from RBA statistical table F2 |
| NZD | manual | `yield_2y` | percent | daily | level | **no** | **unknown** | the RBNZ publishes 2-year government bond yields in table B2, but rbnz.govt.nz, nzdmo.govt.nz and debtmanagement.treasury.govt.nz all refuse automated requests with HTTP 403. Not verified, not wired in. This may be an environment block rather than a policy one, so it is worth retrying from another network before accepting the manual route. |

#### `yield_2y_chg_1m` and `yield_2y_chg_3m`

One-month and three-month changes in the two-year government bond yield, in basis points, resampled to month-end or quarter-end before differencing. Together they carry 0.45 of the monetary pillar, more than the level itself, because the direction of repricing typically leads the level in FX.

Neither is a separately published series. No source in this registry, or found by search, publishes a pre-differenced government bond yield change for any G10 issuer. Both reuse `yield_2y`'s own source, series ID, unit and verification status for every currency, under the registry's `chg_1m` and `chg_3m` transforms, which is why their source table is not repeated here: it is `yield_2y`'s table above, currency for currency, source for source. Coverage, freshness and the CHF/NZD manual gap are therefore identical to `yield_2y`'s.

Pillar: **monetary**. Canonical unit: `basis_points`. Staleness allowance: 10 days. Fresh coverage: 75% (both).

#### `yield_10y`

Ten-year benchmark government bond yield. Slower than the 2y and less directly tied to policy, but it has clean, current coverage across all eight, so it carries the monetary pillar for the two currencies whose front end is missing.

Pillar: **monetary**. Canonical unit: `percent`. Staleness allowance: 75 days. Fresh coverage: 100%.

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
| EUR | manual | `employment_chg` | persons | quarterly | level | **no** | **unknown** | no live euro-area or German employment level on FRED (LFEMTTTTEZQ647S stopped at 2022-10); take the Eurostat quarterly employment release by hand |
| GBP | fred | `LFEMTTTTGBQ647S` | persons | quarterly | diff | yes | 2026-01-01 | 2026Q1 |
| JPY | fred | `LFEMTTTTJPM647S` | persons | monthly | diff | yes | 2026-06-01 | - |
| CHF | fred | `LFEMTTTTCHQ647S` | persons | quarterly | diff | yes | 2026-01-01 | 2026Q1 |
| CAD | fred | `LFEMTTTTCAM647S` | persons | monthly | diff | yes | 2026-07-01 | - |
| AUD | fred | `LFEMTTTTAUM647S` | persons | monthly | diff | yes | 2026-06-01 | - |
| NZD | fred | `LFEMTTTTNZQ647S` | persons | quarterly | diff | yes | 2026-04-01 | 2026Q2 |

#### `retail_sales_yoy`

Retail trade volume, year on year. The fastest read on household demand, and the growth pillar's main monthly input given that GDP arrives quarterly and late.

Pillar: **growth**. Canonical unit: `percent`. Staleness allowance: 270 days. Fresh coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USASLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | OECD retail volume growth, chosen over the fresher US-only RSAFS so the eight legs are measured the same way |
| EUR | fred | `DEUSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | euro-area aggregate stopped updating; German national series used as the euro-area proxy: EA19SLRTTO01GYSAM stopped at 2023-10. |
| GBP | fred | `GBRSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-06-01 | - |
| JPY | fred | `JPNSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| CHF | fred | `CHESLRTTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| CAD | fred | `CANSLRTTO01GYSAM` | percent | monthly | level | yes | 2026-04-01 | - |
| AUD | fred | `SLRTTO01AUQ659S` | percent | quarterly | level | yes | **2025-04-01** (stale) | DISCONTINUED at 2025Q2. Australia has no live retail series on FRED and none was found on the OECD API either. |
| NZD | fred | `SLRTTO01NZQ659S` | percent | quarterly | level | yes | 2026-01-01 | 2026Q1 |

#### `indpro_yoy`

Industrial production, year on year. Coverage here is the worst of the growth inputs: four of eight are live. Weight it accordingly, or the growth pillar ends up scoring the countries that happen to publish rather than the countries that happen to be growing.

Pillar: **growth**. Canonical unit: `percent`. Staleness allowance: 180 days. Fresh coverage: 50%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USAPRINTO01GYSAM` | percent | monthly | level | yes | 2026-06-01 | OECD basis for cross-country comparability; INDPRO is the fresher US-only alternative |
| EUR | fred | `DEUPRINTO01GYSAM` | percent | monthly | level | yes | **2023-12-01** (stale) | DISCONTINUED at 2023-12. euro-area aggregate stopped updating; German national series used as the euro-area proxy, and the German proxy has now stopped too. |
| GBP | fred | `GBRPRINTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| JPY | fred | `JPNPRINTO01GYSAM` | percent | monthly | level | yes | 2026-05-01 | - |
| CHF | manual | `indpro_yoy` | percent | quarterly | level | **no** | **unknown** | no Swiss industrial production series on FRED in any live form |
| CAD | fred | `CANPRINTO01GYSAM` | percent | monthly | level | yes | 2026-04-01 | - |
| AUD | manual | `indpro_yoy` | percent | quarterly | level | **no** | **unknown** | no Australian industrial production series on FRED |
| NZD | manual | `indpro_yoy` | percent | quarterly | level | **no** | **unknown** | no New Zealand industrial production series on FRED |

#### `pmi_composite`

Composite purchasing managers' index, manufacturing and services blended, 50 being the expansion line. The best leading indicator in the growth pillar and the one with zero free coverage, which is why the manual source exists at all. Until an operator has a services print to blend in, entering the manufacturing headline alone under this key is a stated approximation: note it as such in the manual entry's `meta` rather than presenting it as the real composite. See "Question 5" in `docs/answers/data.md` for a free OECD business-confidence proxy that is a different, real thing (a percentage balance, not a 50-centred diffusion index) and belongs under its own key rather than this one.

Pillar: **growth**. Canonical unit: `index`. Staleness allowance: 45 days. Fresh coverage: 0%.

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
| USD | fred | `USAB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| EUR | fred | `DEUB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| GBP | fred | `GBRB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| JPY | fred | `JPNB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| CHF | fred | `CHEB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| CAD | fred | `CANB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| AUD | fred | `AUSB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |
| NZD | fred | `NZLB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | **2024-10-01** (stale) | DISCONTINUED at 2024Q4, as is every leg of this family. No free replacement was found: the OECD API's balance of payments dataflows cover trade in services and merchandise, not the quarterly current account balance. |

#### `cot_net_pct_oi`

Net speculative position in CME currency futures from the CFTC Commitments of Traders report. A crowded position is a reason to fade a fundamental view, not to add to it, so this pillar usually works against the others by design.

This key was renamed from `cot_net_position` to match `fbe.pillars.positioning.PositioningPillar` and `docs/scoring-spec.md` section 3.6, both of which want net non-commercial positioning as a share of open interest, `(long - short) / open_interest`, not a raw contract count. The rename is not a fix for that gap: the division by open interest is not implemented anywhere yet, so a fetch under this key today still returns raw contracts, and `Canonical unit` below still says so honestly. Do not treat the identifier resolving as evidence the percentage is being computed.

Pillar: **positioning**. Canonical unit: `contracts`. Staleness allowance: 21 days. Fresh coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Last obs | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | cftc | `098662` | contracts | weekly | net_position | yes | 2026-09-01 | USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The primary dollar read is the sign-flipped complement of the other seven; this contract is a small, thinly held cross-check. |
| EUR | cftc | `099741` | contracts | weekly | net_position | yes | 2026-09-01 | EURO FX, CME, TFF gpe5-46if |
| GBP | cftc | `096742` | contracts | weekly | net_position | yes | 2026-09-01 | BRITISH POUND, CME |
| JPY | cftc | `097741` | contracts | weekly | net_position | yes | 2026-09-01 | JAPANESE YEN, CME |
| CHF | cftc | `092741` | contracts | weekly | net_position | yes | 2026-09-01 | SWISS FRANC, CME |
| CAD | cftc | `090741` | contracts | weekly | net_position | yes | 2026-09-01 | CANADIAN DOLLAR, CME |
| AUD | cftc | `232741` | contracts | weekly | net_position | yes | 2026-09-01 | AUSTRALIAN DOLLAR, CME |
| NZD | cftc | `112741` | contracts | weekly | net_position | yes | 2026-09-01 | NZ DOLLAR, CME |

#### `equity_index`

Benchmark equity index for each economy. Feeds the risk pillar in two ways: as a proxy for the local growth and earnings picture, and, in concert with `CurrencyMeta.risk_beta`, as a read on whether the market is in risk-on or risk-off.

Pillar: **risk**. Canonical unit: `index`. Staleness allowance: 75 days. Fresh coverage: 100%.

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
| GDP | FRED | quarterly | weekly |
| COT positioning | CFTC | weekly, Friday 15:30 ET | Saturday, or Monday morning |
| Economic calendar | Forex Factory | weekly, current week only | Monday, then cached all week |
| PMIs, manual entries | you | monthly | first business day of the month |

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
gap. Do not work around it by widening `max_staleness_days`: that converts a
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
calendar clears every blackout.

**Calendar shows nothing for Monday on a Friday run**
Expected. The feed covers the current week only and there is no next-week feed.
Fetch early in the week.

**COT data is over a week old**
Expected and structural. Positions are snapped Tuesday and published Friday
15:30 ET. If it is more than two weeks old, check
`CotSource.latest_report_date`; publication has been interrupted before.

**A currency scores with low coverage**
`CurrencyScore.coverage` below 1.0 means part of the pillar weight had no usable
data. Run `registry.stale_refs()` for the actionable list. If it is CHF or NZD,
it is almost certainly the missing 2-year yield taking the monetary pillar with
it; see "The gaps that remain" above. Otherwise it is PMI, which is manual for
everyone.

**`coverage_report()` and `identifier_coverage()` disagree**
That is them working. `identifier_coverage()` at 1.0 with `coverage_report()` at
0.0 means every identifier is correct and the source stopped publishing, which
is a data problem to solve at the source. Both at 0.0 means the registry has no
source at all, which is a wiring problem. `current_account_gdp` is the standing
example of the first.

**A currency's monetary pillar is missing entirely**
Check `yield_2y` first. The monetary pillar draws most of its sub-weight from
the 2-year, so a currency without one falls below the component floor and loses
the whole pillar rather than degrading gracefully. CHF and NZD are in this state
by default. Everything else has a fetched 2-year.

**A central bank source returns nothing**
Run `CurvesSource.provider_health()` before touching parsing code. It reports
each provider's newest observation and separates "we broke it" from "they
stopped publishing". The Swiss franc's front end disappeared because the SNB
stopped, and no amount of debugging the client would have found that.

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
