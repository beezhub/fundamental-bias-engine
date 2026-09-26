# 0015. A series inside a source is a named gap, and the collector asks for it alone

Status: Accepted

## Context

`OecdSource.fetch` walks every registry entry routed to the OECD, one narrow
request per `(indicator, currency)`, and lets the first `SourceError` out. The
collector catches it and records the whole source as `FAILED` with zero
observations. So one series failing after its retries costs every OECD series in
the run.

This has now happened three times. On 2026-09-24 the first refresh failed on
`JPN.M.IRSTCI.PA` with HTTP 500 and the second on `DEU.M.IRLT.PA` with a read
timeout, while the DEU series answered by hand in under six seconds. On
2026-09-25 the OECD answered 38 of 39 series, every one of them was cached, the
Japanese policy rate failed, and the source reported zero observations. Coverage
fell from 64-96% to 34-51% and every pair in the day's bias went neutral. An
`fbe --offline refresh -s oecd` afterwards refused on the one missing cache
entry, so the 38 cached bodies were unusable too.

ADR 0013 ruled this question for the curve providers: one provider failing
costs its own currency, the failure is a named status, and a provider that
answers with nothing is still a raised error. It was implemented in #218 by
splitting the fan-out into one source class per provider. That shape does not
transfer. The OECD's 39 series share one institution, one base URL, one rate
limit and one `available()`, and 39 classes for one endpoint would be structure
with no meaning.

The triage desk's comment on #275 made two further points this record takes.
The defect is not OECD-specific: `fred`, `prices` and `cot` have the same loop
and the same first-error-wins behaviour. And a partial result must be a status
with the lost series in a structured field, not prose on a completed line.

## Decision

**ADR 0013's three rules extend to series scope, unchanged.**

1. **The other series' observations are returned.** Nothing raised for one
   series may withhold what another served.
2. **Each failure is a status, not a swallowed exception and not detail text on
   a completed line.** The failed series is named by indicator and currency,
   with the error, and the refresh output prints it on its own line.
3. **A series that answers with no rows in the window is still a raised error
   for that series.** The lookback is years long. A series with nothing in it is
   dead or mis-keyed, and an empty success would score the currency as uncovered
   with nothing to say why.

**The shape: the collector asks a series-scoped source for one series at a
time.** The failure boundary moves into the path every source shares, and no
source has to report its own partial failures.

- `BaseDataSource` gains a class-level declaration of its failure scope, source
  or series, defaulting to source. `OecdSource` declares series. The exact name
  and type are the data engineer's.
- For a source declaring series scope, `_collect_one` calls
  `source.fetch([indicator], [currency], start, end)` once per routed pair, on
  the same instance, and catches each call exactly as it catches a whole-source
  fetch today. The `fetch` contract does not change: it returns observations or
  it raises. The per-series catch lives in one place and cannot be forgotten by
  a source.
- `SourceStatus` gains `PARTIAL`: asked, some series served, some failed.
  `SourceOutcome` gains a structured field of failed series, each carrying
  indicator, currency and the error text, empty by default. The status follows
  from the counts with no threshold. Every series served is `COMPLETED` with no
  failures. At least one served and at least one failed is `PARTIAL`. None
  served is `FAILED`, with the failures field populated and `detail` saying how
  many series were asked and failed. `COMPLETED` with a non-empty failures field
  is a constructed impossibility and a test says so.
- Rule 3 is enforced by the source, not by the collector. `OecdSource.fetch`
  raises `SourceError` when a series it was asked for yields no in-window rows.
  Whether an empty answer is a dead series or a genuine reading is a fact about
  the provider: COT documents an empty sequence as a reading, and the collector
  cannot know which it has. `OecdSource._decode` returning an empty tuple for a
  header-only body stays correct, because that is a statement about a body, not
  about a series.
- The refresh output prints a `PARTIAL` source's counts line as it would a
  completed one, marked partial, followed by one line per failed series naming
  the source, the currency, the indicator, the word `failed` and the error. The
  layout is the interface owner's, and `docs/interfaces.md` shows it.
- A `PARTIAL` result is usable. It does not change the exit code a refresh or a
  score command returns.

**`fbe --offline` follows from the shape, and the rule is the same.** With 38
of 39 series cached, the offline run serves the 38 and reports the 39th as a
failed series, because the cache miss raises `SourceError` inside that series'
own `fetch` call and nowhere else. The outcome is `PARTIAL`. No offline special
case is written.

**FRED gets the same treatment, through a separate issue.** `FredSource.fetch`
has the same loop, one request per series, and the same first-error-wins
behaviour. It should declare series scope. It is not in #275, because it carries
one complication the OECD does not: the refs whose transform has no `UNITS`
entry are passed over with `continue` (the #169 fix). Under series scope such a
ref returns an empty sequence, which rule 3 would turn into a daily failure line
for a series that was never going to be asked. The follow-up must decide
whether those refs stop being routed to FRED or become a named skip, and it
needs its own tests. `prices` and `cot` are not ruled here. COT derives the
dollar from the other seven contracts, so its series are not independent and
source scope may be the correct answer for it. The #275 pull request names which
of them still need a ruling, as the triage desk asked.

## Alternatives considered

**Keep the whole-source contract.** Rejected for the reasons ADR 0013 gives, and
the cost is now measured rather than argued: one series decided whether the day
had any tradeable pair, three times in two days.

**One source class per series.** ADR 0013's preferred shape, rejected here.
Thirty-nine classes for one endpoint, one rate limit and one availability answer.
`fbe doctor` would probe the same host 39 times. The unit of a source class stays
the provider; the unit of failure becomes the series.

**Catch inside `OecdSource.fetch`, return the partial result, log the rest.**
Rejected. This is the `except: pass` from the standards table wearing a logger,
and ADR 0013 rejected it for the same reason.

**Change `fetch` to return observations and failures together.** A new result
type on the `DataSource` protocol in `types.py`. Rejected. It is a breaking
change for every source, and it puts failure bookkeeping inside each source,
where one that forgets to fill the failures field produces a quiet partial. The
collector's loop cannot forget.

**The source records its failures on itself and the collector reads them after
`fetch`.** Rejected. A collector that does not read the attribute sees a
successful partial fetch, so the failure mode is silent.

**An exception subclass that carries the partial observations.** Rejected, as in
ADR 0013. `fetch` would both return and raise, and every source would need to
build the exception correctly.

**Ask every source for one series at a time, with no declaration.** Rejected.
COT's dollar series is derived from the other seven, `ManualSource` is asked for
the whole request by design, and COT documents an empty result as a reading.
Forcing series scope onto those would change their meaning, not just their
failure boundary. Opting in keeps each source's current behaviour until someone
has checked that series scope is true of it.

**`PARTIAL` with the per-series catch inside each source (ADR 0013's second
choice, literally).** This is the decision with a different channel. It still
needs one of the three rejected channels above to carry the failures from the
source to the collector. Moving the loop into the collector removes the channel
instead of designing one.

## Consequences

- `BaseDataSource` gains one declaration and `collect.py` gains a second call
  path, a status and a field. That is the seam the FRED follow-up lands in, and
  any later source with one request per series opts in with one line.
- The OECD makes the same number of requests as today, one per series, and the
  rate limiter still holds across them because it lives on the one instance.
  `elapsed_seconds` for a series-scoped source is the sum across its calls.
- `SourceOutcome.series` and `observations` on a `PARTIAL` line count what was
  served, so the line still reconciles with the cache.
- The bad consequence, carried over from ADR 0013: a degraded OECD now produces
  a normal-looking report with n/a or reduced coverage for one currency, where
  before it produced an obviously broken one. Rule 2 stands in for the lost
  noise. The refresh output names the series and the score table shows the
  absence. If that proves too easy to overlook, the fix is on the report, not a
  return to failing the source.
- A second accepted cost, stated on #275 by the triage desk: 38 of 39 series
  failing reads as `PARTIAL`, not `FAILED`. There is no threshold, so no free
  parameter to defend. Coverage makes that case visible on the page.
- A failed series is not retried beyond `RetryPolicy`. Retry counts and the
  OECD's own slowness are not in scope.
- `docs/data-sources.md` states the failure scope of every source, and
  `OecdSource`'s module and `fetch` docstrings say what fails loudly and at what
  scope.

## Status

Accepted, 2026-09-25. Ruled on #275, which carries the work. Extends ADR 0013 to
series scope without superseding it: ADR 0013 remains the ruling for sources
whose providers are separate institutions. The FRED follow-up needs its own
issue.
