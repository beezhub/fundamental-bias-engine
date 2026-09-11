# Routines

Four kinds of scheduled run keep the backlog moving without anyone at the
keyboard: triage, build, improve and audit. This file is the authority for what
each one may do. Where a routine's prompt and this file disagree, this file
wins, and the run says so.

Each routine is bound to its own host session, and that session's container is
cloned from `main` on every run, so the team definitions, the skills and this
file are present from the first command. The hosts are tagged `fbe-routine` in
the sessions list.

## Reused sessions, stale transcripts

A host session is reused across runs, which means earlier turns sit above the
current run in its transcript. They are from a previous run and they are stale:
issues have been closed, answered, labelled or fixed since. Every run rebuilds
its picture from GitHub at the start and trusts nothing it only remembers. It
never refiles something because the transcript shows it filing that before, and
never assumes a check still fails because it failed last time.

## Getting current

The container clones `main`. A run confirms that with

    git rev-parse --abbrev-ref HEAD

and, if `.claude/agents/` is missing, says so in one line and stops.

## The runs

| Run | When (SAST) | Role | Does | Does not |
| --- | --- | --- | --- | --- |
| triage | weekdays 06:00 and 12:00 | architect | Answers questions on `status:needs-decision` issues in a comment. Moves an issue to `status:ready` when it meets the definition of ready. Widens the unattended pool by labelling qualifying `status:ready` issues at `p2` or `p3` `routine-safe`, with a comment giving the reason. Splits any ready issue too big for one pull request into child issues and sets the parent to `status:blocked`. Files at most 3 defects it finds while reading. | Touch source. Open proposals. Advance anything past the approval gate. |
| build, lane 1 | weekdays 07:00, 11:00 and 15:00 | developer, then test-engineer, then code-reviewer | Claims **one** issue, the lowest-numbered that is `status:ready`, `routine-safe`, `p2` or `p3`. Branch, failing test, fix, four checks, pull request. | Take a second issue. Take an issue already at `status:in-progress`. Touch `types.py`. Change a value in `ScoringConfig` or `RiskConfig`. Merge. |
| build, lane 2 | weekdays 09:00, 13:00 and 17:00 | developer, then test-engineer, then code-reviewer | Claims **one** issue, the highest-numbered that is `status:ready`, `routine-safe`, `p2` or `p3`. Branch, failing test, fix, four checks, pull request. | Take a second issue. Take an issue already at `status:in-progress`. Touch `types.py`. Change a value in `ScoringConfig` or `RiskConfig`. Merge. |
| improve | Sunday 08:00 | product-analyst | Converts every `type:proposal` carrying `approved` into a `type:requirement`. Then files at most 3 new proposals at `status:needs-approval`. | Approve anything. Convert a proposal without the `approved` label. |
| audit | Saturday 08:00 | architect | Reads the whole repository against the standards and the specifications. Files at most 10 issues, verified present. Read-only checkout, writes nothing. | Fix anything. Refile something already open or already closed. |

The two build lanes take the backlog from opposite ends so they do not race
for the same issue. Both re-read the labels on GitHub immediately before
claiming, and neither takes an issue already at `status:in-progress`, because
a lane cannot tell from its own transcript whether the other lane claimed
something in the last two hours. One issue per run, one pull request per
issue, and a human merges.

Each triage runs an hour before the next build so that answers, the
`routine-safe` label and any split are in place before a lane tries to claim
work. Routines bound to a host session send no push notifications, so the trail
on GitHub is the record of what a run did.

## How a human approves a proposal

Add the `approved` label. That is the whole mechanic.

The improve run converts it into a requirement issue with acceptance criteria,
links the two, and moves the proposal to `status:in-progress`. The proposal
closes when the requirement does. Nothing else advances a proposal, including
a comment saying "looks good", because a label is unambiguous and a comment is
not.

## What every run must do

- **Leave a trail.** A comment on every issue it touched saying what it did and
  what it chose not to do. A run that leaves no trace is indistinguishable from
  one that did nothing.
- **Put questions on GitHub.** A question that exists only in the run's final
  report will not be read for days. It goes on the issue, with
  `status:needs-decision`.
- **End with a clean working tree.** Work belongs on a pushed branch or
  nowhere. An abandoned run discards its changes rather than leaving them for
  the next run to trip over.
- **Stop early when the bar is not met.** No qualifying issue, a failing check
  it cannot fix, a definition of ready not satisfied: say so in one paragraph
  and stop. Doing nothing correctly is a valid outcome.

## Pull requests from the build run

A human merges. The build run opens the pull request, drives it to green, and
stops. Nothing here merges on its own, because the one control that matters on
a repository that sizes real positions is that a person reads the diff.

## Stopping a run

Disable the routine. For one issue only, remove `routine-safe` and it will not
be claimed. There is no other pause mechanism on purpose: two ways to stop
something means one of them will be forgotten.

## Bounds, restated

The `issue-workflow` skill defines what an unattended run may and may not do,
and it is the authority. In short: nothing touching `types.py`, nothing
changing a configured threshold, nothing above `p2`, never more than a few
issues in one run, never a proposal without a human.
