# 0009. A model name in `docs/` is limited to the product dependency, and process configuration is stated by reference

Status: Accepted

## Context

`CLAUDE.md` carries the rule: never describe the work as AI-generated or
AI-assisted anywhere in the repository, and no model, agent or tool names in
branch names, code, comments or docs. One exception is stated, for a model used
as a product dependency, because `src/fbe/reasoning/` calls a language model the
way the data layer calls FRED.

Issue #48 asks `docs/routines.md` to record which model the routine hosts run,
so the setting cannot drift unnoticed when a host is recreated. The maintenance
desk declined to build it and escalated, on the grounds that a ceiling is a model
name, `docs/routines.md` is docs, and the routine hosts are the development
process rather than a product dependency, so the stated exception does not reach
them.

The position in the tree, verified rather than recalled:

- `.claude/agents/*.md` carry `model:` in frontmatter, twelve files, at two
  distinct settings. That directory is agent configuration and is not `docs/`.
  (This record originally named the two settings here. The check its own
  consequences called for, landed by #48 as `tests/test_model_policy.py`,
  refused the record when it finally reached `main`, and it now states the fact
  by reference, which is what it rules.)
- `docs/` contains exactly one model token across the whole tree, in
  `docs/reasoning-layer.md`, naming the reasoning layer's own dependency under
  the heading that describes it. That is the stated exception, and the name is
  not repeated here for the same reason this record gives.
- `docs/routines.md:79` already forbids the practice in the file the issue wants
  to edit: "Nothing else goes in that line: no tool, no model, no agent name, per
  the writing rules in `CLAUDE.md`."

So the repository is already stricter than the rule requires, and the issue as
specified would be the first departure from it.

## Decision

The rule reaches process configuration. A model name may appear in `docs/` only
where a model is a product dependency of the software, which today is
`docs/reasoning-layer.md` alone.

Where documentation needs to constrain what the routine hosts run, it states the
constraint **by reference** to `.claude/agents/*.md` rather than by naming a
model. For #48 that reads: a host's model is set explicitly at creation, and no
host runs a model below the one the judgement-heavy agent definitions name.

`CLAUDE.md` is not edited. The rule as written already produces this answer.

## Why

**A literal ceiling in `docs/` would be the same value in two places.** The
operative setting lives in `.claude/agents/*.md`. A prose ceiling in
`docs/routines.md` would be a hand-maintained second copy with nothing holding
the two together, which is the duplication this project has been bitten by
repeatedly, in its documentation form.

**The ceiling protects nothing mechanically in either version.** What prevents
the drift #48 reports is that a host's model is set explicitly at creation rather
than inherited. The ceiling is a reminder at the moment someone recreates a
routine, and that person must open `.claude/agents/` anyway to know what to pass.
Pointing them there is more useful than a number they then have to reconcile.

**The product-dependency line is real and worth keeping.** Naming the model in
`docs/reasoning-layer.md` describes what the software needs in order to run, and
a reader deciding whether to operate the engine needs it. Naming the model that
builds the software describes how the work was done, which is the disclosure the
rule exists to prevent. `docs/routines.md` already documents the automated
process in full without naming its tooling, which shows the line is drawable in
practice and not only in principle.

## Alternatives considered

**Widen the exception in `CLAUDE.md` to cover process configuration, then record
the ceiling literally.** Cleanest to read afterwards. Rejected: it moves a
house-style rule in order to record a value that is already recorded elsewhere,
and it would make `docs/routines.md` the first place in `docs/` to name a model
for a non-product reason. A `p3` documentation issue is not enough reason to
move the rule.

**Move the section into `.claude/` beside the agent definitions it governs, and
leave `docs/routines.md` pointing at it.** Keeps names out of `docs/` and keeps
the ceiling literal. Rejected: it splits routine documentation across two files
when `docs/routines.md` is the authority for what each run may do, and
`docs/routines.md` makes the argument against it itself, that two ways to do a
thing means one gets forgotten. The duplication with `.claude/agents/*.md` also
survives, a directory away instead of a file away.

## Consequences

Documentation that needs to constrain the hosts is one step less direct: a reader
follows a pointer into `.claude/agents/*.md` rather than reading a name. That is
the cost, and it is small next to two copies of one setting drifting apart.

This line will be re-litigated, because the reasoning on the other side is not
silly: `docs/routines.md` already tells a reader that unattended agents do the
work, so naming the model is arguably incremental rather than new disclosure.
That is why #48 carries a criterion for a check asserting that `docs/` holds no
model name outside `docs/reasoning-layer.md`. A rule that fails loudly is worth
more than a paragraph, and without one this record is a paragraph.

Nothing here changes `.claude/`, which is outside the rule's stated scope and
where the per-agent `model:` fields remain the operative setting.

This record is reopened if a second product dependency on a model appears
outside `src/fbe/reasoning/`, or if the owner decides the disclosure concern no
longer applies to their repository, which is theirs to decide rather than the
desk's.
