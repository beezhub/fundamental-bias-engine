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

## FRED

`fred_dgs2_observations.json` is **not** a capture. It is constructed in FRED's
response shape, because FRED requires a key and none was available. Its own
header says so and `tests/test_fred.py` asserts that it does. See #56.
