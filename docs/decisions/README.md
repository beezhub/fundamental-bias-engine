# Architecture decision records

One file per decision, numbered in order and never renumbered. A decision that
lives only in a commit message is a decision that will be quietly reversed.

Write one when a decision is cross-cutting, when it was contested, or when
someone will later read the code and ask why it is like that. Do not write one
for a routine choice inside a single module.

Superseding is normal. Mark the old record superseded with a pointer to its
replacement and leave it in place. Reasoning that turned out to be wrong is
often more useful later than reasoning that was right.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-rolling-re-standardisation-divisor.md) | Estimate the blend divisor over recent runs | Accepted |
| [0002](0002-representing-not-known.md) | Absence is represented explicitly, never as a passing value | Accepted |
| [0003](0003-publish-effective-loadings-rather-than-remove-real-policy-rate.md) | Publish the effective loadings rather than remove `real_policy_rate` | Accepted |
| [0004](0004-yield-change-window-and-observation-unit.md) | The yield change window ends at the latest session, and an observation carries its indicator's unit | Accepted |
| [0005](0005-growth-takes-a-free-leading-survey.md) | GROWTH takes the OECD business confidence balance in place of the licensed PMI | Accepted |
