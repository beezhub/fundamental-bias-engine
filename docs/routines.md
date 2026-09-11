# Routines

Seven scheduled runs keep the project moving without anyone at the keyboard:
deliver, triage, build, two implementation lanes, improve and audit. This file
is the authority for what each one may do. Where a routine's prompt and this
file disagree, this file wins, and the run says so.

Each routine is bound to its own host session, and that session's container is
cloned from `main` on every run, so the team definitions, the skills and this
file are present from the first command. The hosts are tagged `fbe-routine` in
the sessions list.

## Two pools, and why they do not overlap

The runs that write code draw from two separate pools, and an issue belongs to
exactly one of them.

**The maintenance pool** is `status:ready` + `routine-safe` + `p2` or `p3`.
Build lane 1 takes it. The work is bounded by construction: one owner's files,
no configured value, nothing in `types.py`, one pull request. It keeps the
scaffold honest, and it cannot build the engine.

**The roadmap pool** is `type:requirement` + `roadmap` + `status:ready`, at any
priority. The two implementation lanes take it. The work turns stubs into
working code, so it is wider by design: multi-file diffs, new fields on
`ScoringConfig` or `DataConfig`, anything under `src/fbe/` short of the
restrictions below.

**A roadmap issue is never labelled `routine-safe`.** That label is the
maintenance lanes' claim key, and applying it to roadmap work would hand
implementation work to a lane whose prompt forbids exactly the changes the work
requires. The lane would either refuse it, taking the issue out of the queue,
or attempt it and be stopped by its own bounds part way through. The deliver
run never applies the label and triage never adds it to anything carrying
`roadmap`.

The corollary matters as much: implementation work is routinely `p1`, and
priority does not gate the implementation lanes. Build lane 1 is still barred
from `p0` and `p1`, because a high-priority defect deserves a person deciding
who fixes it.

## The weekday schedule

Read down. Two runs must never share a slot, because a shared slot is how two
lanes end up racing for the same claim.

| SAST | Run |
| --- | --- |
| 05:00 | deliver |
| 06:00 | triage |
| 07:00 | build lane 1 |
| 08:00 | implement lane B |
| 09:00 | implement lane A |
| 11:00 | build lane 1 |
| 12:00 | triage, implement lane B |
| 13:00 | implement lane A |
| 15:00 | build lane 1 |
| 16:00 | implement lane B |
| 17:00 | implement lane A |

Saturday 08:00 is audit. Sunday 08:00 is improve.

Deliver runs first so the queue is refilled before anything tries to claim from
it. Triage runs an hour before the first build so that answers, the
`routine-safe` label and any split are in place. The 12:00 triage shares its
slot with implement lane B on purpose: triage writes labels and lane B reads
them, and a lane that reads a label one cycle stale claims a slightly worse
issue, which is not a failure.

## The runs

| Run | When (SAST) | Role | Does | Does not |
| --- | --- | --- | --- | --- |
| deliver | weekdays 05:00 | product-analyst | Counts open issues carrying `roadmap` and `status:ready` whose dependencies have all landed. Four or more: files nothing and stops. Fewer: decomposes the current phase of `docs/roadmap.md` into `type:requirement` issues, at most 5 in one run, aiming for roughly 6 unblocked. Posts the delivery order as a comment on the phase's lowest-numbered issue. | Apply `routine-safe`. File defects or proposals. Touch any source file. Change what a stub docstring says a function should do. |
| triage | weekdays 06:00 and 12:00 | architect | Releases stranded `status:in-progress` claims with no open pull request. Answers `status:needs-decision` in a comment. Unblocks what has landed. Spot-checks `status:ready`. Widens the maintenance pool by labelling qualifying `p2` or `p3` issues `routine-safe`. Splits anything too big for one pull request. Files at most 3 defects. | Touch source. Open proposals. Advance anything past the approval gate. Add `routine-safe` to anything carrying `roadmap`. |
| build lane 1 | weekdays 07:00, 11:00 and 15:00 | developer, then test-engineer, then code-reviewer | Looks after its own open pull requests first. Then claims **one** issue, the lowest-numbered that is `status:ready`, `routine-safe`, `p2` or `p3`. Branch, failing test, fix, four checks, pull request. | Take a second issue. Take an issue already at `status:in-progress`. Touch `types.py`. Change a value in `ScoringConfig` or `RiskConfig`. Take anything at `p0` or `p1`. Merge. |
| implement lane A | weekdays 09:00, 13:00 and 17:00 | developer, then test-engineer, then code-reviewer | Looks after its own open pull requests first. Then claims **one** issue carrying `type:requirement`, `roadmap` and `status:ready`, at any priority, preferring the **lowest** number whose dependencies have all landed. Tests first, each shown to fail against the stub for the right reason, then the implementation, four checks, pull request. | Take a second issue. Claim an issue whose dependencies are still open. Change an existing default in `ScoringConfig` or `RiskConfig`. Change `types.py` without an architect ruling in the issue body naming every consumer. Force-push. Merge. |
| implement lane B | weekdays 08:00, 12:00 and 16:00 | developer, then test-engineer, then code-reviewer | The same, preferring the **highest** number whose dependencies have all landed. | The same. |
| improve | Sunday 08:00 | product-analyst | Converts every `type:proposal` carrying `approved` into a `type:requirement`. Then files at most 3 new proposals at `status:needs-approval`. | Approve anything. Convert a proposal without the `approved` label. |
| audit | Saturday 08:00 | architect | Reads the whole repository against the standards and the specifications. Files at most 10 issues, verified present. Read-only checkout, writes nothing. | Fix anything. Refile something already open or already closed. |

The lanes within a pool take it from opposite ends so they do not race for the
same issue: build lane 1 and the two implementation lanes each prefer one end
of their own queue. Every lane re-reads the labels on GitHub immediately before
claiming, and none takes an issue already at `status:in-progress`, because a
lane cannot tell from its own transcript whether another lane claimed something
in the last hour. One issue per run, one pull request per issue, and a human
merges.

Routines bound to a host session send no push notifications, so the trail on
GitHub is the record of what a run did.

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

and, if `.claude/agents/` is missing, says so in one line and stops. The runs
that write nothing to the repository, deliver and audit, check out
`origin/main` detached instead, so nothing of theirs can be pushed anywhere.

## The delivery order

The phase's lowest-numbered issue carries a comment giving the current delivery
order: what can start now, what is blocked on what, and a one-line reason each.
Deliver posts it and refreshes it when it changes, rather than leaving a stale
one. Both implementation lanes read it before claiming.

A lane never claims an issue whose dependencies are still open, even when the
issue is labelled `status:ready`. A pull request built on an unlanded
dependency cannot be verified, and the lane would be writing against a stub it
has assumed the shape of. If every ready issue is blocked, the lane says which
and on what, and stops.

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

## Pull requests from a lane

A pull request is not finished when it is opened. It is finished when it
merges, and the lane that opened it owns it until then.

That means every lane works its own open pull requests **before** claiming
anything new:

- **Conflicted with `main`.** Merge `origin/main` into the branch and resolve.
  Never rebase, never amend, never force-push: a merge commit keeps the pull
  request's history intact and keeps anyone's checkout valid. Resolve by taking
  both intentions rather than picking a side, re-verify the branch still does
  what its own tests claim, run all four checks, push, and comment saying what
  conflicted and how it was resolved.
- **Red CI.** Read the failing job, reproduce it locally, fix it, show the same
  check passing, push. Never skip, disable or weaken a test to get green, and
  never push an empty commit to re-run.
- **Review comments.** Address small local asks and push. Reply on anything the
  lane is not doing, saying why.
- **Green and clean.** Leave it. Do not comment just to say so.

**Fixing a conflict or a red check on a pull request the lane already opened is
never a second issue**, and is always allowed. A run that spends its whole slot
getting its own pull requests back to green has done the right thing and says
so instead of claiming new work.

A human merges. A lane opens the pull request, drives it to green, and stops.
Nothing here merges on its own, because the one control that matters on a
repository that sizes real positions is that a person reads the diff.

## Stopping a run

Disable the routine. For one maintenance issue only, remove `routine-safe` and
build lane 1 will not claim it. For one roadmap issue only, move it off
`status:ready`. There is no other pause mechanism on purpose: two ways to stop
something means one of them will be forgotten.

## Bounds, restated

The `issue-workflow` skill defines what an unattended run may and may not do,
and it is the authority. In short: nothing touching `types.py` without an
architect ruling naming every consumer, nothing changing a configured
threshold, never more than a few issues in one run, never a proposal without a
human, and never a merge.
