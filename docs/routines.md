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

## Who did what

Every run is signed. The commits all carry the repository owner's name, and a
pull request list that does not say which run produced each row cannot be read
at a glance, so the identity has to be written into the artefact itself.

Each run has a desk name and a label, both fixed:

| Run, as its own prompt opens | Desk | Label |
| --- | --- | --- |
| "Scheduled delivery run" | planning desk | `run:planning` |
| "Scheduled triage run" | triage desk | `run:triage` |
| "Scheduled build run, lane 1" | maintenance desk | `run:maintenance` |
| "Scheduled implementation run, lane A" | build desk A | `run:build-a` |
| "Scheduled implementation run, lane B" | build desk B | `run:build-b` |
| "Scheduled audit run" | audit desk | `run:audit` |
| "Scheduled improvement run" | proposals desk | `run:proposals` |

The first column is how each run's own prompt introduces it, so a run can
find its own row without being told which one it is. A prompt that matches no
row means the schedule has changed and this file has not: say so and sign as
the run name in the prompt rather than guessing a desk.

A run applies its own label to every issue it files and every pull request it
opens, and to nothing else. It never removes another desk's label, because the
label records who produced the thing rather than who last touched it.

A run opens every issue comment and every pull request body it writes with one
line naming itself and when it ran:

    **Build desk A**, 2026-09-11 09:00 SAST.

That line is the first thing in the body, before the template's first heading.
Two runs can work the same issue on the same day, and without the timestamp the
order they did so in is guesswork.

The desk names are seats, not people, and a seat is held by whichever run is
scheduled into it. Nothing else goes in that line: no tool, no model, no agent
name, per the writing rules in `CLAUDE.md`.

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
| triage | weekdays 06:00 and 12:00 | architect | Releases stranded `status:in-progress` claims with no open pull request. **Rules** every `status:needs-decision` issue that is not the owner's to decide, and never leaves one longer than two days. Unblocks what has landed. Spot-checks `status:ready`. Widens the maintenance pool by labelling qualifying `p2` or `p3` issues `routine-safe`. Splits anything too big for one pull request. Files at most 3 defects. | Touch source. Open proposals. Advance anything past the approval gate. Add `routine-safe` to anything carrying `roadmap`. |
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

## Working on it yourself

The seven runs above are unattended. When the owner sits down to the project in
their own time, two skills in `.claude/skills/` do the same work with them at
the keyboard:

| Skill | Does |
| --- | --- |
| `/status` | Where the project is, in plain language, in under a minute. Reads only. |
| `/next` | Picks up one issue and takes it to a pull request. `/next 62` takes that one. |

`/next` follows the same rules as the build desks and this file is its
authority too. The differences are only the ones that follow from a person
being present: it proposes one issue rather than listing eight, it asks before
touching `src/fbe/types.py` or a configured value rather than refusing outright,
and it stops and says so rather than guessing.

Two things it must not do, the same as the desks: it does not merge, and it does
not decide anything from the four kinds above that belong to the owner.

A session at the keyboard and a scheduled run can collide, since the desks run
on weekdays. Both claim by label and neither takes an issue already at
`status:in-progress`, which is what keeps them apart. Claim before building, not
after.

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

## What goes to the owner, and what does not

The owner is a working developer, not an economist. The `issue-workflow` skill
carries the rule in full and it is the authority. In short, four kinds of
decision are theirs: facts only they hold, their own habits and appetite,
whether a thing is built at all, and what merges. Everything else belongs to
the desk, and the architect rules it rather than parking it.

**A `status:needs-decision` issue that is not one of those four is the
architect's, and triage may not leave it unanswered for more than two calendar
days.** The ruling goes on the issue, an ADR goes in `docs/decisions/` when the
decision is cross-cutting, and the issue moves to `status:ready`. "I would
rather a human decided" is not a ruling.

**The same test applies to anything a lane writes on a pull request.** A run
that finds itself listing options for the owner, or writing "both yours", or
"I have no preference strong enough to argue for", has found a decision and
handed it over instead of taking it. Those phrases are the tell. Unless the
question is one of the four kinds, the lane rules on it, says why, and names
the cost of being wrong.

Merge mechanics are the clearest case and the one that went wrong. Which merge
button to press, whether a commit's closing keyword should be struck from a
squash message, which tracking state an issue is left in: none of these is a
fact only the owner holds, their own appetite, whether to build a thing, or
what merges. "What merges" is the decision to merge this diff at all, not the
bookkeeping that follows from it. A lane that cannot rule on its own
bookkeeping should not be opening pull requests.

This rule exists because it went wrong. #56, #58 and #59 each merged their work
and then sat for two days on one question about a differencing convention,
which nobody but the quant analyst and the data engineer could have answered.
Three issues stopped, and the person they were waiting on could not have
answered it.

When a decision genuinely is the owner's, it reaches them as five lines with no
jargon, a recommendation, and a default that applies if they say nothing:

```
What:            one sentence.
Why you:         which of the four kinds this is.
Options:         A or B, in plain words.
Recommendation:  A, and the one reason why.
If no reply:     A happens on <date>.
```

Every decision here is reversible, so a default that moves is better than a
question that stops the work.

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
- **Sign what it produces.** Its desk line on every comment and pull request
  body, its `run:` label on every issue it files and pull request it opens. See
  "Who did what".
- **Put questions on GitHub.** A question that exists only in the run's final
  report will not be read for days. It goes on the issue, with
  `status:needs-decision`.
- **End with a clean working tree.** Work belongs on a pushed branch or
  nowhere. An abandoned run discards its changes rather than leaving them for
  the next run to trip over.
- **Stop early when the bar is not met.** No qualifying issue, a failing check
  it cannot fix, a definition of ready not satisfied: say so in one paragraph
  and stop. Doing nothing correctly is a valid outcome.

## Writing a pull request description

The description is read by the owner, who merges it. They did not write the
code, they are not a specialist in whatever it touches, and they are reading it
between other work. A description that opens on the thing the lane found most
interesting is a description they have to decode before they can act.

So the body has a fixed shape, after the desk signature line:

```
**Maintenance desk**, 2026-09-15 07:22 SAST.

## What you need to do

Merge it normally. Nothing here needs a decision from you. CI is green.

## What this changes, in plain words

...

---

## Technical record

...
```

**"What you need to do" comes first and is one or two sentences.** Usually it is
"merge it normally". When it is not, it says exactly what is different and why.

**"In plain words" means a reader outside the specialism can follow it.** Name
the thing that was wrong and what it would have cost. A term the owner would
have to look up is either replaced or explained where it first appears. The
test is whether someone who has never opened the file can say what changed and
why it mattered.

**Everything else goes below the rule, under "Technical record".** Acceptance
criteria, the estimator, the mutation table, the four checks: all of it stays,
because it is the record and a reviewer wants it. It just stops being the first
thing anyone reads. Nothing is deleted to meet this; it is moved.

**Never head a section "For the reviewer" without saying who the reviewer is.**
On this repository the owner merges, so an unaddressed note reads as though it
is for them and describes work they cannot evaluate. Either address them, or
say which desk it is for and why it is in a document they read.

This is not a style preference. #118 opened on `ddof`, sample versus population
estimators and `1 / sqrt(2)`, and closed with an unaddressed "For the reviewer"
section. The owner could not tell whether the pull request was written for them
or for another desk, which is the whole question a description exists to
settle. The change underneath it was one comment and thirteen lines.

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
