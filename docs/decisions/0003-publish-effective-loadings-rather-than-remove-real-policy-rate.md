# 0003. Publish the effective loadings rather than remove `real_policy_rate`

Status: Accepted

## Context

`MonetaryPillar` carries `real_policy_rate` at a sub-weight of 0.15, defined as
`policy_rate - cpi_yoy`. That places a coefficient of minus one on headline CPI
inside a pillar weighted 0.30. `InflationPillar` carries a positive loading on
the same series through `cpi_gap` at a sub-weight of 0.40 inside a pillar
weighted 0.15. The model holds two opposing loadings on one series and neither
appears in `ScoringConfig`.

Computed from the section 7 fixture's own cross-sectional dispersions, per
percentage point of headline CPI:

| Term | Composite loading |
| --- | --- |
| INFLATION, headline sub-indicator | +0.1008 |
| INFLATION, core sub-indicator, if core moves one for one with headline | +0.1666 |
| MONETARY, real policy rate | -0.0501 |

A move in headline alone cancels 49.7% of INFLATION's response. Headline and core
moving together cancels 18.7%. These are arithmetic from one run's dispersions
rather than a measurement, and the exact figures move run to run because every
standard deviation in them is recomputed daily. The direction and rough magnitude
do not.

Section 2.3 of `docs/scoring-spec.md` exists so that the declared weights are the
operative ones. This term breaks that guarantee for the pillar it touches, and
nothing in the output reveals it. The finding is recorded in section 10 item 1
and in `docs/answers/framework.md` Q1, in both cases as deliberately not acted
on, to be ruled on together with item 2. This record is that ruling.

## Decision

Change no pillar definition, no sub-weight and no `ScoringConfig` value. Publish
the effective per-series composite loadings alongside the declared pillar
weights, in section 3.1 and section 3.2 of the scoring spec, with the figures
above and a test that reproduces them from the fixture.

State in section 2.3 that the guarantee it provides is per pillar and does not
extend to a series appearing in two pillars with opposing signs. State in the
`ScoringConfig.weights` docstring that a declared weight is a pillar's share of
the blend and not the model's total loading on any one underlying series.

Invariant 4 of the architecture, that declared weights must be the operative
weights, is satisfied in the only form available before Phase 6: the reader can
see what the model actually weighs. The defect was never that the model holds an
opposing term. It is that the published weights conceal one.

## Alternatives considered

**Remove `real_policy_rate` from MONETARY.** Restores a clean one-loading-per-
series model and removes the cancellation entirely. Rejected because it deletes
the only term in the engine that catches a central bank tolerating an overshoot,
and the mechanism visibly works: on the section 7 fixture JPY has the fastest
rising two-year yield in the universe, at +1.459 and +1.528, and still finishes
MONETARY at -0.31, pulled there by a real policy rate of -2.30%. Removing it
changes the heaviest pillar in the model with nothing measured behind the change,
which is the pattern `docs/roadmap.md` forbids until Phase 6.

**Adopt the front-end response gate in INFLATION instead.** Section 10 item 1
establishes that the gate and the real rate term are two implementations of one
correction and should be chosen between rather than stacked, so this is the other
half of the same choice rather than an addition. Rejected because choosing
between them requires measurement that does not exist, and because the gate
introduces two free parameters, the `0.10pp` scale and the pivot rule, that
nobody can currently defend. The KISS section of the engineering standards names
this case: a conditional is a free parameter wearing a disguise.

**Raise INFLATION's weight to compensate for the cancellation.** Restores the
intended net loading without touching either pillar's construction. Rejected for
two reasons. It hides a structural property of the model inside a weight, which
is the same concealment in the other direction, and the weight would then be
wrong for any run where core and headline move together, since the cancellation
is 49.7% in one case and 18.7% in the other and a single weight cannot serve
both. It is also a change to a `ScoringConfig` number with no evidence behind it.

**Substitute core CPI for headline inside `real_policy_rate`.** Considered and
dismissed quickly. It does not reduce the cancellation, it relocates it onto the
0.60-weighted core sub-indicator, where the opposing loading would be larger.
`MonetaryPillar._transform` also gives a reason for headline that survives: the
deposit rate a saver compares against is the one that includes food and fuel.

## Consequences

Good. The model is unchanged, which is the right default when no evidence
separates the alternatives, and a reader can now recover the net loading from the
documents rather than by deriving it. A test pinning the three figures means a
future change to `component_weights` that moves the cancellation fails the build
rather than passing silently, which is the specific way this went unnoticed until
now.

Bad, and the honest cost. The model still contains an inflation response roughly
half the size its weights imply when headline moves alone, and this record does
not fix that. It documents it. Someone reading `ScoringConfig` and seeing
INFLATION at 0.15 will still form the wrong first impression, and will only be
corrected if they read section 3.2. Documentation is a weaker remedy than
construction and it is being chosen because the stronger remedies all require
changing a number nobody has measured.

The published figures are also fixture-specific. They will drift as the
cross-section changes and the test will need a tolerance rather than an equality,
which invites the tolerance being widened rather than the figures being
recomputed.

This decision is arbitrary in the narrow sense that matters: between keeping the
real rate term and adopting the gate, no evidence available today separates them,
and one was kept because it is the one already in the model. That is recorded as
an arbitrary choice, not as a finding.

## Reopening

Reopen with evidence, not with a preference, and reopen together with section 10
item 2 as that item already requires. The measurement that would separate the
gate from the real rate term is a comparison of the two constructions against
subsequent pair returns, which needs the forward record Phase 6 produces.
`docs/answers/scoring-maths.md` Spec 8 is worth reading first: it shows that two
constructions which rarely disagree cannot be separated at this cadence within
any reasonable horizon, and the same arithmetic may well apply here.

## References

- Issue #9, which carries the acceptance criteria.
- `docs/scoring-spec.md` section 10 items 1 and 2.
- `docs/answers/framework.md` Q1, which produced the loading table.
- `docs/answers/scoring-maths.md` Spec 2, on the sub-weights this must be ruled
  with.
