# 0005. GROWTH takes the OECD business confidence balance in place of the licensed PMI

Status: Accepted

## Context

Section 3.3 of `docs/scoring-spec.md` describes GROWTH as mixing "one lagging and
comprehensive measure (GDP), one leading survey (PMI), and two coincident
hard-data series", with sub-weights 0.30, 0.30, 0.20 and 0.20.

The leading survey slot is filled by `pmi_composite`, which is licensed. It has
no free feed for any G10 country, so `registry.coverage_report()` reports it at
0 of 8 and it carries a value only in months an operator keys eight numbers into
`data/manual/` by hand. On a run where nobody has, CHF, AUD and NZD hold exactly
0.50 of the pillar's sub-weight, and `MIN_COMPONENT_WEIGHT` compares with "at or
below" since #15, so all three are scored as absent rather than renormalised.
The heaviest leading input in the pillar is empty for everyone on almost every
run.

`docs/answers/data.md` question 5 established that the OECD Business Tendency
Surveys composite covers all eight currencies for free, monthly for USD, EUR,
GBP and CHF and quarterly for JPY, CAD, AUD and NZD. Issue #6 approved
registering it. Pull request #111 registers it as `business_confidence_mfg` with
unit `percentage_balance`, and places it in `UNCONSUMED_INDICATORS`, because
whether GROWTH consumes it is a separate question. Issue #23 is that question.

Two earlier shapes were considered on #23 and rejected on 2026-09-11: adding the
series for only the four currencies that publish monthly, and adding it for all
eight while the global staleness ramp was still in force. Both would have built
a component z-scored across four currencies for one half of the universe and
absent for the other, permanently. #8 has since landed, so each leg now carries
its own registry allowance and a quarterly leg is freshness-discounted rather
than absent, which removes the objection to covering all eight.

## Decision

GROWTH consumes `business_confidence_mfg` at a sub-weight of 0.30, in place of
`pmi_composite` rather than alongside it. The other three sub-weights do not
move and the four still sum to 1.0.

    before                        after
    gdp_yoy            0.30       gdp_yoy                    0.30
    pmi_composite      0.30       business_confidence_mfg    0.30
    indpro_yoy         0.20       indpro_yoy                 0.20
    retail_sales_yoy   0.20       retail_sales_yoy           0.20

`pmi_composite` stays registered and moves to `UNCONSUMED_INDICATORS`, so its
coverage figure stops reading as a live input and re-adopting it if it is ever
licensed is a one-line change.

No value in `ScoringConfig` changes. The pillar weight of 0.15 is untouched and
no sub-weight number changes. What changes is which canonical key occupies the
leading slot.

## Why substitution rather than addition

**The slot is defined by role, not by series.** Section 3.3 asks for one leading
survey. Both candidates are one. Changing which series fills the slot does not
change what the pillar measures, which is what keeps this an arbitration between
two data sources rather than a change to the pillar's economics.

**Addition would build an instability already rejected elsewhere in this
project.** Hold `pmi_composite` at any non-zero sub-weight and GROWTH carries a
component present only in months a chore was done. `blend_components`
renormalises over the sub-weight a currency actually has, so the same economic
reality yields a different score depending on whether an operator keyed the
numbers. The owner's approval on proposal #5 rejected the daily manual entry
path on precisely this ground: a chore done on most days and missed on some
produces intermittent presence, the cross-section changes composition day to
day, and the run-to-run diff shows moves that are composition rather than data.
That argument does not weaken when the chore is monthly instead of daily.

**The unit difference is handled one layer down, and is why a separate key was
correct.** A percentage balance is neutral at zero; a diffusion index is neutral
at 50. Each component is z-scored cross-sectionally before the blend, so a
constant offset is normalised away and the two series are comparable as
z-scores even though their raw values are not. Writing OECD values under the PMI
key would have been the unit error this project keeps finding, since the offset
would shift every currency by the same amount and the ranking would still look
orderly with nothing raising. Registering a distinct key with a distinct unit is
what makes the substitution safe at the blend.

## Alternatives considered

**Consume nothing, leave GROWTH as it is.** Costs nothing to decide and leaves
the pillar's leading input empty on almost every run, with three currencies
scored as absent on GROWTH outright. Rejected: a slot the spec describes as
carrying a leading survey, carrying nothing, is worse than one carrying a
survey with a known cadence limitation.

**Split the 0.30 between the two series.** Rejected for the intermittency
reason above, which applies at any non-zero PMI sub-weight.

**Add the series and rescale all four sub-weights.** Rejected: it changes three
numbers nobody has evidence to move, to accommodate a fourth component the
pillar was not designed to hold, and it keeps the intermittent component.

## Consequences

Four currencies carry a leading survey updated quarterly where four carry one
updated monthly. That asymmetry is real and permanent. Under the per-indicator
allowance from #8 the quarterly legs are discounted by their own freshness and
remain present in the cross-section, so the component is z-scored across all
eight rather than across four, which is the property that makes this acceptable.

The substitution is not measured. The correlation between the OECD balance and
the manual PMI cannot be computed, because the manual PMI file holds no values,
so there is no claim here that the two series rank the G10 the same way. The
claim is narrower: the slot's stated role is a leading survey, and one that is
present is worth more than one that is not.

A percentage balance may have different cross-sectional dispersion from a
diffusion index, so the component may speak at a different volume than the PMI
would have. `PillarScore.diagnostics["emit_sd"]` is where that becomes visible,
and issue #12 is the work that populates it.

This record is reopened if `emit_sd` shows the component materially quieter than
the 0.30 sub-weight implies, or if a free PMI feed appears, or if the two series
turn out to disagree about the cross-section once both have values.
