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

**Both bounds are on the change, not on the file.** A lane may correct a
docstring in `src/fbe/types.py` or in a config type. It may not change a field,
a type, an argument order, a default or a number. The bar exists because a
moved number silently re-prices every score or every position and nobody reads
a diff that changes one character in a float, and because renaming a field in
`types.py` breaks every consumer at once. A docstring does neither: it is the
description of the contract, not the contract. Read as a file bound it costs
the pool a class of work it is well suited to, in exchange for protecting
against nothing, which is why #11, #40 and #182 sat unclaimable. Ruled on #154.

**A filing desk that wants an issue kept out of the maintenance pool applies
`desk-only`.** Triage never adds `routine-safe` to an issue carrying it, the
same way it never does to `roadmap` or `routine-hold`, so the three exclusions
are one list and a lane matches on all of them.

The label exists because prose does not work here. #71 is the case: it ends
with a section headed "Why this is not `routine-safe`", arguing that its two
halves belong to different owners and would drift if split, and it carried
`routine-safe` anyway from 14 September. Build lane 1 could have claimed it at
any of the nine weekday slots since, against the written reasoning of the desk
that filed it, because **a lane matches on labels and does not read the closing
section**. It was caught by luck. A filing desk writing that paragraph today
applies `desk-only` as well, and the paragraph becomes the reason rather than
the mechanism.

`routine-hold` cannot take this job, and that is a ruling rather than a
preference. Its defining sentence is "Only a person adds or removes it", so a
filing desk applying it would break the one property that makes it legible:
seeing it on an issue tells you a human stopped this deliberately. Overloaded,
it would mean either that or "a desk thinks this needs two owners", with
nothing to tell them apart, which is this section's own defect one level up in
the control meant to fix it.

**Where an issue argues against the label and carries no `desk-only`,** triage
either applies `desk-only` itself or adds `routine-safe` with a comment that
answers the argument. Never silently, and never a comment that merely notes it.
"Labelling anyway, the two-owner concern is stale because the templates landed
in #226" is a decision. "Labelling anyway, noted" is silence with extra words.

**`desk-only` has no effect until the routine prompts carry it.** The label
lives in this repository and the behaviour lives in four routine definitions
outside it, which change only by recreating the routine. Until triage, build
lane 1 and both implementation lanes have been recreated with the rule, adding
`desk-only` marks the issue on the board and stops nothing. The same is still
true of `routine-hold`, and this is not hypothetical: the build lane 1 prompt
names `roadmap` in its exclusion rule and says nothing about `routine-hold`,
so a run that followed its prompt rather than this file would claim a held
issue. Recreating those four routines is the step that makes either label
live, and only the owner can take it.

**A `type:requirement` converted from an approved proposal carries `roadmap`,
and carries no `phase:N`.** The implementation lanes then claim it like any
other work in their pool, at any priority. `roadmap` is the claim key for that
pool rather than a statement about which document the work came from, which is
how the rule above the corollary already uses it. The phase label is omitted
rather than guessed: work that came from a proposal did not come from a phase of
`docs/roadmap.md`, and stamping one on it would put a false provenance on the
issue to satisfy a label schema. The `run:` label and the approval link in the
issue body already record where it came from.

That the human gate has already run is what makes this safe at `p1`. A person
approved the proposal, the proposals desk wrote the acceptance criteria, and a
person still merges the pull request. Ruled on #97.

**A `type:requirement` carrying neither `roadmap` nor `routine-safe` belongs to
no pool and nothing will claim it.** That is not a resting state, it is a
labelling mistake, and the fix is to decide which pool it belongs in rather than
to leave it at `status:ready` where the board reads it as queued. Five issues
sat in it for a week, descended from proposals the owner had approved, while
eleven lane slots a weekday passed over them and every lane correctly reported
no qualifying issue. Nothing errors when this happens, which is why it is
written down here.

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
| triage | weekdays 06:00 and 12:00 | architect | Releases stranded `status:in-progress` claims with no pull request referencing the issue, **open or merged**. Reports a claim whose pull request has merged for closure instead of releasing it. **Rules** every `status:needs-decision` issue that is not the owner's to decide, and never leaves one longer than two days. Unblocks what has landed. Spot-checks `status:ready`. Widens the maintenance pool by labelling qualifying `p2` or `p3` issues `routine-safe`. Splits anything too big for one pull request. Files at most 3 defects. | Touch source. Open proposals. Advance anything past the approval gate. Add `routine-safe` to anything carrying `roadmap`, `routine-hold` or `desk-only`. Override an issue's own argument against the label without a comment answering it. |
| build lane 1 | weekdays 07:00, 11:00 and 15:00 | developer, then test-engineer, then code-reviewer | Looks after its own open pull requests first. Then claims **one** issue, the lowest-numbered that is `status:ready`, `routine-safe`, `p2` or `p3`. Branch, failing test, fix, four checks, pull request. | Take a second issue. Take an issue already at `status:in-progress` or carrying `routine-hold` or `desk-only`. Change a field, a type, an argument order or a default in `src/fbe/types.py`. Change a value in `ScoringConfig` or `RiskConfig`. Take anything at `p0` or `p1`. Merge. |
| implement lane A | weekdays 09:00, 13:00 and 17:00 | developer, then test-engineer, then code-reviewer | Looks after its own open pull requests first. Then claims **one** issue carrying `type:requirement`, `roadmap` and `status:ready`, at any priority, preferring the **lowest** number whose dependencies have all landed. Tests first, each shown to fail against the stub for the right reason, then the implementation, four checks, pull request. | Take a second issue. Claim an issue carrying `routine-hold`, or one whose dependencies are still open. Change an existing default in `ScoringConfig` or `RiskConfig`. Change `types.py` without an architect ruling in the issue body naming every consumer. Force-push. Merge. |
| implement lane B | weekdays 08:00, 12:00 and 16:00 | developer, then test-engineer, then code-reviewer | The same, preferring the **highest** number whose dependencies have all landed. | The same. |
| improve | Sunday 08:00 | product-analyst | Converts every `type:proposal` carrying `approved` into a `type:requirement`. Then files at most 3 new proposals at `status:needs-approval`. | Approve anything. Convert a proposal without the `approved` label. |
| audit | Saturday 08:00 | architect | Reads the whole repository against the standards and the specifications. Files at most 10 issues, verified present. Read-only checkout, writes nothing. | Fix anything. Refile something already open or already closed. |

**The release test is "no pull request, open or merged."** A merged pull request
means the claim was honoured, so releasing the issue sends a lane back over
finished work. That has happened, to #16 and #20, and it cost most of a
maintenance slot. The check is one command:

```
git log main -E --grep="#NN\b"
```

**The right-hand boundary is the whole of the command.** Without it, `--grep`
takes `#2` as a prefix and matches every commit mentioning #20, #24 or #201. On
`main` today that reports 54 commits of finished work behind #2, which has none,
and the conclusion the paragraph draws from a hit is that the issue is finished.
Every issue filed in the 200s adds another false match for #2 and #20. The `-E`
pins the dialect rather than enabling the boundary: `\b` is a GNU extension and
works here without it, verified on git 2.43.0 in the run container. If a git
build ever rejects it, `-E --grep="#NN([^0-9]|$)"` is the POSIX form and gives
the same answer.

Nothing found means the claim really is stranded and triage releases it.
Something found means the work landed and the issue is waiting to be closed, not
to be reclaimed: triage reports it for closure and leaves the label alone. Only a
person closes an issue, so the report is the whole of what triage does here.

**A `type:proposal` is never released by this test.** A proposal at
`status:in-progress` is a parent waiting on its children, and the workflow
forbids implementing one directly, so it has no pull request of its own and
never will. "Nothing found" therefore carries no information about it. Read
literally, the paragraph above would move every approved proposal into the
claimable pool, which is the one direction the `issue-workflow` skill says has
no agent override: a lane matching on labels would find a `type:proposal`
offered as work. Five sit in exactly that state today and only judgement has
kept them there.

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

An ADR is a change to `main` and lands like one: on a branch, through a pull
request that indexes it in `docs/decisions/README.md`, linked from the issue it
rules on. A branch pushed and left is not a record. Five were left that way and
the folder the project treats as authoritative was missing them for weeks, which
is issue #151. Until the pull request merges, the ruling on the issue is the
authority and the ADR is a draft.

**The same test applies to anything a lane writes on a pull request**, not only
to an issue at `status:needs-decision`. The `writing` skill lists the phrases
that give it away and settles merge mechanics, which is the case that went
wrong: "what merges" is the decision to merge the diff at all, not the
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

The `writing` skill in `.claude/skills/` is the authority and carries the shape,
the jargon test and the worked example. Load it before writing a description, an
issue, or a comment on either.

In short: the owner merges everything, so they read every description first,
whatever else it is for. What they need to do comes first, in one or two
sentences. What changed goes next, in words a reader outside the specialism can
follow. Everything else, acceptance criteria included, goes below a horizontal
rule under "Technical record", moved rather than deleted.

The one run-specific addition: the desk signature line still comes before the
first heading, per "Who did what" above.

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

## The model a run uses

**Every routine host has its model set explicitly at creation, and never
inherits it.** A host created without one takes whatever the creating session
happened to be running, which is silent, survives every later run, and surfaces
only when a bill or a change in behaviour makes it obvious. Setting it at
creation is what makes inheritance a mistake rather than the default.

**The per-run models live in `.claude/agents/*.md`**, in the `model:` field of
each definition's frontmatter. That is the operative setting and the only place
it is written down. The judgement-heavy roles and the rest are deliberately not
the same, and which is which is visible by reading those twelve files.

**The bound, stated by reference:** no host runs a model below the one the
judgement-heavy agent definitions name in `.claude/agents/*.md`.

By reference rather than by name, and that is not a shorthand. Writing the name
here would put a second copy of a value in a second place, maintained by hand,
with nothing holding the two together, which is the disagreement this project
keeps finding in its own config. It would also be the first model name in
`docs/` outside `docs/reasoning-layer.md`, where naming one describes what the
engine depends on to run rather than how it gets built.
`tests/test_model_policy.py` fails if either half stops being true.

**Recreating a host means passing the model, never relying on the caller.** A
routine's prompt can only be changed by recreating it, so recreation is ordinary
rather than exceptional, and it is exactly the moment the setting is lost. Read
the `model:` field first and pass it; do not assume the session doing the
recreating is running the right thing.

## Stopping a run

Disable the routine to stop it altogether.

**For one issue only, add `routine-hold`.** No unattended run claims an issue
carrying it, in either pool, and triage never adds `routine-safe` to one. Only
a person adds or removes it.

That is the only per-issue pause, and the two this section used to document are
gone. Removing `routine-safe` no longer pauses a maintenance issue. Triage adds
that label back when it widens the pool, and nothing it is handed can
distinguish a label never applied from one a person removed an hour earlier, so
the pause ended at 06:00 the next weekday without anyone being told. Moving a
roadmap issue off `status:ready` had the same shape. A control that appears to
work and does not is worse than no control, and two ways to stop something
still means one of them will be forgotten.

**The label has no effect until the routine prompts carry the rule.** The label
lives in this repository. The behaviour lives in four routine definitions
outside it, and a prompt changes only by recreating its routine. Until triage,
build lane 1, implement lane A and implement lane B have each been recreated
with the rule in their prompts, adding `routine-hold` marks the issue on the
board and stops nothing. Recreating those four routines is the step that makes
it live, and only the owner can take it.

## Bounds, restated

The `issue-workflow` skill defines what an unattended run may and may not do,
and it is the authority. In short: nothing touching `types.py` without an
architect ruling naming every consumer, nothing changing a configured
threshold, never more than a few issues in one run, never a proposal without a
human, and never a merge.
