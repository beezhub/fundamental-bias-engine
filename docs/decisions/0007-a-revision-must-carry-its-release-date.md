# 0007. A revision must carry its release date, and the assumed lag dates only an original print

Status: Accepted

## Context

`BasePillar._extract` applies two rules so that a historical run sees only what
was knowable at the time.

The visibility rule, `BasePillar._visible` in `src/fbe/pillars/base.py`: an
observation carrying `released_at` is visible when `released_at.date() <= asof`;
one without it is visible when
`period + DEFAULT_PUBLICATION_LAG_DAYS[frequency] <= asof`.

The vintage rule, `BasePillar._newest_vintages` in the same module: where a
source republishes a period, take the highest `revision` among the observations
visible at `asof`, breaking ties on the later `released_at`.

Each is correct alone. Together they admit a later vintage at the original
print's date, because the fallback expression takes `period` and `frequency` and
nothing else, so every vintage of one period becomes visible at the same instant
and the vintage rule then picks the newest of them.

Build desk B found this while implementing #116 and filed it as #121, with a
reproduction: `cpi_yoy` for period 2026-06-01 at revision 0 value 2.0 and
revision 2 value 3.5, both unstamped, `asof` 2026-07-16, returns 3.5. Carried
into `MonetaryPillar._transform` against a policy rate of 4.25, that gives a
`real_policy_rate` of 0.75 where a July run could only have known 2.25: 1.5
percentage points inside a component holding 0.15 of the heaviest pillar, in the
flattering direction.

The engine is therefore honest exactly when a source bothers to stamp its data
and flatters when it does not, which is the wrong way round.

`ManualSource` in `src/fbe/datasources/manual.py` is the live route in.
`released_at` is optional and `revision` is parsed independently, so a hand-typed correction
carrying `revision: 2` and no timestamp produces the shape exactly. It is also
the only route: `manual.py` is the only module in `src/fbe/` that sets `revision`
at all, and every other source takes the default of 0.

## Decision

**An observation with `revision` greater than zero must carry `released_at`.**
Enforced where observations are built, so the shape never reaches a pillar. A
source that cannot supply a release date may not express a revision.

`ManualSource` raises on load for a row with a revision above zero and no
`released_at`, naming the file and the row.

**`BasePillar._visible` treats an unstamped observation with `revision` greater
than zero as not visible**, as a second line rather than as the primary defence,
so that a pillar does not depend on upstream validation having run and both
halves fail in the same direction.

**`DEFAULT_PUBLICATION_LAG_DAYS` states that it dates an original print and
cannot date a revision.** That sentence is what was missing, and its absence is
why two individually correct rules could be jointly wrong.

## Why not the resolution proposed on the issue

#121 proposed refusing an unstamped revision and falling back to the vintage
that certainly existed, on the grounds that it is conservative in the right
direction: it can cost a backtest a correction it could legitimately have seen,
and can never fabricate one it could not.

That is right for a backtest and wrong for a live run. `_extract` cannot tell the
two apart, so the refusal would apply on every run, and on a live run the newest
vintage is the only one that matters. A source that republishes a corrected
figure without a timestamp would have that correction silently ignored for ever,
and the engine would score today's currency off a superseded number while
reporting nothing.

That trades a flattered backtest for a wrong number in the live path, which is
the worse of the two by this project's own ordering. Refusing the shape at the
source boundary avoids the trade: the correction is neither used early nor
dropped silently, it is rejected with a message, and the operator supplies the
date.

## Alternatives considered

**Leave it and document it.** Rejected: the failure is silent, systematic and
always in the flattering direction, which is the combination the prime directive
exists for.

**Derive a revision's date by adding a second, longer lag.** Rejected: the lag
between a print and its revision is not a property of the frequency, it varies by
statistics office and by how large the correction is, and inventing a constant
for it would be a free parameter nobody can defend.

**Validate inside `Observation` in `src/fbe/types.py`.** Rejected here, though it
is the natural home for the rule, because that module is required to carry no
computation and a validating constructor is computation. The contract sentence
still belongs on `Observation.released_at`'s docstring, which is a docstring-only
additive change under the human gate and is noted on #121 with its consumers
listed.

## Consequences

A free source that later republishes revisions without timestamps cannot be
ingested as revisions until someone gives it a date rule. That is a real
constraint, accepted deliberately: a source that cannot say when it published
something cannot support a backtest, and pretending otherwise is the defect this
record fixes. The escape hatch is per-source and explicit, a documented rule that
derives `released_at` for that source, rather than a silent fallback.

The cost today is one validation path in `manual.py`, because no other source
emits a revision.

An operator who types a correction without a date gets an error instead of a
silent misdating. That is the intended trade: a loud failure at the point where
the missing fact is still recoverable.

Implementation is issue #121, which was still open when this record reached
`main`. The rules above are the ruling it builds to.

This record is reopened if a source appears whose revisions are genuinely
undateable and materially improve coverage, or if the visibility rule is replaced
by something that carries per-vintage dates directly.
