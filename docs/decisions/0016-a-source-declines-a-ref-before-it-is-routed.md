# 0016. A source declines a ref it cannot serve before the collector routes it

Status: Accepted

## Context

ADR 0015 moved FRED towards series scope and left one question open. The #169
fix makes `FredSource.fetch` pass over any ref whose `transform` has no `UNITS`
entry. Today that is `yield_2y_chg_1m` and `yield_2y_chg_3m` for USD, which
reuse `DGS2` with a transform no source computes yet (ADR 0004). Under series
scope the collector would ask for each of those refs alone. `fetch` would
return nothing for them, and rule 3 of ADR 0015, a series that answers with
nothing is a raised error, would then print two failure lines every morning for
series nobody was ever going to request.

The curve source has the same pattern. `SERVED_TRANSFORMS` holds only `level`,
and `fetch` passes over the change refs in the same way. It is source-scoped
per provider, so it has no daily noise today. The next source to opt into
series scope would have the noise.

Issue #296 offered three options. A fourth came up during the ruling, and one
tempting shortcut had to be checked and rejected.

## Decision

**`BaseDataSource` gains `serves(ref: SeriesRef) -> bool`, defaulting to
`True`. The collector's `_routed_pairs` routes a pair only when the source's
own `refs()` claims it and `serves` accepts its ref.** A declined ref is never
asked for, so it produces no request, no observation and no failure line, on
either call path.

- `FredSource.serves` answers `ref.transform in UNITS`. `fetch` uses the same
  predicate in place of its inline check, so the routing and the fetch cannot
  disagree about what FRED serves. The #169 behaviour of `fetch` stays: called
  directly with a declined ref, it makes no request.
- `FredSource` declares `failure_scope = "series"`. `fetch` raises
  `SourceError` when a series it requested returns no observation in the
  window. That is rule 3, enforced by the source as ADR 0015 requires.
  `_parse_observations` returning an empty list stays correct, because that is
  a statement about a response body, not about a series.
- **The hook is tied to the registry by a test.** For every source in
  `ALL_SOURCES`, every ref in its `refs()` that `serves` declines must have
  `fetchable` False in the registry. So a declined ref is always a named gap in
  `stale_refs`, and so in `CollectionResult.gaps`, and never counts as
  coverage. A source that declines a ref the registry calls verified fails the
  test rather than dropping the series quietly. The reverse is not required:
  the dead FRED series below are unverified and still served.
- The curve source may adopt the hook with `ref.transform in
  SERVED_TRANSFORMS`. That is not required by #296, because under source scope
  the curves have no noise to remove.

## Alternatives considered

**Stop routing those refs to FRED in the registry.** Rejected. The change refs
are built from `YIELD_2Y.series` by `_yield_change_series` precisely so they
cannot drift from the level they are derived from, and the USD leg is FRED's
`DGS2`. Pointing that leg at a different source, or dropping it, turns "declared
and not yet derivable" into "no source at all". Those are different facts, and
the first is the one the ADR 0004 work needs to find. It would also break the
single derivation for one currency.

**Route on the existing `SeriesRef.fetchable`.** This is the tempting shortcut:
the change refs already carry `verified=False`, so the registry seems able to
say "declared but not fetchable" today. Rejected, because `verified=False`
means two things in this registry. FRED also carries three refs that are
unverified because the series is dead but still answers: `retail_sales_yoy`
for AUD (`SLRTTO01AUQ659S`), `indpro_yoy` for EUR (`DEUPRINTO01GYSAM`), and
every leg of `current_account_gdp`. FRED requests all of them today and their
observations reach the pillars, subject to the staleness ramp. Filtering on
`fetchable` would silently stop that, which changes scored inputs. That is a
scoring question with its own owner, not a failure-scope question.

**`fetch` raises a distinguishable error and the collector prints a named
skip.** Rejected. It needs a new exception type, a new branch in
`_collect_by_series` and a new line format. It still prints something every
morning about a series that was never requested. It also makes `fetch` raise
for a pair the source knew in advance it would not serve, which is a question
about routing answered in the wrong place.

**Enforce rule 3 only for requested refs and treat a passed-over ref as an
empty success.** Rejected. Behaviour on the day would match this ruling, but
`fetch([indicator], [currency])` returning nothing would then have two meanings
for FRED. The collector would count a declined ref as an asked series that
completed with nothing. That is the empty success rule 3 exists to forbid,
allowed by a comment. A later change to the skip condition would widen it
without any test noticing.

**Do nothing until ADR 0004's derivation lands.** Rejected. The derivation has
no date, and FRED's first-error-wins loop has already cost MONETARY, GROWTH,
EMPLOYMENT and EXTERNAL together once (#169).

## Consequences

- The collector gains one predicate call per claimed pair, and the base gains
  one method. No change to `types.py`: the `DataSource` protocol does not gain
  `serves`, just as it did not gain `failure_scope`.
- `SourceOutcome.detail`'s "N of M series failed" counts only routed pairs.
  That is the honest denominator, since a declined ref was never asked for.
- The gap report is unchanged. `CollectionResult.gaps` comes from
  `stale_refs`, which reads the registry, not the routing, and the test above
  keeps every declined ref in it. What goes is the refresh output's mention of
  those refs, and there was none before either: under source scope FRED passed
  over them without a word.
- The bad consequence: a source now has a way to not be asked about a ref, and
  a hook that means "don't ask me" is the shape a quiet drop takes. The
  registry test is the guard. If a later source needs to decline a ref the
  registry calls verified, that is a new decision, not an edit to the test.
- A source whose every claimed ref is declined reads `skipped` with "no series
  routed to it". No current source is in that state.
- The three dead FRED series will start failing by name once the lookback
  window passes their last print, because rule 3 now applies to them. That is
  correct, and it is the first time the refresh output will say so.
- When a source implements the ADR 0004 derivation, the same commit widens its
  `serves` and flips the change refs' `verified` back, as
  `_yield_change_series` already says.

## Status

Accepted, 2026-09-26. Ruled on #296, which carries the work. Extends ADR 0015
and does not supersede it.
