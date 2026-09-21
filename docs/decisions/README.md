# Architecture decision records

One file per decision, numbered in order and never renumbered. A decision that
lives only in a commit message is a decision that will be quietly reversed.

Write one when a decision is cross-cutting, when it was contested, or when
someone will later read the code and ask why it is like that. Do not write one
for a routine choice inside a single module.

Superseding is normal. Mark the old record superseded with a pointer to its
replacement and leave it in place. Reasoning that turned out to be wrong is
often more useful later than reasoning that was right.

A record lands the way every other change lands: on a branch, through a pull
request, merged to `main`, and indexed in the table below in the same change.
Five records once sat on branches with no pull request for weeks, invisible from
this index because it only lists what is here. `tests/test_adr_index.py` now
fails when this table and the directory disagree in either direction, so the
gap shows on the first record that strands rather than the fifth.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-rolling-re-standardisation-divisor.md) | Estimate the blend divisor over recent runs | Accepted |
| [0002](0002-representing-not-known.md) | Absence is represented explicitly, never as a passing value | Accepted |
| [0003](0003-publish-effective-loadings-rather-than-remove-real-policy-rate.md) | Publish the effective loadings rather than remove `real_policy_rate` | Accepted |
| [0004](0004-yield-change-window-and-observation-unit.md) | The yield change window ends at the latest session, and an observation carries its indicator's unit | Accepted |
| [0005](0005-growth-takes-a-free-leading-survey.md) | GROWTH takes the OECD business confidence balance in place of the licensed PMI | Accepted |
| [0006](0006-limit-inputs-sources-and-breach-behaviour.md) | Where the limit inputs come from, and what a breached limit does | Accepted |
| [0007](0007-a-revision-must-carry-its-release-date.md) | A revision must carry its release date, and the assumed lag dates only an original print | Accepted |
| [0008](0008-a-thin-blend-has-no-cross-section.md) | Fewer than three currencies above the component floor means the pillar has no cross-section | Accepted |
| [0009](0009-model-names-stay-out-of-docs.md) | A model name in docs is limited to the product dependency, and process configuration is stated by reference | Accepted |
| [0010](0010-the-registry-supplies-the-denominator.md) | The registry supplies a scaled quantity's denominator, and a key never mixes global and per-currency refs | Accepted |
| [0011](0011-positioning-reads-leveraged-funds.md) | POSITIONING reads leveraged funds, as a percent of open interest | Accepted |
| [0012](0012-available-answers-about-configuration.md) | `available()` answers about configuration, never about reachability | Accepted |
| [0013](0013-a-provider-inside-a-fan-out-source-is-a-named-gap.md) | A provider inside a fan-out source is a named gap, not the end of the source | Accepted |
| [0014](0014-the-staleness-ramp-is-derived-from-the-leg.md) | The staleness ramp is derived from the leg's own publication lag and cycle, and the hand-keyed allowance goes | Accepted |
