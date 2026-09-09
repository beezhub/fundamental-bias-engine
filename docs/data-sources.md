# Data sources

Where every number in the engine comes from, what it costs, what its licence
allows, how often it changes, and where the holes are.

Read the "Coverage reality" section before anything else. The registry looks
fuller than the data actually is, and the difference is the thing most likely
to produce a confident, wrong bias.

All identifiers below were checked against the live source. Anything that could
not be checked is marked unverified, here and in
`src/fbe/datasources/registry.py`. Verification date: 2026-09-09.

---

## The five sources

| Source | Backs | Key | Cost | Cadence |
| --- | --- | --- | --- | --- |
| FRED | MONETARY, INFLATION, GROWTH, EMPLOYMENT, EXTERNAL | yes, free | free | daily to quarterly by series |
| CFTC COT | POSITIONING | no | free | weekly, Friday 15:30 ET, 3-day lag |
| Stooq | RISK, EXTERNAL price proxies | no | free | daily |
| Forex Factory | news blackout, no pillar | no | free | weekly, current week only |
| Manual | everything the others cannot supply | no | your time | when you type it |

---

## FRED

Federal Reserve Bank of St. Louis economic data. The backbone of the engine and
the only free source here with real cross-country reach.

**Base URL** `https://api.stlouisfed.org/fred/`

**Endpoints used** `series/observations`, `series`, `series/vintagedates`,
`series/search`. All four verified live.

### Getting a key

1. Create an account at <https://fredaccount.stlouisfed.org/apikeys>.
2. Request a key. It is issued immediately, free, no approval step.
3. It is a 32-character lowercase alphanumeric string.
4. Export it: `export FRED_API_KEY=your_key_here`

`fbe.config.default_config` reads `FRED_API_KEY` from the environment, so the
key never has to appear in a config file or a commit.

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
  - indicator: pmi_manufacturing
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
| `pmi.yaml` | Manufacturing and services PMIs for all eight. The largest manual burden. |
| `yields.yaml` | 2y government yields for the seven non-US currencies. |
| `inflation.yaml` | Headline and core CPI YoY for GBP, JPY, CHF, CAD, AUD, NZD. |
| `guidance.yaml` | Central bank guidance tone per currency, `-1..+1`, dovish to hawkish. |
| `overrides.yaml` | Ad-hoc corrections. Read last, so it wins. |

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

`registry.coverage_report()` counts whether a real, non-manual identifier exists
for each currency. It says nothing about whether that identifier still updates.
Several indicators score 100% while resting on series that stopped publishing in
2024 or 2025.

### What actually still updates

| Indicator | Identifier coverage | Currently updating | Verdict |
| --- | --- | --- | --- |
| `policy_rate` | 8/8 | 6/8 | CHF stopped 2024-03, NZD 2024-12 |
| `yield_2y` | 1/8 | 1/8 | **US only. Everything else is manual.** |
| `yield_10y` | 8/8 | 8/8 | good, monthly, 2-3 months behind |
| `cpi_yoy` | 8/8 | 2/8 | **USD and EUR only** |
| `core_cpi_yoy` | 7/8 | 2/8 | **USD and EUR only** |
| `gdp_yoy` | 8/8 | 8/8 | good, quarterly |
| `unemployment_rate` | 8/8 | 8/8 | good |
| `employment_change` | 7/8 | 7/8 | EUR manual |
| `retail_sales_yoy` | 8/8 | 7/8 | AUD stopped 2025Q2 |
| `industrial_production_yoy` | 5/8 | 4/8 | EUR/DEU stopped 2023-12; CHF, AUD, NZD absent |
| `pmi_manufacturing` | 0/8 | 0/8 | **licensed, entirely manual** |
| `trade_balance` | 8/8 | 8/8 | good |
| `current_account` | 8/8 | 0/8 | **all eight stopped 2024Q4** |
| `cot_net_position` | 8/8 | 8/8 | good, weekly, 8-10 days stale by design |
| `equity_index` | 8/8 | 8/8 | USD and JPY daily, the rest monthly |
| `vix` | global | yes | good, daily |
| `commodity_index` | global + CAD, AUD | yes | NZD dairy is manual |

### The four gaps that matter

**1. Inflation outside USD and EUR.** FRED's entire OECD CPI complex, index
levels (`...CPIALLMINMEI`) and year-on-year rates (`CPALTT01...`,
`CPGRLE01...`) alike, stopped updating in March or April 2025. Japan's stopped
in 2021-06, New Zealand's in 2023Q3. Six of eight currencies have no free,
current inflation print. This is the largest gap and the main reason
`ManualSource` exists. Fallback: `data/manual/inflation.yaml`, from each
statistics office directly.

**2. Two-year yields outside the US.** No 2y government bond series exists on
FRED for any non-US G10 issuer, verified by search. The front end of the curve
is where rate expectations live and rate expectations are what move G10 FX, so
these seven missing legs cost more than their count suggests. Fallback:
`data/manual/yields.yaml`. The monetary pillar leans on the 10y in the
meantime, which is slower and less policy-sensitive.

**3. PMIs.** S&P Global and ISM license these; no free API carries them. All
eight are manual and always will be unless a licence is bought. This is the one
gap where the manual burden is a monthly recurring chore rather than a one-off.

**4. Current account.** Every leg of the `B6BLTT02` family stopped at 2024Q4. At
`max_staleness_days = 45` the whole indicator drops out of coverage. It is a
slow structural measure, so a two-year-old reading is not worthless, but if the
external pillar is to use it, the staleness rule needs an explicit per-indicator
exception rather than a silent pass.

### The euro-area substitution

Eurostat feeds FRED current HICP, but the euro-area **aggregates** for
unemployment, employment, retail sales, industrial production, trade and the
current account all stopped between 2022 and 2023. Where that happens the
registry falls back to the German national series and says so in the note.

Germany is roughly a third of euro-area GDP. This is a real approximation, not a
free lunch, and it is biased in a knowable direction on at least one indicator:
Germany runs a structural trade surplus larger than the bloc's, so the proxy
flatters the euro on `trade_balance`.

Affected: `unemployment_rate`, `retail_sales_yoy`, `industrial_production_yoy`,
`trade_balance`, `current_account`, `equity_index`.

### Manual fallback per gap

| Gap | File | Where the number comes from |
| --- | --- | --- |
| CPI, core CPI for GBP/JPY/CHF/CAD/AUD/NZD | `inflation.yaml` | ONS, Statistics Bureau of Japan, BFS, StatCan, ABS, Stats NZ |
| 2y yields, seven currencies | `yields.yaml` | Bundesbank, DMO, MoF Japan, SNB, BoC, AOFM, NZDM, or a broker terminal |
| PMIs, all eight | `pmi.yaml` | S&P Global releases, ISM for the US |
| Policy rate CHF, NZD | `overrides.yaml` | SNB and RBNZ announcements |
| Retail sales AUD | `overrides.yaml` | ABS monthly retail turnover |
| Industrial production CHF, AUD, NZD | none needed | drop the indicator for these; do not fake it |
| NZD commodity link | `overrides.yaml` | GlobalDairyTrade index, fortnightly, globaldairytrade.info |
| Guidance tone, all eight | `guidance.yaml` | your own reading of the last statement |

---

## Indicator registry

Generated from `src/fbe/datasources/registry.py`, which is the source of
truth. Every identifier below was checked against the live source on
2026-09-09.

`Verified` means the identifier was confirmed to exist and return
observations. It does **not** mean the series is current: read the note.
A `manual` row is never verified, because that flag records the absence of
a free machine-readable source, not the operator's typing.

### Summary

| Indicator | Pillar | Unit | Freq | Verified refs | Manual refs |
| --- | --- | --- | --- | --- | --- |
| `policy_rate` | monetary | `percent` | daily | 8/8 | 0 |
| `yield_2y` | monetary | `percent` | daily | 1/8 | 7 |
| `yield_10y` | monetary | `percent` | monthly | 8/8 | 0 |
| `cpi_yoy` | inflation | `percent` | monthly | 8/8 | 0 |
| `core_cpi_yoy` | inflation | `percent` | monthly | 7/8 | 1 |
| `gdp_yoy` | growth | `percent` | quarterly | 8/8 | 0 |
| `unemployment_rate` | employment | `percent` | monthly | 8/8 | 0 |
| `employment_change` | employment | `persons` | monthly | 7/8 | 1 |
| `retail_sales_yoy` | growth | `percent` | monthly | 8/8 | 0 |
| `industrial_production_yoy` | growth | `percent` | monthly | 5/8 | 3 |
| `pmi_manufacturing` | growth | `index` | monthly | 0/8 | 8 |
| `trade_balance` | external | `usd` | monthly | 8/8 | 0 |
| `current_account` | external | `percent_of_gdp` | quarterly | 8/8 | 0 |
| `cot_net_position` | positioning | `contracts` | weekly | 8/8 | 0 |
| `equity_index` | risk | `index` | daily | 8/8 | 0 |
| `vix` | risk | `index` | daily | 1/1 | 0 |
| `commodity_index` | external | `index` | monthly | 3/4 | 1 |

### Full registry

#### `policy_rate`

The central bank's target rate, or the overnight rate that tracks it. The level matters less than where it sits relative to the rest of the G10, which is the whole premise of a relative-value framework.

Pillar: **monetary**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DFEDTARU` | percent | daily | level | yes | fed funds target range upper limit; current |
| EUR | fred | `ECBDFR` | percent | daily | level | yes | ECB deposit facility rate, the effective policy rate; current |
| GBP | fred | `IUDSOIA` | percent | daily | level | yes | SONIA, not Bank Rate itself, but it tracks Bank Rate within a few basis points; current |
| JPY | fred | `IRSTCI01JPM156N` | percent | monthly | level | yes | call money rate, monthly average; last observation 2026-06 |
| CHF | fred | `IRSTCI01CHM156N` | percent | monthly | level | yes | DISCONTINUED, last observation 2024-03; use the manual SNB policy rate entry instead |
| CAD | fred | `IRSTCI01CAM156N` | percent | monthly | level | yes | overnight money market rate; last observation 2026-06 |
| AUD | fred | `IRSTCI01AUM156N` | percent | monthly | level | yes | interbank overnight cash rate; last observation 2026-06 |
| NZD | fred | `IRSTCI01NZM156N` | percent | monthly | level | yes | DISCONTINUED, last observation 2024-12; use the manual RBNZ OCR entry instead |

#### `yield_2y`

Two-year government bond yield, the market's own forecast of where policy goes next. The 2y differential is the strongest single fundamental driver of a G10 pair over a multi-week horizon, which makes the seven missing legs below the registry's most expensive gap.

Pillar: **monetary**. Canonical unit: `percent`. Fetchable G10 coverage: 12%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DGS2` | percent | daily | level | yes | Treasury constant maturity; current |
| EUR | manual | `yield_2y` | percent | daily | level | **no** | German 2y Schatz; no 2y series for any non-US G10 issuer exists on FRED, verified by search. Source from the Bundesbank or a broker terminal and enter by hand. |
| GBP | manual | `yield_2y` | percent | daily | level | **no** | 2y gilt; see the EUR note, no free FRED series exists |
| JPY | manual | `yield_2y` | percent | daily | level | **no** | 2y JGB; see the EUR note, no free FRED series exists |
| CHF | manual | `yield_2y` | percent | daily | level | **no** | 2y Swiss Confederation; see the EUR note |
| CAD | manual | `yield_2y` | percent | daily | level | **no** | 2y Government of Canada; see the EUR note |
| AUD | manual | `yield_2y` | percent | daily | level | **no** | 2y Australian Commonwealth Government Bond; see the EUR note |
| NZD | manual | `yield_2y` | percent | daily | level | **no** | 2y New Zealand Government Bond; see the EUR note |

#### `yield_10y`

Ten-year benchmark government bond yield. Slower than the 2y and less directly tied to policy, but it is the one rate with clean, current coverage across all eight currencies, so it carries the monetary pillar wherever the front end is missing.

Pillar: **monetary**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `DGS10` | percent | daily | level | yes | Treasury constant maturity, daily; current |
| EUR | fred | `IRLTLT01DEM156N` | percent | monthly | level | yes | 10y Bund, the euro-area benchmark; the EZ aggregate IRLTLT01EZM156N lags further. Last observation 2026-06. |
| GBP | fred | `IRLTLT01GBM156N` | percent | monthly | level | yes | last observation 2026-06 |
| JPY | fred | `IRLTLT01JPM156N` | percent | monthly | level | yes | last observation 2026-06 |
| CHF | fred | `IRLTLT01CHM156N` | percent | monthly | level | yes | last observation 2026-06 |
| CAD | fred | `IRLTLT01CAM156N` | percent | monthly | level | yes | last observation 2026-06 |
| AUD | fred | `IRLTLT01AUM156N` | percent | monthly | level | yes | last observation 2026-06 |
| NZD | fred | `IRLTLT01NZM156N` | percent | monthly | level | yes | last observation 2026-06 |

#### `cpi_yoy`

Headline consumer price inflation, year on year. The inflation pillar scores the gap to each central bank's target rather than the raw print, so `CurrencyMeta.inflation_target` is the other half of this input.

Pillar: **inflation**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `CPIAUCSL` | index | monthly | yoy | yes | CPI-U all items, seasonally adjusted index; current |
| EUR | fred | `CP0000EZ19M086NEST` | index | monthly | yoy | yes | Eurostat HICP all items, euro area 19; current |
| GBP | fred | `CPALTT01GBM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-03. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| JPY | fred | `CPALTT01JPM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2021-06. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| CHF | fred | `CPALTT01CHM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-04. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| CAD | fred | `CPALTT01CAM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-03. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| AUD | fred | `CPALTT01AUQ659N` | percent | quarterly | level | yes | DISCONTINUED, last observation 2025-01. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| NZD | fred | `CPALTT01NZQ659N` | percent | quarterly | level | yes | DISCONTINUED, last observation 2023-07. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |

#### `core_cpi_yoy`

Consumer prices excluding food and energy, year on year. Central banks react to this more than to the headline, so it leads policy and therefore leads the currency.

Pillar: **inflation**. Canonical unit: `percent`. Fetchable G10 coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `CPILFESL` | index | monthly | yoy | yes | CPI-U less food and energy, SA index; current |
| EUR | fred | `00XEFDEZ19M086NEST` | index | monthly | yoy | yes | Eurostat HICP excluding energy, food, alcohol and tobacco; current |
| GBP | fred | `CPGRLE01GBM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-03. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| JPY | fred | `CPGRLE01JPM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2021-06. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| CHF | fred | `CPGRLE01CHM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-04. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| CAD | fred | `CPGRLE01CAM659N` | percent | monthly | level | yes | DISCONTINUED, last observation 2025-03. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| AUD | fred | `CPGRLE01AUQ659N` | percent | quarterly | level | yes | DISCONTINUED, last observation 2025-01. FRED's OECD CPI complex stopped updating in 2025-03/04; no free current series exists for this currency |
| NZD | manual | `core_cpi_yoy` | percent | quarterly | level | **no** | no core CPI series for New Zealand on FRED; the RBNZ sectoral factor model estimate is the usual substitute |

#### `gdp_yoy`

Real GDP growth, year on year. Slow and heavily revised, so it anchors the growth pillar rather than driving it. Full G10 coverage, which is rare enough in this registry to be worth stating.

Pillar: **growth**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `GDPC1` | billions_chained_usd | quarterly | yoy | yes | real GDP, SAAR chained 2017 dollars; last observation 2026Q2 |
| EUR | fred | `CLVMNACSCAB1GQEA19` | millions_chained_eur | quarterly | yoy | yes | Eurostat real GDP, euro area 19; last observation 2026Q2 |
| GBP | fred | `NGDPRSAXDCGBQ` | millions_chained_gbp | quarterly | yoy | yes | last observation 2026Q2 |
| JPY | fred | `JPNRGDPEXP` | billions_chained_jpy | quarterly | yoy | yes | real GDP by expenditure; last observation 2026Q2 |
| CHF | fred | `CLVMNACSCAB1GQCH` | millions_chained_chf | quarterly | yoy | yes | last observation 2026Q2 |
| CAD | fred | `NGDPRSAXDCCAQ` | millions_chained_cad | quarterly | yoy | yes | last observation 2026Q2 |
| AUD | fred | `NGDPRSAXDCAUQ` | millions_chained_aud | quarterly | yoy | yes | last observation 2026Q2 |
| NZD | fred | `NZLGDPRQPSMEI` | percent | quarterly | level | yes | already published as a year-on-year growth rate, so no transform; last observation 2026Q1 |

#### `unemployment_rate`

Harmonised unemployment rate. Compared cross-sectionally against the rest of the G10 and against its own recent trend, since the level that counts as full employment differs by country.

Pillar: **employment**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `UNRATE` | percent | monthly | level | yes | BLS headline U-3; current, roughly one month behind |
| EUR | fred | `LRHUTTTTDEM156S` | percent | monthly | level | yes | German harmonised rate. euro-area aggregate on FRED stopped updating; German national series used as the euro-area proxy (LRHUTTTTEZM156S last observation 2023-01). Last observation 2026-06. |
| GBP | fred | `LRHUTTTTGBM156S` | percent | monthly | level | yes | last observation 2026-04 |
| JPY | fred | `LRHUTTTTJPM156S` | percent | monthly | level | yes | last observation 2026-06 |
| CHF | fred | `LRUN64TTCHQ156S` | percent | quarterly | level | yes | quarterly ILO rate, aged 15-64; Switzerland publishes no monthly harmonised rate on FRED. Last observation 2026Q1. |
| CAD | fred | `LRHUTTTTCAM156S` | percent | monthly | level | yes | last observation 2026-07 |
| AUD | fred | `LRHUTTTTAUM156S` | percent | monthly | level | yes | last observation 2026-06 |
| NZD | fred | `LRHUTTTTNZQ156S` | percent | quarterly | level | yes | quarterly by publication, not by choice; last observation 2026Q2 |

#### `employment_change`

Change in the number of people employed. The flow, not the stock: a falling unemployment rate driven by people leaving the labour force is a different signal from one driven by hiring, and this indicator is what separates them.

Pillar: **employment**. Canonical unit: `persons`. Fetchable G10 coverage: 88%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `PAYEMS` | thousands_of_persons | monthly | diff | yes | total nonfarm payrolls; the differenced level is the NFP headline. Current. |
| EUR | manual | `employment_change` | persons | quarterly | level | **no** | no live euro-area or German employment level on FRED (LFEMTTTTEZQ647S last observation 2022-10); take the Eurostat quarterly employment release by hand |
| GBP | fred | `LFEMTTTTGBQ647S` | persons | quarterly | diff | yes | last observation 2026Q1 |
| JPY | fred | `LFEMTTTTJPM647S` | persons | monthly | diff | yes | last observation 2026-06 |
| CHF | fred | `LFEMTTTTCHQ647S` | persons | quarterly | diff | yes | last observation 2026Q1 |
| CAD | fred | `LFEMTTTTCAM647S` | persons | monthly | diff | yes | last observation 2026-07 |
| AUD | fred | `LFEMTTTTAUM647S` | persons | monthly | diff | yes | last observation 2026-06 |
| NZD | fred | `LFEMTTTTNZQ647S` | persons | quarterly | diff | yes | last observation 2026Q2 |

#### `retail_sales_yoy`

Retail trade volume, year on year. The fastest read on household demand, and the growth pillar's main monthly input given that GDP arrives quarterly and late.

Pillar: **growth**. Canonical unit: `percent`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USASLRTTO01GYSAM` | percent | monthly | level | yes | OECD retail volume growth, chosen over the fresher US-only RSAFS so the eight legs are measured the same way. Last observation 2026-05. |
| EUR | fred | `DEUSLRTTO01GYSAM` | percent | monthly | level | yes | euro-area aggregate on FRED stopped updating; German national series used as the euro-area proxy (EA19SLRTTO01GYSAM last observation 2023-10). Last observation 2026-05. |
| GBP | fred | `GBRSLRTTO01GYSAM` | percent | monthly | level | yes | last observation 2026-06 |
| JPY | fred | `JPNSLRTTO01GYSAM` | percent | monthly | level | yes | last observation 2026-05 |
| CHF | fred | `CHESLRTTO01GYSAM` | percent | monthly | level | yes | last observation 2026-05 |
| CAD | fred | `CANSLRTTO01GYSAM` | percent | monthly | level | yes | last observation 2026-04 |
| AUD | fred | `SLRTTO01AUQ659S` | percent | quarterly | level | yes | DISCONTINUED, last observation 2025Q2; Australia has no live retail series on FRED |
| NZD | fred | `SLRTTO01NZQ659S` | percent | quarterly | level | yes | last observation 2026Q1 |

#### `industrial_production_yoy`

Industrial production, year on year. Coverage here is the worst of the growth inputs: four of eight are live. Weight it accordingly, or the growth pillar ends up scoring the countries that happen to publish rather than the countries that happen to be growing.

Pillar: **growth**. Canonical unit: `percent`. Fetchable G10 coverage: 62%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USAPRINTO01GYSAM` | percent | monthly | level | yes | OECD basis for cross-country comparability; INDPRO is the fresher US-only alternative. Last observation 2026-06. |
| EUR | fred | `DEUPRINTO01GYSAM` | percent | monthly | level | yes | DISCONTINUED, last observation 2023-12. euro-area aggregate on FRED stopped updating; German national series used as the euro-area proxy, and the German proxy has now stopped too |
| GBP | fred | `GBRPRINTO01GYSAM` | percent | monthly | level | yes | last observation 2026-05 |
| JPY | fred | `JPNPRINTO01GYSAM` | percent | monthly | level | yes | last observation 2026-05 |
| CHF | manual | `industrial_production_yoy` | percent | quarterly | level | **no** | no Swiss industrial production series on FRED in any live form |
| CAD | fred | `CANPRINTO01GYSAM` | percent | monthly | level | yes | last observation 2026-04 |
| AUD | manual | `industrial_production_yoy` | percent | quarterly | level | **no** | no Australian industrial production series on FRED |
| NZD | manual | `industrial_production_yoy` | percent | quarterly | level | **no** | no New Zealand industrial production series on FRED |

#### `pmi_manufacturing`

Manufacturing purchasing managers' index, 50 being the expansion line. The best leading indicator in the growth pillar and the one with zero free coverage, which is why the manual source exists at all.

Pillar: **growth**. Canonical unit: `index`. Fetchable G10 coverage: 0%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| EUR | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| GBP | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| JPY | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| CHF | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| CAD | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| AUD | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |
| NZD | manual | `pmi_manufacturing` | index | monthly | level | **no** | PMIs are licensed by S&P Global and ISM and are on no free API; operator enters the headline print by hand |

#### `trade_balance`

Merchandise trade balance in US dollars, seasonally adjusted. Already currency-converted by the source, so the eight legs are directly comparable without an FX step. Full, current G10 coverage.

Pillar: **external**. Canonical unit: `usd`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `XTNTVA01USM667S` | usd | monthly | level | yes | OECD basis for comparability; BOPGSTB is the fresher US-only goods and services balance. Last observation 2026-06. |
| EUR | fred | `XTNTVA01DEM667S` | usd | monthly | level | yes | euro-area aggregate on FRED stopped updating; German national series used as the euro-area proxy (XTNTVA01EZM667S last observation 2022-12). Germany runs a structural surplus larger than the bloc's, so this proxy flatters the euro. Last observation 2026-05. |
| GBP | fred | `XTNTVA01GBM667S` | usd | monthly | level | yes | last observation 2026-06 |
| JPY | fred | `XTNTVA01JPM667S` | usd | monthly | level | yes | last observation 2026-06 |
| CHF | fred | `XTNTVA01CHM667S` | usd | monthly | level | yes | last observation 2026-06 |
| CAD | fred | `XTNTVA01CAM667S` | usd | monthly | level | yes | last observation 2026-06 |
| AUD | fred | `XTNTVA01AUM667S` | usd | monthly | level | yes | last observation 2026-06 |
| NZD | fred | `XTNTVA01NZM667S` | usd | monthly | level | yes | last observation 2026-06 |

#### `current_account`

Current account balance as a share of GDP. A structural measure of whether a currency is financed by the world or financing it. It moves slowly, which is fortunate, because every leg below stopped updating in late 2024.

Pillar: **external**. Canonical unit: `percent_of_gdp`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `USAB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 across the whole family |
| EUR | fred | `DEUB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | euro-area aggregate on FRED stopped updating; German national series used as the euro-area proxy (EA19B6BLTT02STSAQ last observation 2022Q4). Last observation 2024Q4. |
| GBP | fred | `GBRB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |
| JPY | fred | `JPNB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |
| CHF | fred | `CHEB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |
| CAD | fred | `CANB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |
| AUD | fred | `AUSB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |
| NZD | fred | `NZLB6BLTT02STSAQ` | percent_of_gdp | quarterly | level | yes | last observation 2024Q4 |

#### `cot_net_position`

Net speculative position in CME currency futures from the CFTC Commitments of Traders report. A crowded position is a reason to fade a fundamental view, not to add to it, so this pillar usually works against the others by design.

Pillar: **positioning**. Canonical unit: `contracts`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | cftc | `098662` | contracts | weekly | net_position | yes | USD Index on ICE, in the Legacy report (6dca-aqww), not TFF. The primary dollar read is the sign-flipped complement of the other seven; this contract is a small, thinly held cross-check. |
| EUR | cftc | `099741` | contracts | weekly | net_position | yes | EURO FX, CME, TFF dataset gpe5-46if |
| GBP | cftc | `096742` | contracts | weekly | net_position | yes | BRITISH POUND, CME |
| JPY | cftc | `097741` | contracts | weekly | net_position | yes | JAPANESE YEN, CME |
| CHF | cftc | `092741` | contracts | weekly | net_position | yes | SWISS FRANC, CME |
| CAD | cftc | `090741` | contracts | weekly | net_position | yes | CANADIAN DOLLAR, CME |
| AUD | cftc | `232741` | contracts | weekly | net_position | yes | AUSTRALIAN DOLLAR, CME |
| NZD | cftc | `112741` | contracts | weekly | net_position | yes | NZ DOLLAR, CME |

#### `equity_index`

Benchmark equity index for each economy. Feeds the risk pillar in two ways: as a proxy for the local growth and earnings picture, and, in concert with `CurrencyMeta.risk_beta`, as a read on whether the market is in risk-on or risk-off.

Pillar: **risk**. Canonical unit: `index`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USD | fred | `SP500` | index | daily | level | yes | daily close; FRED holds a rolling ten-year window only |
| EUR | fred | `SPASTT01DEM661N` | index | monthly | level | yes | OECD share price index, monthly average, 2015=100. Monthly is too slow for a risk pillar; prefer the Stooq daily feed and keep this as the offline fallback. Last observation 2026-06. |
| GBP | fred | `SPASTT01GBM661N` | index | monthly | level | yes | OECD share price index; last observation 2026-06 |
| JPY | fred | `NIKKEI225` | index | daily | level | yes | daily close; current |
| CHF | fred | `SPASTT01CHM661N` | index | monthly | level | yes | OECD share price index; last observation 2026-06 |
| CAD | fred | `SPASTT01CAM661N` | index | monthly | level | yes | OECD share price index; last observation 2026-06 |
| AUD | fred | `SPASTT01AUM661N` | index | monthly | level | yes | OECD share price index; last observation 2026-06 |
| NZD | fred | `SPASTT01NZM661N` | index | monthly | level | yes | OECD share price index; last observation 2026-06 |

#### `vix`

CBOE implied volatility on the S&P 500. A single global number, not a per-currency one: it sets the risk regime, and the currencies then sort themselves by `CurrencyMeta.risk_beta`.

Pillar: **risk**. Canonical unit: `index`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GLOBAL | fred | `VIXCLS` | index | daily | level | yes | daily close; current |

#### `commodity_index`

Terms-of-trade proxy for the commodity currencies, plus a global benchmark. Only the three currencies with a `commodity_link` in `CurrencyMeta` carry a specific ref; the others take the global index or nothing.

Pillar: **external**. Canonical unit: `index`. Fetchable G10 coverage: 100%.

| Currency | Source | Series ID | Unit | Freq | Transform | Verified | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GLOBAL | fred | `PALLFNFINDEXM` | index | monthly | level | yes | IMF all commodity price index, 2016=100; last obs 2026-07 |
| CAD | fred | `DCOILWTICO` | usd_per_barrel | daily | level | yes | WTI spot, the standard Canadian dollar terms-of-trade proxy |
| AUD | fred | `PIORECRUSDM` | index | monthly | level | yes | IMF iron ore price index; last observation 2026-07 |
| NZD | manual | `commodity_index` | index | irregular | level | **no** | no dairy price index on FRED, verified by search. The GlobalDairy Trade auction index is the right series and is published fortnightly on globaldairytrade.info; enter it by hand. PFOODINDEXM is a poor but free substitute. |

---

## Refresh cadence

What is worth refetching, and how often.

| Data | Real cadence | Fetch |
| --- | --- | --- |
| Yields, spot FX, VIX, equity closes | daily | daily, before the session |
| CPI, unemployment, retail sales, IP, trade balance | monthly | daily is harmless, weekly is enough |
| GDP, current account | quarterly | weekly |
| COT positioning | weekly, Friday 15:30 ET | Saturday, or Monday morning |
| Economic calendar | weekly, current week only | Monday, then cached all week |
| PMIs, manual entries | monthly, when you type them | first business day of the month |

The engine is built for a daily morning run, matching the plan's daily routine.
Nothing here needs intraday polling, and every source in the stack punishes it.

---

## Troubleshooting

**"FRED source unavailable"**
`FRED_API_KEY` is not in the environment. Check with `echo $FRED_API_KEY`. Get
one at <https://fredaccount.stlouisfed.org/apikeys>. Or run with `offline: true`
if there is a warm cache.

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
Almost certainly one of the discontinued OECD families. Check the `note` on its
`SeriesRef` in the registry, then confirm with `FredSource.last_updated`. The
whole "Coverage reality" section above is about this failure mode. Do not work
around it by widening `max_staleness_days`: that hides the gap everywhere at
once.

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
data. Check `registry.coverage_report()` and `ManualSource.missing()`. Usually
it is CPI or PMI, which is to say usually it is one of the two known gaps.

**An offline run raises on a missing cache entry**
Correct behaviour. Offline means the cache is the source of truth, and a missing
entry is a misconfiguration, not an empty result. Run once online to warm the
cache, then switch back.

**Two runs on the same day give different biases**
Either offline is off and a source revised something between runs, or the config
changed. `BiasReport.config_digest` tells you which. If the digest matches and
the bias moved, a data source revised a print, which is exactly what ALFRED
exists to make visible.
