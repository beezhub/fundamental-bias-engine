# Routines

Four scheduled runs keep the backlog moving without anyone at the keyboard.
This file is the authority for what each one may do. Where a routine's prompt
and this file disagree, this file wins, and the run says so.

Every run is a fresh session. Nothing from a previous run sits in its
transcript, so it cannot mistake last week's state for today's. Everything it
needs to know it reads from GitHub and from this repository at the start.

## Getting current

The container clones `main`. Until the integration branch is merged, `main`
does not contain the team definitions, the skills, or this file, so every run
starts with:

    git fetch origin claude/anton-kreil-trading-system-4yj5yk
    git checkout -B work origin/claude/anton-kreil-trading-system-4yj5yk

Once that branch is merged, the checkout becomes a no-op and the line can go.
A run that cannot find `.claude/agents/` after the checkout says so in one line
and stops.

## The four runs

| Run | When (SAST) | Role | Does | Does not |
| --- | --- | --- | --- | --- |
| triage | weekdays 06:00 | architect | Answers questions on `status:needs-decision` issues in a comment. Moves an issue to `status:ready` when it meets the definition of ready. Files at most 3 defects it finds while reading. | Touch source. Open proposals. Advance anything past the approval gate. |
| build | weekdays 07:00 | developer, then test-engineer, then code-reviewer | Claims **one** issue: `status:ready`, `routine-safe`, `p2` or `p3`. Branch, failing test, fix, four checks, pull request. | Take a second issue. Touch `types.py`. Change a value in `ScoringConfig` or `RiskConfig`. Merge. |
| improve | Sunday 08:00 | product-analyst | Converts every `type:proposal` carrying `approved` into a `type:requirement`. Then files at most 3 new proposals at `status:needs-approval`. | Approve anything. Convert a proposal without the `approved` label. |
| audit | Saturday 08:00 | architect | Reads the whole repository against the standards and the specifications. Files at most 10 issues, verified present. Read-only checkout, writes nothing. | Fix anything. Refile something already open or already closed. |

Triage runs an hour before build so answers are in place before anyone tries to
claim work.

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
