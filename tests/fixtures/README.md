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

Both CSV bodies are byte-exact as returned. The monthly one arrives out of
order (June, May, July), which is why the parser is not allowed to assume the
API sorts.

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
