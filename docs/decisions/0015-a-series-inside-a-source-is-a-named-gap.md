# 0015. A series inside a source is a named gap, not the end of the source

Status: Accepted

## Context

ADR 0013 settled the question one level up: a provider inside a fan-out source
is a named gap, not the end of the source. It works there because each curve
provider is a different institution with its own base URL, its own name and its
own registry key, so each is a source class and `fbe doctor` can probe each.

#275 is the same argument one level down, for the series inside a single
provider. On 2026-09-24, twice about two hours apart, `fbe refresh` recorded the
OECD as failed with zero observations. Probed by hand at the same moment, the
Japanese policy rate answered HTTP 500 after 30 seconds and the German long
yield answered HTTP 200 in under six. So "the OECD is down" was false and the
source said it anyway.

The next day the same shape was measured further. The OECD answered 38 series
and cached every one of them, then one series failed after its retries and the
whole source was recorded as failed. The offline refresh afterwards refused on
that one missing entry, so the 38 cached bodies were unusable too. Coverage fell
from 64-96% to 34-51% and every pair went neutral, on one series.

Checked on `main` at `fe647e2`: none of `fred`, `oecd`, `prices` or `cot`
catches a per-series failure inside `fetch`. In all four the first series to
fail after its retries propagates out and costs every series the source was
asked for. The OECD is where it was seen because the OECD is the provider
having a bad day.

ADR 0013's own remedy does not transfer. The OECD's legs share one provider,
one base URL, one rate limit and one `available()`. One source class per leg
would be dozens of classes for one endpoint. The unit of failure is the series;
the unit of a source class stays the provider.

## Decision

**One series failing costs that series, by name, and nothing else. The
narrowing lives in the collector, not in the source.**

- `BaseDataSource` declares a failure scope, `FailureScope.SOURCE` or
  `FailureScope.SERIES`, defaulting to source. `OecdSource` declares series.
- For a series-scoped source, `collect` calls `fetch` once per routed
  `(indicator, currency)` on the same instance, and catches each call the way
  it catches a whole fetch today.
- `fetch` keeps its contract: it returns or it raises. No source has to report
  its own partial failures, so none can forget to.
- `SourceStatus` gains `PARTIAL`. `SourceOutcome` gains a structured field of
  failed series. All served is `COMPLETED`, some served is `PARTIAL`, none
  served is `FAILED` with the field populated. There is no threshold.
- ADR 0013's rule 3 stays in the source: `OecdSource.fetch` raises when a
  series it was asked for has no rows in the window. The collector cannot know
  whether empty means dead, and `fbe.datasources.cot` documents empty as a
  reading.

## Consequences

A morning like 2026-09-24 costs one currency one component instead of every
OECD-backed indicator for every currency. The refresh line says which series
went and why, so the operator reads it there rather than by diffing two days of
coverage.

The status is a member rather than prose on `COMPLETED` because a completed
outcome carrying its losses in `detail` is the ADR 0002 shape: nothing can
group it, and the refresh line reads as success. A partial run exits 0, since
most of the cache is filled and the scorer has something to read.

A whole outage still reads as one. Thirty named gaps beside a source that did
not fail is honest and useless: it buries the one fact the operator needs. The
cost of having no threshold is accepted and is real: 29 series of 30 failing
reads as 29 gaps and one success. Coverage carries that case, falling to near
nothing on the page, and the report shows it.

Scoring is cross-sectional, so a component computed over seven currencies
instead of eight has a different mean and spread and every currency's score on
it moves a little. Measured on the fixture in `tests/test_collect.py`, losing
one currency's policy rate moves two of the other seven by 0.01 on the -3..+3
band and leaves five unchanged. That residual is the model working as designed.
What must not happen, and no longer does, is another currency losing its score
or its coverage.

Rule 3 reasons about the default window, which is
`ScoringConfig.lookback_years`. A window narrower than a series' own period is
outside that reasoning: `fbe refresh --since` a few days ago asks every
quarterly series for a span it cannot hold a print in, and each is then named
as dead. The observations are unaffected and the empty bodies cache under their
own period keys, so the cost is a false cause on the refresh line and a wasted
run rather than a wrong number. Filed rather than guessed at, because a gate on
the window is a second rule and the wrong one would restore the silent empty
success.

`prices` and `cot` are not ruled on, and `cot` is worth a look: it issues one
request per contract code, so it has the shape this record is about. FRED needs
the same treatment and not in #275: the refs its `fetch` passes over with
`continue`, from the #169 fix, would become a daily failure line under rule 3,
and that needs its own decision.
