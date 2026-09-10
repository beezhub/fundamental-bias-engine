# 0001. Estimate the blend divisor over recent runs

Status: Accepted

## Context

A pillar built from several sub-indicators blends z-scored components using
sub-weights. Because those components are imperfectly correlated, the blend has
a cross-sectional standard deviation below one, so a five-component pillar
arrives quieter than a one-component pillar whatever the weights in
`ScoringConfig` say. Measured on the worked example, the monetary pillar blended
to 0.700 while inflation reached 0.971, which makes their effective ratio about
1.44 to 1 rather than the 2 to 1 the config specifies.

A re-standardisation pass corrects this. The question was what to divide by.

## Decision

Divide by the median blend standard deviation over
`ScoringConfig.restandardisation_window_runs` recent runs, engaging only once
`min_restandardisation_runs` of history exists, and falling back to the run's own
cross-sectional standard deviation until then. Record which path was taken on
the score.

Centring stays run-local. Only the scale is estimated from history.

## Alternatives considered

**Divide by the current run's own standard deviation.** Simplest, self-contained,
needs no history, and was the original specification. Rejected because every
component is already forced to unit standard deviation cross-sectionally one
stage earlier, so the blend's dispersion does not track how similar the eight
economies are. It tracks the correlation between a pillar's own sub-indicators.
A run-local divisor therefore scales a pillar up precisely when its own
components disagree with each other, which is when it has earned less influence
rather than more, and nothing downstream would reveal it.

**Divide by the theoretical `sqrt(sum of squared sub-weights)`.** Fixed, needs no
history, no calibration file. Rejected on measurement: for the monetary pillar
that is 0.4583 against the 0.7001 actually observed, because it assumes the
components are independent when they are visibly correlated. It would scale the
heaviest pillar in the model by 2.18x instead of 1.43x, erring toward
over-amplification in the place that costs most.

**Do nothing.** Rejected. It leaves the configured weights inoperative, which is
the defect class this project has now found twice.

## Consequences

Good: a pillar's volume matches its weight, the scale no longer moves day to day,
and the clip stops interacting with a shifting divisor.

Bad: pillars now carry state across runs, which they did not before. Scores
computed under the fallback are not on the same scale as scores computed under
the rolling estimate, so a run-to-run comparison that spans the switch shows a
change the market did not make. That is why the path is recorded rather than
merely used.

Also bad: centring run-local while scaling is not looks like an inconsistency and
will attract a well-meaning cleanup. Centring is what makes a pillar a statement
about this run's cross-section, so it must come from this run. Scaling only
corrects for how many parts the pillar is built from, which is a property of the
pillar rather than of the day.

Unresolved: whether a median over 60 runs with a 20 run minimum is right. Both
numbers are conventions, neither is fitted, and both should be revisited once
there is real run history.

## References

`src/fbe/pillars/base.py::blend_divisor`, `docs/scoring-spec.md` section 2.3,
`docs/answers/scoring-maths.md`.
