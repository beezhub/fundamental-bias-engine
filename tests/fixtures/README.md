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
