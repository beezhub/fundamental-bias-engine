# 0017. The implementation lanes' claim key has a fallback

Status: Accepted

## Context

The two implementation lanes claim an issue carrying `type:requirement`,
`roadmap` and `status:ready`, and never one whose dependencies are still open.
That second rule is not a formality: a pull request built on an unlanded
dependency cannot be verified, because the branch is written against a stub
whose shape the author has assumed.

On 24 and 25 September both lanes stopped at step 2 on consecutive days. The
pool read as four claimable issues by label and held zero by the rule:

| Issue | Waits on | State of the blocker |
| --- | --- | --- |
| #253, #261 | #252 | open, claimed, pull request #267 open and green |
| #262 | #251, #252 | #251 landed, #252 as above |
| #265 | #263, #264 | #263 open, pull request #268 open and green; #264 open and claimed, with no pull request |

`#211` was also ready and carried `routine-hold`, which no lane may take.

The diagnosis on #270 is the part worth keeping: **the bottleneck is not a
shortage of filed work, it is that a dependency only counts as landed when a
human merges.** Two green, unreviewed pull requests were holding three of
those four issues shut: #267 held all three on its own, through #252. The
fourth waited on #268 and on #264, which had no pull request at all. Filing
more work does not help, because the phases are sequential by
construction: each deliverable reads or renders what the one before it
computes, which is the same reason the no-unlanded-dependency rule exists.

Counted on the same morning, `status:ready` issues outside the roadmap pool
with no unlanded dependency of any kind: ten, nine of them carrying an
architect ruling with acceptance criteria. Both lanes idled with that list in
front of them, because the claim key requires `roadmap` and none of them
carried it. Nothing about a lane's competence excludes that work, but not
all of it is open to a run with nobody watching. The `issue-workflow` skill,
which `docs/routines.md` names as the authority, lets an unattended run fix a
`status:ready` issue at `p2` or `p3` whose diff stays inside one owner's files,
and bars `p0`, `p1`, `src/fbe/types.py` and proposals. Some of the ten fit
inside that bound and some did not.

## Decision

**The claim key gains a fallback. When no issue carrying `type:requirement`,
`roadmap` and `status:ready` has all its dependencies landed, the lane claims
instead a `status:ready` issue that**

- is `p2` or `p3`,
- carries an architect ruling with acceptance criteria on the issue,
- if it is a `type:requirement`, links its human approval,
- names one owner and its diff stays inside that owner's files,
- needs no change to `src/fbe/types.py` and no value changed in
  `ScoringConfig` or `RiskConfig`,
- has no open dependency,
- carries neither `routine-hold` nor `desk-only`,
- is not already `status:in-progress`,
- is not a `type:proposal`.

**The fallback never goes past the skill.** The ruling on #270 said "at any
priority" and let the pool span two owners' files. The owner narrowed it before
it merged: an implementation lane is an unattended run, and the skill bounds
every unattended run to `p2` or `p3` work inside one owner's files. A ruling in
this directory cannot widen the skill, because the skill is the authority this
directory defers to.

**`p1` and cross-owner work stay with a session a person starts**, such as
`/next 172`. Whether an unattended lane may take `p1` is the open question on
#313, and it is decided there, not here.

**A requirement must link its human approval.** That is the skill's definition
of ready for a `type:requirement`, and the fallback checks it rather than
trusting the `status:ready` label, because the fallback reaches issues that no
other claim key has already checked.

Lane A takes the lowest-numbered and lane B the highest, as in the roadmap
pool, so the two work from opposite ends of what is claimable. **The lane says
in its pull request body that it claimed from the fallback and why**, because
nothing else records that the roadmap pool was shut that day, and a reviewer
reading a defect fix from an implementation lane would otherwise have no way to
tell it from a lane ignoring its own pool.

**The ruling with criteria is a fence, and it is not a person deciding.** A
ready issue with no ruling is not in the fallback pool, because without
acceptance criteria a lane cannot show it did what was asked. The ruling does
not stand in for a person, though: the architect also runs as an unattended
routine, so a ruling can be written with nobody watching. That is why the
priority and owner bounds stay the skill's, and why a ruling does not open
`p1` to the fallback.

**Stopping stays the floor.** If the fallback is empty too, the lane says which
issues are blocked and on what, and stops. A lane that claims an unruled issue
because nothing else was left has removed the fence.

**Deliver counts landed dependencies, not labels.** `docs/routines.md` already
said so, and the sentence is correct as it stands. What was missing is what
deliver does when the current phase is fully decomposed and the count is still
under four: it files nothing and stops. Decomposing further would widen a queue
that is already longer than the merge rate.

## Alternatives considered

**Let a lane branch from an unmerged pull request's head when that is its only
open dependency.** Rejected, and it is the tempting one, because it unblocks
immediately and costs nothing on the day. It trades away the reason the
delivery order exists: the dependency can still change under review, and the
second pull request then has to be rebuilt or it merges something nobody
verified. The rebase debt lands on whoever reviews the second one, which is the
same person who has not yet reviewed the first.

**File more issues.** Rejected. The queue is already longer than the merge
rate, and decomposing a sequential phase produces a longer chain rather than
parallel work. The morning this was filed, the delivery run had decomposed
correctly and still left the aim unmet.

**Accept the idling.** Kept, but as the floor rather than the answer. It is
honest and cheap and it leaves throughput equal to the owner's merge rate. What
made it the wrong first answer is that it was being taken while nine ruled,
unblocked issues sat claimable by nobody.

**Let the maintenance desk take them instead.** Rejected for the part of the
list that fits the skill's bound. Build lane 1 claims only issues carrying
`routine-safe`, which triage applies, and the ruled issues on that list did not
carry it. On the day this was filed build lane 1 was working and the
implementation lanes were idle, so the fallback uses slots that were going
unused. The part of the list that does not fit, `p1` and two-owner work, is
reachable by neither, and stays with a session a person starts.

**Let the fallback reach `p1` and two-owner work, as the ruling first said.**
Rejected by the owner. It would let an unattended run do what the skill bars,
on the strength of a ruling that may itself have been written unattended. If
that bound should move, it moves in the skill, on #313.

## Consequences

- The roadmap advances more slowly on a day the fallback fires, because a lane
  slot goes to a defect. On such a day the roadmap was advancing at zero
  anyway, and the defect had typically been ready for days.
- A reader of the pull request list sees implementation lanes producing defect
  fixes. The declaration in the body is what makes that legible.
- The fallback pool is not maintained anywhere. It is whatever the board holds
  at claim time, which is deliberate: a second list would drift from the first.
- A ready issue with no architect ruling is claimable by nobody when the
  maintenance desk's own key does not reach it. That is the same gap this
  record widens the lanes to cover, one step further out, and it stays a
  labelling question for triage rather than something a lane decides.
- A ruled, unblocked issue at `p1`, or one whose diff spans two owners, is
  still claimed by no routine. It waits for a person to start a session on it,
  and #313 is where that may change.
- The rule lived only in a GitHub comment from 25 to 28 September while
  `docs/routines.md` described the key it replaced. Three lane runs claimed
  from it in that window, counted in the claim comment on #270 that opened the
  third. This record and the routines change land together so that cannot
  recur.

## Status

Accepted, 2026-09-25, ruled on #270, which carries the work. Narrowed by the
owner before it merged, so that the fallback stays inside the `issue-workflow`
skill's bound on unattended runs.

**On the number.** The ruling named `docs/decisions/0015-*.md`. 0015 was free
when it was written on 25 September and was taken the same day by the
series-scope decision for #275, so this record cannot be 0015 without
overwriting a different accepted decision.

**This record was written as 0016 and renumbered to 0017 when it merged
second.** #296's source-declines-a-ref decision took 0016 first. The collision
was invisible to every test while both were unmerged, which is the case
`tests/test_adr_index.py`'s own docstring names: a file on an unmerged branch
is not detectable from `main`. The number is the only thing that changed and
nothing in either ruling depends on it.

`tests/test_adr_index.py` states the invariant as "A record is never renumbered
and never deleted". That holds from the merge onward, which is what the test
can see. A record on an unmerged branch has not been published under its
number, so renumbering it before it lands is what keeps the invariant true
afterwards rather than an exception to it.
