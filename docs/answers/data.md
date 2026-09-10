# Data: open questions answered

Covers roadmap items 3, 5, 7 and 8. Every endpoint below was fetched live from
this machine on 2026-09-10. Where a fetch failed, the failure mode is recorded
in the same way the RBNZ 403 is recorded elsewhere in this repository:
distinguished from "does not exist."

A note on method for this pass. I did not have a `FRED_API_KEY` in this
environment and did not create a St. Louis Fed account to get one, because
doing so means sending an email address to a third party for a purpose nobody
asked for. Two things stood in for it. First, `fred.stlouisfed.org/graph/fredgraph.csv`
is a public, keyless CSV endpoint that serves the current vintage of any FRED
series, and it is what backs question 8's cross-sectional numbers below.
Second, everything under the OECD SDMX API, the ECB Data Portal, the Bank of
England, the RBA and the Federal Reserve Board's own research data is free and
keyless by design, which is most of what questions 3 and 5 turned on.

---

## Question 3: should the expected policy path be scored separately from the current setting?

**The question:** does a free forward-curve or policy-expectations source exist
for the G10, so that the modelling call is unblocked on data grounds?

**Verdict: NARROWED.** A free forward curve exists for four of eight
currencies and was fetched live. It does not exist, free, for the other four.

**Evidence.**

EUR, live. The ECB Data Portal `YC` dataflow, the same AAA government curve
already backing `yield_2y` and `yield_10y`, also carries instantaneous forward
rates under the `IF_nY` measure code, at no extra integration cost:

```
GET https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.IF_1Y?format=csvdata&lastNObservations=1
-> 2026-09-08, 3.0369 (1-year instantaneous forward rate)
GET .../SV_C_YM.IF_2Y -> 2026-09-08, 3.0779
GET .../SV_C_YM.IF_3Y -> 2026-09-08, 3.0889
GET .../SV_C_YM.IF_5Y -> 2026-09-08, 3.3256
```
compared with the spot curve on the same day, `SR_1Y` = 2.7654, `SR_2Y` =
2.9198. The forward curve sits above the spot curve, i.e. the market prices
further ECB tightening from here, which is exactly the separate signal the
question asks for.

GBP, live. The Bank of England's yield curve archive, the same ZIP already
named in `docs/data-sources.md` for the GBP 2-year, contains a second workbook
alongside the nominal curve: `OIS daily data current month.xlsx`, sheet
`1. fwds, short end`, titled in the sheet itself "UK instantaneous OIS forward
curve, short end". Verified by download and by reading the workbook:

```
GET https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip
-> OIS daily data current month.xlsx, sheet "1. fwds, short end"
2026-09-01: 1-month forward = 3.735%, 12-month forward = 4.576%, 60-month forward = 4.668%
```
This is a genuine OIS curve, not a government-bond proxy, which makes it the
cleanest of the four.

AUD, live. The RBA publishes a fitted forward curve directly, table F17:

```
GET https://www.rba.gov.au/statistics/tables/csv/f17-forward-rates.csv
-> 31-Aug-2026 row, series FZCF0D..FZCF1000D (0 to 10 years, quarterly steps)
0yr = 4.35, 1yr = 4.65, 2yr = 4.54, 5yr = 5.03, 10yr = 5.76
```
Daily, back to January 2017, zero-coupon forward rates fitted from the
government curve. Same caveat as the ECB series: it is a bond-curve forward,
not an OIS forward, so it embeds whatever small term premium sits in AGS.

USD, live, but not from FRED. The Federal Reserve Board publishes its own
Treasury forward curve, the Gurkaynak-Sack-Wright dataset, free and keyless,
updated weekly:

```
GET https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv
-> 2026-09-04: SVENF01 (1y instantaneous forward) = 4.3473
                SVENF04 (4y instantaneous forward) = 4.7307
                SVEN1F01 (1-year rate, 1 year forward) = 4.5011
                SVEN1F04 (1-year rate, 4 years forward) = 4.8326
                SVEN1F09 (1-year rate, 9 years forward) = 5.3920
```
The file explicitly disclaims itself as "a staff research product and not an
official statistical release, subject to delay, revision, or methodological
changes without advance notice." That caveat should travel with the series if
it is ever wired in.

CAD, not found. Bank of Canada Valet's group list was searched for every
series group mentioning OIS, CORRA, forward, or term structure. The only hits
are chart annotations inside staff analytical notes and financial stability
reports (`SAN_MARU20240305_C5`, `SAN_GUIL20210923_C2`, and similar), not
standing time series. No live CORRA-OIS forward curve is exposed as a Valet
series.

JPY, not found. The Ministry of Finance CSV (`jgbcme.csv`, verified again this
session) carries only par yields, columns `1Y` through `40Y`, no forward
column. A web search for a free TONA-OIS forward curve turns up only paid
vendors (Refinitiv-adjacent data resellers, cbonds, MacroMicro); the Bank of
Japan itself publishes the TONA fixing but not a forward curve derived from it.

CHF, not found, and unlikely to exist even if found. The SNB's own spot curve
(`rendoblid`) already stopped updating at 2025-07-31, per the existing entry
in `docs/data-sources.md`. A forward curve, if the SNB portal exposes one at
all, would be built on the same discontinued input and would be equally
frozen. Not worth a separate integration until the underlying curve resumes.

NZD, not found, same access block as the 2-year yield. `rbnz.govt.nz` was
retried this session and still returns HTTP 403 on every path tried,
including the direct statistics page. This confirms the existing note rather
than adding new information: the block is still in place from this network. I
cannot distinguish "this proxy is blocked" from "RBNZ blocks all automated
traffic" from here. Notably, the RBNZ is the one G10 central bank that
publishes its own explicit OCR forecast track in every Monetary Policy
Statement, which would be a better policy-path series than any curve-derived
one. That data exists and is exactly the data this block prevents reaching.

**Recommendation.** This is a data question, cleanly separable from the
modelling one, and the answer is: build it for four currencies, not eight. If
the macro strategist wants a `policy_path` sub-indicator, the concrete
registry additions would be:

- `EUR`: `SOURCE_ECB`, `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.IF_2Y` (or `IF_1Y`), no new source.
- `GBP`: `SOURCE_BOE`, the OIS workbook's forward sheet, same spreadsheet-reader
  dependency already blocked on `openpyxl` per the existing note in
  `docs/data-sources.md`.
- `AUD`: `SOURCE_RBA`, `f17-forward-rates.csv`, a new endpoint on an already-integrated source.
- `USD`: a new source, the Federal Reserve Board's `feds200628.csv`, distinct
  from FRED and carrying its own "not an official release" caveat that should
  be surfaced wherever the number is shown.
- `CAD`, `JPY`, `CHF`, `NZD`: no addition. `coverage_report()` for this
  indicator would sit at 50% by construction, same shape as `yield_2y` today.

Whether a 4/8 indicator is worth adding at all, given the coverage floor logic
already demotes currencies short a component, is the strategist's call, not
mine. What I can say on data grounds alone: for exactly the four currencies
that already have a live 2-year (EUR, GBP, AUD, USD, plus CAD which does not
have a forward curve), a genuine, free, separately-sourced expectations series
exists and was fetched live today.

---

## Question 5: is there a free PMI proxy, and is it better than the alternatives?

**The question:** what does a manual CSV actually cost to maintain, does a free
proxy exist, and would it beat dropping PMI from `GROWTH` altogether.

**Verdict: ANSWERED**, and the registry's own 0/8 for `pmi_manufacturing` is
correct but no longer the whole story: a free proxy covering all eight exists
and was not previously found.

**Evidence.** The OECD's Business Tendency Surveys dataflow, `DSD_STES@DF_BTS`,
version 4.0, on the same `sdmx.oecd.org` endpoint already used for CPI and
policy rates, carries a `Composite business confidence` measure (`BCICP`) by
country and by activity, with a manufacturing activity code of `C`. It was not
previously indexed in this registry, and it resolves for all eight, current as
of this session:

```
GET https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_BTS,4.0/DEU.M.BCICP......?format=csvfilewithlabels&lastNObservations=6
-> Germany, manufacturing, 2026-08 = -11.0, 2026-07 = -12.9 (percentage balance, monthly)

USA.M ... 2026-08 = 9.2, 2026-07 = 11.2
GBR.M ... 2026-08 = -8.2, 2026-07 = -22.7
CHE.M ... 2026-08 = -3.0, 2026-07 = -5.3
```

Japan, Canada, Australia and New Zealand carry the same measure but only at
quarterly frequency, matching each country's own survey cadence (Japan's is
the Tankan, published quarterly; Australia's and New Zealand's national
surveys are the NAB and ANZ business outlooks the question names, and the OECD
is republishing them, not running its own survey):

```
GET .../JPN.Q........  -> 2026-Q2, BCICP, manufacturing = 14
GET .../CAN.Q........  -> 2026-Q2, BCICP, manufacturing = -3.9
GET .../AUS.Q........  -> 2026-Q2, BCICP, manufacturing = 3.67
GET .../NZL.Q........  -> 2026-Q2, BCICP, manufacturing = -3.93
```

So the real coverage picture is: four currencies monthly (USD, EUR via DEU,
GBP, CHF), four quarterly (JPY, CAD, AUD, NZD), all free, all current to
within one release cycle, none manual.

Two honest caveats before this is treated as a drop-in PMI replacement.
First, the unit is `PB`, percentage balance (net percent of respondents
positive minus negative), not the 50-centred diffusion index a PMI is. Zero is
neutral here, not 50. Scoring code written against "distance from 50" would
silently misscore every observation if pointed at this series under the
`pmi_manufacturing` key, which is exactly the class of bug
`CLAUDE.md`'s unit-mismatch rule exists to prevent. Second, this is each
country's own national business tendency survey harmonised by the OECD, which
for Germany is the same family as the ifo and ZEW surveys the question names,
but it is not verified here to be the identical ifo headline series
number-for-number; it is the OECD's own compiled equivalent. I looked for a
free machine-readable ifo or ZEW series directly (FRED's `BSCICP03DEM665S` is
the OECD's own older BCI mirror and is frozen at January 2024, same pattern as
every other frozen FRED-OECD mirror in this registry; a Bundesbank SDW lookup
for an ifo series ID came back 404 on every guess tried) and did not find one.
The OECD BTS dataflow is the free source, not ifo or ZEW directly.

The European Commission's own Economic Sentiment Indicator (the ESI the
question also names) is real and free, via Eurostat's `teibs010` and DG
ECFIN's business and consumer survey downloads, confirmed to exist by fetch,
but it is EU-specific and would only ever help the EUR leg, which the OECD
series already covers. Not worth a second integration for the same currency.

**Recommendation.** Do not drop PMI from `GROWTH`, and do not keep the manual
CSV as the only route. Add the OECD BTS composite business confidence as a
new canonical indicator, distinct from `pmi_manufacturing` rather than a
replacement value under the same key, precisely because the unit differs.
Concretely: a new indicator key, something like `business_confidence_mfg`,
`SOURCE_OECD`, dataflow `DSD_STES@DF_BTS`, measure `BCICP`, activity `C`, unit
`percentage_balance`, with `DEU` standing in for `EUR` per the existing
euro-area-proxy convention. `pmi_manufacturing` stays in the registry exactly
as it is, licensed and manual, because it is a different, real thing (a
diffusion index against a defined neutral line of 50) and the two should not
be blended under one key. Whether `GROWTH` keeps both, weights the free proxy
higher given its live coverage, or retires the manual PMI input in favour of
it, is the scoring layer's call; the data-availability question is settled.

The manual PMI CSV upkeep cost, for the record, is one number per currency per
month, eight entries, entered from whichever headline release the operator
already reads (S&P Global for most, ISM for the US). That is a few minutes a
month, not a burden that argues for dropping the indicator on its own; the
better argument for adding the free proxy is coverage and freshness, not labour.

---

## Question 7: what covers the calendar if Forex Factory breaks?

**The question:** what do central banks and statistics offices publish as
machine-readable release calendars, and what would a fallback actually cover.

**Verdict: NARROWED.** Real, working fallbacks exist per country and per
release type. Nothing found replaces Forex Factory's single unified feed
covering all eight currencies and all ten event categories from one source.

**Evidence, confirmed working.**

The UK's ONS release calendar has a real RSS feed, fetched live, listing exact
scheduled release date and time, months to years ahead:

```
GET https://www.ons.gov.uk/releasecalendar?rss&limit=10&release-type=type-upcoming&sort=date-oldest
-> "Consumer price inflation, UK: December 2027", pubDate Wed, 19 Jan 2028 07:00:00 +0000
-> "GDP quarterly national accounts, UK: July to September 2027", pubDate Wed, 22 Dec 2027 07:00:00 +0000
```
This alone covers three of the plan's ten categories for GBP specifically:
CPI/PPI, GDP, and (the feed carries every ONS release, so also) retail sales
and the labour market release that carries UK unemployment and claimant count.
No impact rating is attached; the feed would need the same title-substring
classification `calendar.AVOID_PATTERNS` already does for Forex Factory.

The Federal Reserve publishes its full-year FOMC meeting calendar as a plain,
reliably scrapable HTML page, fetched live:

```
GET https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
-> 2026 dates present: e.g. meeting weeks including "April 08, 2026", "June ... 2026" and further 2026/2027 dates
```
This covers interest rate decisions for USD with total date reliability
(central banks essentially never move a scheduled decision date; only the
decision itself is the surprise), but it is HTML, not RSS or an API, and every
other G10 central bank publishes the equivalent on its own site in its own
format: the ECB has a governing council calendar page (confirmed to exist,
no `.ics` export link found on it), the BoE, BoJ, SNB, BoC, RBA and RBNZ each
publish their own annual meeting schedule. None checked here exposes a feed as
convenient as ONS's; all are real, public, and known well in advance.

**What was tried and blocked or not found.** The US Bureau of Labor
Statistics, the source for CPI and Non-Farm Payrolls, the single highest-impact
release on the plan's list, returns HTTP 403 to every automated request tried
in this session, including its RSS feed (`bls.gov/feed/bls_latest.rss`),
Akamai-fronted exactly like the RBNZ block. The BLS does publish its full
release schedule a year ahead on its own website; the data is not the
problem, automated access to it from this network is. Eurostat's euro
indicators release calendar page exists and was confirmed live, but no RSS or
API endpoint for it was found within this session; it may exist and simply
was not located. That is a gap in this search, not a claim that it does not
exist. Statistics Canada, the ABS, and Statistics NZ each publish release
calendars on their own sites; none was checked this session, budget did not
extend that far, and each should be assumed to follow the same pattern as
ONS: real, official, single-country.

**What this means for the fail-open/fail-closed decision.** A fallback built
from statistics-office and central-bank calendars would cover, with reasonable
confidence: interest rate decisions (all eight, central bank calendars, dates
known far in advance, very low risk of being wrong), GDP/CPI/PPI/retail
sales/employment for at least GBP (confirmed working) and plausibly the other
seven (same pattern, not individually confirmed). It would not cover central
bank speeches or FOMC-minutes-style releases in any structured way (those are
calendar entries a central bank adds to its own site ad hoc, not a standing
schedule), and it would do nothing for geopolitical events, which the current
design already marks as human judgement. The practical shape of a fallback is
the same one already forced by the 2-year yields: one integration per
currency, each covering only its own country, so losing Forex Factory would
mean rebuilding roughly eight to ten small scrapers rather than one. That is
the fact the risk decision should be made against.

---

## Question 8: score the first print, the latest vintage, or both?

**The question:** measure how large GDP revisions actually are, and say
whether the difference is large enough to move a cross-sectional z-score
across eight currencies.

**Verdict: ANSWERED.** Revisions are comfortably large enough to change the
cross-sectional ranking. Score the latest vintage for live scoring, exactly as
the registry does today, and reserve first-print data for any future backtest,
which is precisely what `Observation.released_at` and ALFRED exist for.

**A note on how this was measured.** The direct route, pulling the same G10
GDP series in both first-print and latest-vintage form from ALFRED, was
blocked in this environment on two independent fronts. There is no
`FRED_API_KEY` available here, and I chose not to create one, since a St.
Louis Fed account requires handing over an email address for a purpose nobody
asked for. Separately, ALFRED's interactive download page
(`alfred.stlouisfed.org/series/downloaddata`) is fronted by Akamai bot
management: a scripted POST of the download form, with and without a session
cookie jar, returns the same HTML form page rather than a file, and the
response carries `_abck` and `bm_sz` anti-bot cookies. The keyless graph
endpoints (`fredgraph.csv`, `alfredgraph.csv`) were also tried with a
`vintage_dates` parameter; both ignore it and return only the current vintage,
confirmed by requesting several different vintage dates and getting an
identical column labelled with today's date every time. This is a real,
reproducible technical block, not a guess, and it is different from the data
not existing: ALFRED vintages are real and the registry's description of them
is accurate.

What I could measure directly, and did: the current cross-sectional spread of
`gdp_yoy` itself, computed from the same keyless `fredgraph.csv` endpoint the
registry's series IDs point at, using each series' latest available quarter
(2026Q2 for six currencies, 2026Q1 for NZD, matching the registry's own
documented lag):

```
CHF  2.632%   AUD  2.138%   NZD  2.134%   USD  2.098%
GBP  1.174%   EUR  1.157%   CAD  1.128%   JPY  0.727%

mean = 1.648%, standard deviation = 0.636 percentage points
adjacent gaps: AUD-NZD 0.004pp, USD-AUD 0.040pp, GBP-EUR 0.017pp, EUR-CAD 0.029pp
```

Separately, real, sourced revision magnitudes, fetched from the primary
sources rather than from ALFRED:

The US Bureau of Economic Analysis states its own long-run average directly in
every advance-estimate release, quoted exactly from the fetched page:
"From the advance estimate to the latest estimate, the average revision
without regard to sign is 1.2 percentage points" (quarterly annualised rate).
A concrete recent example, also fetched: Q2 2025 US GDP was 3.0% at the
advance estimate, revised to 3.3% at both the second and third estimates, a
0.3 point revision. The UK's ONS: Q2 2025 GDP was reported at 0.4% quarter on
quarter in the preliminary estimate and later revised down to 0.3%, a 0.1 point
revision.

Putting the two together: the cross-sectional standard deviation of `gdp_yoy`
across the current G10 is 0.636 percentage points, and several adjacent
currencies sit within 0.05 points of each other. A quarter-on-quarter
annualised revision of 0.1 to 0.3 points is routine, and even after the
dampening that comes from expressing growth as a year-on-year figure rather
than an annualised quarterly one, a revision of that order to any one of the
four quarters inside a year-on-year window is easily enough to swap the
ranking of, for instance, AUD and NZD, or USD and AUD, on this pillar alone.
`GDP_YOY`'s pillar weight is not the whole `GROWTH` score, but the ranking
itself, not just the score's magnitude, moves.

**Recommendation.** Score the latest vintage for the live daily run; this is
already what the registry does, since it has no other option without a key.
Do not treat this as low-risk because the pillar is slow-moving: the numbers
above show it is not. The two places this actually bites are `coverage_report`
against a currency whose GDP print was just revised, and any future backtest.
For the backtest, `Observation.released_at` and `revision` on every GDP
observation are not optional decoration, they are the only way Phase 6 can
avoid scoring January's decision with data nobody had in January. Getting a
`FRED_API_KEY` and wiring `series/observations` with `realtime_start` /
`realtime_end` (the documented API route, distinct from the blocked
interactive download page) is a prerequisite for that phase and should be
budgeted as a real task, not assumed to fall out of the existing FRED
integration for free.
