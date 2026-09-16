# 0008. Fewer than three currencies above the component floor means the pillar has no cross-section

Status: Accepted

## Context

A pillar score is produced in two standardisation stages.

Stage 3, `BasePillar.cross_sectional_z`, z-scores each component across the
currencies that hold it. It refuses a cross-section below `MIN_CROSS_SECTION`,
which is 3, and its own constant says why: with two values the z-score is
`+/-1.0` whatever the gap between them, which encodes rank and discards
magnitude.

Stage 4, `BasePillar.blend_components`, combines the components into one score
per currency and re-standardises across the currencies that hold the pillar. It
applied no equivalent guard.

The two stages thin the cross-section by different mechanisms, so stage 4 can
fall below three after stage 3 has passed. Stage 3 runs per component and
refuses a component. `MIN_COMPONENT_WEIGHT` runs per currency and refuses a
currency. A run where every component clears three usable currencies can still
leave two currencies clearing the floor.

Build desk B found this while implementing #115 and filed it as #130. Reproduced
against `main` at `e9ead61`, with GROWTH's shipped sub-weights and no history:

    gap of 0.1 in gdp_yoy   ->  {'USD': 1.0, 'EUR': -1.0, 'GBP': None, 'JPY': None}
    gap of 6.0 in gdp_yoy   ->  {'USD': 1.0, 'EUR': -1.0, 'GBP': None, 'JPY': None}

A sixty-fold difference in separation produces identical output. One currency
above the floor returns `{'USD': 0.0}`, and `0.0` is the value this repository
reserves for "every usable currency reported the same value", which is a
finding rather than an artefact.

Because `z` is not `None` in either case, `scoring.coverage` credits the
pillar's full weight and the composite receives the number as evidence.

## Decision

When fewer than `MIN_CROSS_SECTION` currencies clear `MIN_COMPONENT_WEIGHT`,
`blend_components` returns `None` for every currency. The pillar takes the
absence path it already has, so coverage falls, `composite` renormalises around
it, and conviction demotes through `coverage_demotion`.

`MIN_CROSS_SECTION` is reused rather than a second threshold introduced. It is a
module constant in `pillars/base.py` and not a `ScoringConfig` field, so no
configured value changes.

## Why

**Keeping the score was not available.** ADR 0002 rule 1 requires that absence
never be represented by a value inside the normal range that a consumer could
mistake for a reading. A `+/-1.0` on the `-3..+3` band is inside that range, and
it arrives at full weight. Recording the caveat in the specification does not
change what the aggregator does with the number, so documenting it would have
been the defect written down rather than fixed.

**Stating it as a property of the cross-section rather than of a currency.** The
artefact is not currency-specific: when two currencies remain, both receive
`+/-1.0`, so refusing "only where the artefact bites" refuses exactly the same
set. Expressing the rule per cross-section is what makes it explicable. "Fewer
than three currencies clear the floor, so the pillar has no cross-section this
run" is a sentence a reader can act on; a per-currency framing sends the next
reader looking for a currency-level condition that does not exist.

**One threshold, not two.** The reason is identical at both stages: a two-point
standardisation cannot express magnitude. How the cross-section became thin,
per component at stage 3 or per currency at stage 4, does not change that. A
second constant would be the same number in two places, which this project has
already been bitten by.

**A discount was rejected.** Keeping the score at reduced weight, so a thin
cross-section speaks quietly rather than not at all, needs a discount factor
nobody can defend from evidence. The standards call a conditional of that kind a
free parameter wearing a disguise, and stage 3 already set the precedent of a
hard refusal for this artefact.

## Consequences

On a run where a pillar's component floor knocks out six currencies, the pillar
drops for all eight, including the two that held complete data. On the registry
as it stands this is a live GROWTH configuration rather than a hypothetical.

That is the intended outcome. A cross-sectional score is a claim about where a
currency sits relative to the others, and `CLAUDE.md` opens on the point: there
is no such thing as a strong currency in isolation, only one stronger than the
currency it is quoted against. Two currencies do not constitute a cross-section
for a G10 relative-value model. The two with complete data have not lost
information about themselves, they have lost the comparison, and the comparison
is the product.

The loss is visible rather than silent, which is what makes it acceptable:
coverage falls, the report shows it, and conviction demotes. A pillar that
reports it could not see the cross-section is behaving correctly.

Nothing reaches this path today, because `BasePillar.compute` is still
scaffolded. It becomes reachable when `compute` lands.

This record is reopened if a pillar is found whose components thin so often that
the rule costs the model more than the artefact would, which would be an
argument about that pillar's component set rather than about this threshold.
