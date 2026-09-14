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

## FRED

`fred_dgs2_observations.json` is **not** a capture. It is constructed in FRED's
response shape, because FRED requires a key and none was available. Its own
header says so and `tests/test_fred.py` asserts that it does. See #56.
