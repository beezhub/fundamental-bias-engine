# 0016. The implementation lanes' claim key has a fallback

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
carried it. Nothing about a lane's competence excludes that work. The
implementation pool is the one that may span two owners' files, may change
`src/fbe/types.py` under a ruling, and is not barred from `p1`, which is
exactly what makes those issues claimable by a lane and not by the maintenance
desk.

## Decision

**The claim key gains a fallback. When no issue carrying `type:requirement`,
`roadmap` and `status:ready` has all its dependencies landed, the lane claims
instead a `status:ready` issue at any priority that**

- carries an architect ruling with acceptance criteria on the issue,
- has no open dependency,
- carries neither `routine-hold` nor `desk-only`,
- is not already `status:in-progress`,
- is not `p0`, and is not a `type:proposal`.

**The last two are narrower than the ruling, and are recorded here as open.**
The ruling said "at any priority" and argued one bar, `p1`, by the #97
argument. It did not reach `p0`, and it named no type, while the approval gate
on `type:proposal` has no agent override. Both are written as exclusions and
raised on #270 rather than resolved here, because the conservative reading
costs an idle slot and the wide one costs a pull request nobody asked for.

Lane A takes the lowest-numbered and lane B the highest, as in the roadmap
pool, so the two work from opposite ends of what is claimable. **The lane says
in its pull request body that it claimed from the fallback and why**, because
nothing else records that the roadmap pool was shut that day, and a reviewer
reading a defect fix from an implementation lane would otherwise have no way to
tell it from a lane ignoring its own pool.

**The ruling with criteria is the fence, and it is load-bearing.** Build lane 1
is barred from `p1` because a high-priority defect deserves a person deciding
who fixes it. That reasoning survives here rather than being set aside: the
architect is that person, and the ruling on the issue is the record of the
decision. A ready defect with no ruling is not in the fallback pool. This is
the argument #97 used to make `p1` safe for these lanes, which is that the gate
has already run and a person still merges.

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

**Let the maintenance desk take them instead.** Rejected. Those lanes are
barred from `p0` and `p1`, from two-owner diffs and from `src/fbe/types.py`,
which is most of what that list held. Widening the maintenance pool to fit
would move the fence rather than keep it.

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
- The rule lived only in a GitHub comment from 25 to 28 September while
  `docs/routines.md` described the key it replaced. Three lane runs claimed
  from it in that window, counted in the claim comment on #270 that opened the
  third. This record and the routines change land together so that cannot
  recur.

## Status

Accepted, 2026-09-25, ruled on #270, which carries the work.

**On the number.** The ruling named `docs/decisions/0015-*.md`. 0015 was free
when it was written on 25 September and was taken the same day by the
series-scope decision for #275, so this record cannot be 0015 without
overwriting a different accepted decision. 0016 is the next number free on
`main`, and `tests/test_adr_index.py` requires the sequence to have no gaps, so
0016 is what it has to be.

**#296's pull request also uses 0016**, for the source-declines-a-ref decision,
on a branch that has not merged. That collision is invisible to every test
here, which is the case `tests/test_adr_index.py`'s own docstring names: a file
on an unmerged branch is not detectable from `main`. Whichever of the two
merges second has to renumber to 0017 and move its index row. The number is the
only thing that changes; nothing in either ruling depends on it.

`tests/test_adr_index.py` states the invariant as "A record is never renumbered
and never deleted". That holds from the merge onward, which is what the test
can see. A record on an unmerged branch has not been published under its
number, so renumbering it before it lands is the thing that keeps the
invariant true afterwards rather than an exception to it.
