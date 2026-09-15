---
name: writing
description: Who each piece of writing in this repository is for, and how to write it so that reader can act on it. The owner as the default audience, the shape of a pull request description, the jargon test, and which decisions never get handed over. Load before writing a pull request description, an issue, a comment on either, or anything else a person is expected to read and act on.
---

# Writing

The mechanical rules live in `CLAUDE.md` under "House style", and are the
authority. Punctuation, tone, commit subjects, what may never be named: read
them there. They are deliberately not summarised here, not even as a list.
A summary is a second copy, a second copy drifts, and `CLAUDE.md` and
`CONTRIBUTING.md` already carry the same rules twice.

This skill is about the thing those rules do not cover, and the thing that went
wrong: **who is going to read this, and can they act on it.**

## Ask who reads it first

Three readers, and almost everything has exactly one primary one.

| Reader | Reads | Wants |
| --- | --- | --- |
| The owner | Pull request descriptions, issues, anything addressed to them | What do I do, is anything broken, does anything need me |
| Another desk | Issue bodies, review comments, ADRs | The reasoning, in full, in its own vocabulary |
| A future reader of the code | Docstrings, commit bodies, `docs/` | Why it is this way, not what it does |

Get this wrong and the writing can be entirely correct and still useless. That
is not a style problem, it is a delivery problem.

## The owner

**The owner merges everything.** Nothing in this repository lands without them
pressing the button, so a pull request description is read by them first and by
a reviewer second, whatever else it is for.

They are a working developer with a day job. They are not an economist, not a
trading specialist, and not a statistician. They do not have time to research
what a term means before they can decide whether to merge. Writing that assumes
otherwise does not get better scrutiny, it gets less: an unreadable description
is merged unread, which is worse than a short one.

What reaches them answers three questions in this order: what do I do, what
changed and why did it matter, does anything actually need me.

## The shape of a pull request description

After the desk signature line, when a scheduled run wrote it:

```
## What you need to do

Merge it normally. Nothing here needs a decision from you. CI is green.

## What this changes, in plain words

...

---

## Technical record

...
```

**"What you need to do" is first and is one or two sentences.** Usually it is
"merge it normally". When it is not, say exactly what is different and why.

**"In plain words" means a reader outside the specialism can follow it.** Name
what was wrong and what it would have cost. State the size honestly: "one
comment, thirteen lines" tells them how hard to look.

**Everything else goes below the rule, under "Technical record".** Acceptance
criteria, the mutation table, the four checks, the estimator: all of it stays,
because it is the record and a reviewer wants it. It stops being the first thing
anyone reads. Nothing is deleted to meet this shape. It is moved.

**Never head a section "For the reviewer" without saying who that is.** On this
repository the owner merges, so an unaddressed note reads as though it is aimed
at them and describes work they cannot evaluate.

## The jargon test

A term the owner would have to look up is replaced, or explained where it first
appears. The test is whether someone who has never opened the file can say what
changed and why it mattered.

This does not mean writing down to them. It means not making them do the
translation. The same fact, twice:

> `MIN_CROSS_SECTION`'s docstring offered `+/-0.707`, which is `1 / sqrt(2)`,
> the two-point z-score under the sample standard deviation, where the module
> mandates `ddof=0`.

> A comment explaining why a rule exists gave the wrong number. With only two
> currencies the maths can tell you which is higher but nothing about by how
> much, so both score the same. The comment said that score was 0.707. It is
> actually 1.0.

The second is not less precise. It is the same claim with the reader's work
done for them, and the first version is still there in the technical record for
anyone who wants it.

Keep the real identifiers when you name them. `pmi_composite` is what they will
grep for. Explain what it is, do not rename it into prose.

## Never hand over a decision that is not theirs

`issue-workflow` carries the rule and is the authority: four kinds are the
owner's, which are facts only they hold, their own habits and appetite, whether
a thing is built at all, and what merges. Everything else belongs to the desk.

These phrases are the tell that a decision is being handed over rather than
taken:

- "both yours"
- "a human call"
- "I have no preference strong enough to argue for"
- any sentence that lists options for the owner on something that is not one of
  the four kinds

If you catch yourself writing one, you have found a decision. Rule on it, say
why, and name the cost of being wrong.

"What merges" means the decision to merge this diff at all. It does not mean
which merge button to press, whether a closing keyword should be struck from a
squash message, or which tracking state an issue is left in. That is bookkeeping
and it belongs to the desk.

When a decision genuinely is one of the four, it reaches them as five lines with
a recommendation and a default, per `docs/routines.md`.

## Writing for another desk

Be technical. A review comment to the quant analyst about a z-score should use
the word z-score, and an ADR should carry the full argument in its own
vocabulary. The plain-language rule is about the owner, not a ban on precision.

Say which desk, so a reader can tell whether it is for them.

## Writing for the future reader

Docstrings, commit bodies and `docs/` are read by whoever arrives next, which is
usually nobody the writer has met. `engineering-standards` carries the docstring
rules and is the authority: the signature says what, the docstring says why.

For a commit body, say what changed and why, never that a tool made it.

## Before you post it

- [ ] I know which of the three readers this is for, and it says so if that is
      not obvious.
- [ ] If the owner reads it: the first thing is what they need to do.
- [ ] Every term they would have to look up is explained or replaced.
- [ ] The technical detail is still present, below the fold.
- [ ] Nothing is handed to them that is not one of the four kinds.
- [ ] No unaddressed "for the reviewer".
- [ ] `CLAUDE.md` house style holds, checked against `CLAUDE.md` rather than
      from memory.

## Why this exists

#118 fixed a wrong number in a code comment. Thirteen lines. Its description
opened on `ddof`, sample versus population estimators and `1 / sqrt(2)`, and
closed with a section headed "For the reviewer" that never said who that was.
A comment on it asked the owner to choose between two merge styles, with "both
yours" and "I have no preference strong enough to argue for".

The owner's response was that they could not tell whether it was addressed to
them or to another agent. That is the one question a description exists to
settle, and the work underneath it was correct throughout.
