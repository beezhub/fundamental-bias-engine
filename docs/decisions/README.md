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
