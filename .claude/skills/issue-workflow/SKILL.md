---
name: issue-workflow
description: How work enters, moves through and leaves this repository. Issue types and labels, the definition of ready, the human approval gate on proposals, what a routine agent may pick up unattended, branch and pull request rules, and the definition of done. Load before opening an issue, before picking one up, before opening a pull request, and whenever running unattended on a schedule.
---

# Issue workflow

Work moves in one direction, and nothing skips a stage.

```
  audit / review / idea
          |
          v
   +--------------+   questions?   +------------------+
   | type:defect  | -------------> | status:needs-    |
   | type:debt    |                | decision         |
   +--------------+                +------------------+
          |                                 |
          |                        architect answers
          |                                 |
          v                                 v
   +---------------------------------------------+
   |              status:ready                   |  <-- the only state a
   +---------------------------------------------+      developer may claim
          |
          v
   branch -> tests -> implementation -> pull request -> review -> merge
```

Proposals take one extra stage, and it is not optional:

```
  type:proposal  -->  status:needs-approval  -->  HUMAN APPROVES
                                                        |
                                                        v
                                             type:requirement, status:ready
```

## Issue types

| Label | Means | Opened by |
| --- | --- | --- |
| `type:defect` | Something is wrong. Code, spec, or a contradiction between them. | Anyone, usually architect or code-reviewer |
| `type:debt` | Not wrong, but will cost later. Missing test, stale doc, duplicated constant. | Anyone |
| `type:question` | A decision is needed before work can start. | Anyone |
| `type:proposal` | An improvement or enhancement. **Never implemented directly.** | product-analyst |
| `type:requirement` | An approved proposal, written as buildable requirements. | product-analyst, after human approval |

## Status labels

Exactly one per issue, always.

| Label | Means |
| --- | --- |
| `status:needs-decision` | Blocked on an answer. The architect owns clearing this. |
| `status:needs-approval` | A proposal awaiting a human. **No agent may advance this.** |
| `status:ready` | Every question answered, acceptance criteria present. Claimable. |
| `status:in-progress` | Claimed. The claimant's name is in a comment. |
| `status:blocked` | Started, then stopped. The comment says on what. |

Area labels (`area:data`, `area:scoring`, `area:risk`, `area:execution`,
`area:interface`, `area:reasoning`, `area:infra`) route to an owner in
`docs/team.md`. Priority is `p0` through `p3`, where `p0` means a wrong number
is reaching the trader right now.

## Which decisions are the owner's

The owner is a working developer, not an economist and not a trading
specialist. A question they cannot answer from their own life is not theirs,
and parking it in front of them stops the work without producing an answer.
That has happened: three roadmap issues sat blocked for two days on a
differencing convention, which is a question for the quant analyst and the data
engineer.

**Four kinds of decision are the owner's, and nothing else is.**

1. **Facts only they hold.** Their broker's contract specification, their
   account balance, what their platform actually offers.
2. **Their own habits and appetite.** Whether they review the economic calendar
   every morning, how much risk they will carry, when they trade.
3. **Whether to build a thing at all.** This is the approval gate on
   `type:proposal` and it has no agent override.
4. **What merges.**

Everything else belongs to the desk. Transformations, units, thresholds,
conventions, which name is canonical, how a failure is represented, what a
docstring should say, which pull request lands first: the specialists in
`docs/team.md` own these, the architect arbitrates, and the answer is recorded
rather than asked.

**A `type:defect`, `type:debt` or `type:question` never needs the owner's
approval.** Only `type:proposal` does. Asking for approval on the other three
is a mistake that has been made and it costs the owner time for nothing.

### The two-day rule

A `status:needs-decision` issue that is **not** one of the four kinds above is
the architect's to rule on, and it may not sit unanswered for more than two
calendar days.

When the architect rules, it records the ruling on the issue, writes an ADR in
`docs/decisions/` if the decision is cross-cutting or would otherwise live only
in a comment, and moves the issue to `status:ready`. "I would rather a human
decided" is not a ruling. Ruling with the reason stated and the cost named is
what the role is for, and a ruling can be reopened with evidence later.

The ADR lands through a pull request, the same way as any other change: a
`docs/` branch, the record plus its row in `docs/decisions/README.md`, the pull
request linked from the issue. Pushing the branch is not the last step. Until
the pull request merges, the comment on the issue is the ruling and the ADR is
a draft nobody can find from `main`.

If the question **is** one of the four kinds, the architect says so plainly in a
comment, names which kind, and leaves the label. It then goes to the owner in
the form below rather than as a technical write-up.

### Putting a decision to the owner

Five lines. No jargon. If a term would send them to a search engine, the
message is wrong, not the reader.

```
What:            one sentence.
Why you:         which of the four kinds this is.
Options:         A or B, in plain words.
Recommendation:  A, and the one reason why.
If no reply:     A happens on <date>.
```

A decision put to the owner always carries a default and a date. An unanswered
question that stops work is a worse outcome than a reversible default, and
every decision here is reversible.

## Definition of ready

An issue is `status:ready` only when all of these hold. A developer that claims
an issue failing any of them is doing the wrong work well.

- [ ] The problem is stated as an observable behaviour, not a proposed solution.
- [ ] Every open question on it has an answer, in a comment, from the architect.
- [ ] Acceptance criteria are listed and each one is checkable.
- [ ] The affected files or modules are named, and one owner is identified.
- [ ] For a defect: a concrete failure scenario, inputs and the wrong output.
- [ ] For a requirement: the human approval is linked.
- [ ] It is small enough to land in one pull request. If not, split it.

## The approval gate

A `type:proposal` never becomes work without a human. This is the one rule in
this document with no exception and no agent override.

An agent running on a routine may:
- open proposals,
- comment on them,
- answer questions on them.

An agent running on a routine may **not**:
- move `status:needs-approval` to `status:ready`,
- open a requirement from an unapproved proposal,
- implement a proposal because it looks obviously correct.

If a proposal looks obviously correct and urgent, say so in the issue and leave
it for the human. Obviousness is exactly the feeling that precedes an unwanted
change.

## What a routine agent may do unattended

Running on a schedule with nobody watching narrows what is safe, because a
mistake compounds before anyone sees it.

**Allowed:** answering questions in its own domain, fixing a `status:ready`
issue at `p2` or `p3` whose diff is confined to one owner's files, updating a
stale doc, adding a missing test, opening a defect it found, commenting.

**Not allowed without a human in the loop:** anything touching `types.py`,
anything changing a number in `ScoringConfig` or `RiskConfig`, anything a
proposal rather than a defect, anything at `p0` or `p1`, force-pushing,
closing an issue it did not fully resolve, and opening more than a few issues
in one run.

**Always:** leave a comment saying what it did and what it chose not to do. An
unattended run that leaves no trace is indistinguishable from one that did
nothing, and the difference matters at review time.

## Branches and pull requests

- Branch from the default branch, named `feat/`, `fix/`, `chore/`, `docs/` or
  `test/` plus a short kebab-case description. No agent, model or tool names.
- One issue per pull request. `Refs: #NN` or `Closes: #NN` in the body.
- Conventional Commits, imperative subject of 72 characters or less, body
  explaining what changed and why.
- The pull request body restates the acceptance criteria as a checklist, with
  each one ticked and a line saying how it was verified.
- `ruff check`, `ruff format --check`, `mypy` and `pytest` clean before pushing.
  A push that turns CI red costs a cycle and the reviewers' trust.

## Definition of done

- [ ] Every acceptance criterion is met and demonstrably so.
- [ ] Tests cover the behaviour, and at least one would have failed before.
- [ ] All four checks pass.
- [ ] `docs/` updated where behaviour changed. A spec that disagrees with the
      code is a defect, and the next audit will open an issue for it.
- [ ] If the decision was cross-cutting or contested, an ADR exists.
- [ ] The diff is the minimum that satisfies the criteria. Scope not widened.
- [ ] No unmeasured claim anywhere in the diff.

## Writing an issue

Bad: "Improve the positioning pillar."

Good: "POSITIONING emits at a standard deviation near 0.59, so it contributes
about 0.059 against the 0.10 in `ScoringConfig`. Reproduce by computing the
emitted standard deviation across the universe on the section 7 fixture.
Acceptance: the emitted standard deviation is reported in
`PillarScore.diagnostics`, and either the pillar is rescaled or the gap is
documented in the spec with the measured figure."

The difference is that the second can be verified as done by someone who was
not in the conversation.
