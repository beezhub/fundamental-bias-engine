# Test fixtures

Provenance for every committed response body. A fixture with no entry here
cannot be told apart from one somebody typed, and the point of a spot check is
that it breaks the build when a source changes a unit or a scale underneath us.

Each entry records the exact request, the date it was captured, and the status
the source answered with.

## OECD

Captured 2026-09-14 against `https://sdmx.oecd.org/public/rest/`, no
credential, `Accept: application/vnd.sdmx.data+csv`.

| File | Request | Status |
| --- | --- | --- |
| `oecd_gbr_cpi_monthly.csv` | `data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/GBR.M.N.CPI.PA._T.N.GY?startPeriod=2026-05&endPeriod=2026-07` | 200 |
| `oecd_aus_cpi_quarterly.csv` | `data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/AUS.Q.N.CPI.PA._T.N.GY?startPeriod=2026-Q1&endPeriod=2026-Q2` | 200 |
| `oecd_arity_error.txt` | the same monthly key with seven segments instead of eight | 403 |
| `oecd_deu_bts_monthly.csv` | `data/OECD.SDD.STES,DSD_STES@DF_BTS,4.0/DEU.M.BCICP.PB.C.Y...?startPeriod=2026-05-01&endPeriod=2026-08-31` | 200 |
| `oecd_aus_bts_quarterly.csv` | `data/OECD.SDD.STES,DSD_STES@DF_BTS,4.0/AUS.Q.BCICP.PB.C.Y...?startPeriod=2025-01-01&endPeriod=2026-06-30` | 200 |

All four CSV bodies are byte-exact as returned. The monthly CPI one arrives out
of order (June, May, July), which is why the parser is not allowed to assume the
API sorts. Both BTS bodies arrive out of order too, from a second flow, so that
is the API's habit rather than one flow's quirk.

**The two BTS requests above are the exact keys `OecdSource.bts_key` builds and
the registry stores**, nine segments matching `DIMENSIONS["DSD_STES"]`, not the
ten-segment form the finmark refs use (see #57). That is deliberate and a test
holds it: the whole risk in this registry entry is a key that answers HTTP 404
`NoRecordsFound`, which is indistinguishable from a dead series, so the captured
body has to be evidence for the key the code actually sends rather than for a
neighbouring one that also happens to work.

The two BTS bodies are the only fixtures here holding negative observations, and
that is what they are for. Their unit is `PB`, a percentage balance, which is
neutral at zero and sits either side of it. A purchasing managers' index is
neutral at 50 and never negative. Any change that routed this material under
`pmi_composite` would show up as sign, which is the only place it would show up:
the cross-sectional z-score absorbs a constant offset, so the ranking would
still look orderly downstream. See `docs/answers/data.md` question 5.

The monthly window runs to 2026-08-31 so that the body carries `2026-08`, which
is the `last_observed` the registry claims for all four monthly legs and the
-11.0 that `docs/answers/data.md` question 5 publishes. The doc's worked number
is therefore a fixture and not prose.

The quarterly window runs over six quarters rather than the three needed to
demonstrate stamping, because four of the six values are distinct and that is
what lets the test catch a transposition. 2025-Q4 and 2026-Q1 both read
9.333333, so a swap of exactly those two would still pass; no other pair
would.

`oecd_arity_error.txt` holds the body only. The OECD answered it with **HTTP
403**, not the 422 that `src/fbe/datasources/oecd.py` documents. The body text
is exactly what that docstring quotes, so the message is the source's; only the
status differs, and it may differ because of the network this was captured
from. Nothing depends on which it is: any 4xx outside `RetryPolicy.retry_on_status`
raises without being retried.

## Curve providers

Captured 2026-09-14, no credential on any of them.

| File | Request | Status |
| --- | --- | --- |
| `boc_2y_yield.json` | `https://www.bankofcanada.ca/valet/observations/BD.CDN.2YR.DQ.YLD/json?start_date=2026-09-01&end_date=2026-09-04` | 200 |
| `ecb_2y_spot.csv` | `https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y?format=csvdata&startPeriod=2026-09-01&endPeriod=2026-09-04` | 200 |
| `rba_f2_2y.csv` | `https://www.rba.gov.au/statistics/tables/csv/f2-data.csv` | 200 |

The Bank of Canada and ECB bodies are byte-exact as returned.

`rba_f2_2y.csv` is **truncated**, and that is the only edit made to it. Table
F2 carries every session since May 2013, 64,777 lines. The eleven-line header
block is byte-exact, including the byte order mark the RBA serves and the two
blank rows inside it, and the six data rows are the last six of the real file
rather than anything typed. Nothing else was changed: the point of keeping the
header verbatim is that the `Series ID` row is what the parser locates, and its
position within that block is exactly what has moved between releases before.

## Curve providers, second set

Captured 2026-09-14, no credential on any of them.

| File | Request | Status |
| --- | --- | --- |
| `jgbcme.csv` | `https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv` | 200 |
| `jgbcme_all.csv` | `.../interest_rate/historical/jgbcme_all.csv` | 200 |
| `boe_iadb_bank_rate.csv` | `https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp?csv.x=yes&Datefrom=01/Sep/2026&Dateto=11/Sep/2026&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N` | 302 then 200 |
| `boe_yield_curve.zip` | `https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip` | 200 |
| `snb_rendoblid.csv` | `https://data.snb.ch/api/cube/rendoblid/data/csv/en` | 200 |

`jgbcme.csv` and `boe_iadb_bank_rate.csv` are byte-exact. The other three are
truncated or narrowed, and nothing in any of them was typed:

- `jgbcme_all.csv` keeps its real title and header rows, one real row where the
  2-year tenor is `-`, and the four most recent real rows. The full file is
  13,292 lines. Note that the real history file carries **no** footer row and
  no non-ASCII byte anywhere, so this truncation removed nothing. The
  Shift-JIS witness is `jgbcme.csv`, which keeps the blank row and the `※`
  footer and genuinely fails a UTF-8 decode.
- `boe_yield_curve.zip` is rebuilt from the live archive: the five real header
  rows and four real data rows of the real `3. spot, short end` sheet, narrowed
  to the first 27 columns so the fixture is 6 KB rather than 300 KB. The
  maturity headers and the yields are the published ones. A second member is a
  placeholder, present only so the member-selection test has something to not
  pick.
- `snb_rendoblid.csv` keeps the real two metadata lines, the real header, one
  real blank-valued row from 1988 and the four most recent real rows. The cube
  is frozen where the module docstring says it is: last observation
  2025-07-31, `PublishingDate` 2025-09-01.

Two things these captures settled that the spec did not. The workbook's
2-year maturity header is `1.999999920000001`, not `2.0`, so the column is
found by nearest-within-tolerance rather than by equality. And column A arrives
as a `datetime` rather than an Excel serial, because `openpyxl` converts
date-formatted cells; the serial path is still implemented and tested, because
a workbook written another way would need it.

## Reserve Bank of New Zealand

Not a capture. **The owner downloaded this one through a browser**, because the
RBNZ blocks this project's egress and no run can retrieve it. See issue #91 for
the attempt history and the verdict.

| File | Source | Retrieved |
| --- | --- | --- |
| `rbnz_hb2_daily_close.xlsx` | Table B2, "Daily wholesale interest rates (% pa)", link text "Daily close (2018-current), XLSX \| 440KB", from `rbnz.govt.nz/statistics/series/exchange-and-interest-rates/wholesale-interest-rates` | 2026-09-15, by the owner, on a mobile connection |

**Truncated, and nothing in it was typed.** The published file is 450 KB, 48
series and 2183 rows. This fixture is 7 KB and keeps:

- All five real header rows, byte for byte: group, tenor, notes, unit, series ID.
- Seven of the 48 columns, chosen so that locating a column by ID is doing real
  work. `INM.DG102.NZZCF`, the 2-year, keeps its true neighbours
  `INM.DG101.NZZCF` and `INM.DG105.NZZCF` on either side, both in the same unit
  and a similar range, so an off-by-one parser returns a plausible wrong yield
  rather than an error. The Official Cash Rate, the 90-day bank bill, the
  10-year and the 2-year swap are kept as decoys from other groups.
- The five most recent real sessions, 2026-09-08 to 2026-09-14.
- One real session from the 2020 publication gap, 2020-05-15, where the RBNZ
  published the row and left the 2-year blank.

The `Series Definitions` and `Table Description` sheets are kept, the first
narrowed to the seven retained series and the second verbatim. The published
date inside the workbook is 2026-09-15.

The gap row is the reason this fixture exists in this shape. 286 of 2178 rows
are blank in the 2-year column, 224 of them in 2020, with the longest run 255
consecutive sessions ending 2020-11-19 and none after 2021. A blank means the
RBNZ published a session with no 2-year yield to report. Reading it as `0.0`
would give the monetary pillar a policy-relevant rate of zero on a day New
Zealand had none, and the value would look entirely plausible. That is the case
`tests/test_rbnz_b2_fixture.py` pins.

`CurvesSource.fetch_rbnz` reads this workbook and `tests/test_curves_rbnz.py`
serves the fixture to it. The fixture is here so the evidence survives without
asking the owner to download it a second time, and so the parser is checked
against what the RBNZ published rather than against itself.

## FRED

None of these three is a capture. Each is constructed in the shape FRED's
`series/observations` endpoint returns, because FRED requires a key and none
was available in the environment they were written in. Every one carries a
`_fixture_note` saying so and a `_fixture_written_on` date, and a test asserts
that the note is still there.

| File | Series | Stands in for | Written |
| --- | --- | --- | --- |
| `fred_dgs2_observations.json` | `DGS2` | the 2 year Treasury yield, in percent | 2026-09-12 |
| `fred_dexuseu_observations.json` | `DEXUSEU` | EURUSD, US dollars per euro, near 1.08 | 2026-09-14 |
| `fred_dexjpus_observations.json` | `DEXJPUS` | USDJPY, yen per US dollar, near 149 | 2026-09-14 |

The two spot fixtures exist as a pair and have to stay one. FRED names each
series by its own numerator rule, so `DEXUSEU` and `DEXJPUS` are quoted in
opposite directions while `FRED_SPOT_SERIES` is keyed the way the market writes
the pair. Their values sit two orders of magnitude apart so that an inversion
applied to either one cannot satisfy both assertions. Replacing one with a
capture and leaving the other constructed is fine; replacing one with a
different series is not.

Both also publish a missing session at 2026-09-10, a Thursday rather than a
weekend, in the two shapes a hole arrives in: the euro body carries FRED's `"."`
marker for that date and the yen body omits the row.

What a constructed body cannot do is break the build when FRED changes a unit
or a scale underneath us, which is the point of a spot check. A human with a
key should record real responses over these. See #56.

## Forex Factory calendar

Captured 2026-09-18 against `https://nfs.faireconomy.media/ff_calendar_thisweek.json`,
no credential, no headers beyond the client's defaults.

| File | Request | Status |
| --- | --- | --- |
| `forexfactory_calendar_thisweek.json` | the feed's only URL, no parameters | 200 |

Byte-exact as returned. 105 rows for the week of 2026-09-14, carrying exactly
the six keys the module docstring records and nothing else: there is no `actual`
key on any row in this capture, which is why `_display` has to treat an absent
key and an empty string alike.

The week is a good one to have caught. It holds an FOMC decision, a Bank of
England decision and a Bank of Japan decision, so the rate-decision patterns are
exercised against three different central banks' wording rather than one. The
four FOMC rows land on two instants thirty minutes apart, which is the
overlapping case `blackout_windows` has to merge. The merge tests build that
shape by hand rather than reading it from here, so re-capturing this file in a
quieter week costs nothing: what the fixture contributes is evidence that the
shape is real, not the coverage itself.

Every `date` in it carries `-04:00`, US Eastern in summer. That is a limitation
of this capture rather than of the feed: the offset moves to `-05:00` in
November, so the daylight-saving test uses a written payload carrying both.
A capture taken in winter would hold `-05:00` throughout and the parser must not
care either way.

The spot check reads one row, the Federal Funds Rate at `2026-09-16T14:00:00-04:00`,
and asserts it arrives as 18:00 UTC with its forecast and previous intact. That
is the assertion that breaks the build if the publisher renames a field, changes
the offset convention, or starts coercing its display strings to numbers.
