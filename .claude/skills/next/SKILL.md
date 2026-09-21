---
name: next
description: Pick up the next piece of work on this project and take it to a pull request. Use when sitting down to move the project forward and you do not want to decide what to work on, or pass an issue number to work that one. Claims the issue, writes the tests first, implements, runs the four checks, and opens the pull request.
---

# Next

One session, one issue, one pull request. The owner is at the keyboard, so ask
them when it matters rather than guessing, and stop early rather than guessing
late.

Usage: `/next` picks for them. `/next 62` works issue 62.

**Read `docs/routines.md` and load the `engineering-standards` and
`issue-workflow` skills before anything else.** This is the same work the
scheduled desks do and it follows the same rules. Where this file and
`docs/routines.md` disagree, the file wins.

## 1. Get current, and say what you find

```bash
git fetch origin --quiet && git status --short
git log --oneline origin/main -5
python3 -m pytest -q 2>&1 | tail -2
```

If `main` does not pass, that is the work. Say so and fix that instead.

If the working tree is dirty, stop and ask. Never stash or discard someone
else's uncommitted work.

## 2. Pick the issue

If they gave a number, take it. Otherwise list what is claimable and **propose
one**, with the number and one line of why, then wait for a yes. Do not present
a menu of eight and make them choose.

Claimable means `status:ready`, no open pull request, and either:

- `type:requirement` + `roadmap`, which is the engine itself, or
- `routine-safe` at `p2` or `p3`, which is smaller upkeep.

Prefer the roadmap issue whose dependencies have landed and whose number is
lowest. Check the delivery-order comment on the phase's lowest-numbered issue.
**Never claim one whose dependencies are still open**, even if it reads ready: a
pull request built on an unlanded dependency cannot be verified.

Never take one already at `status:in-progress`, whoever holds it. The scheduled
desks run on weekdays and may be mid-flight.

Ask before starting if the issue looks like more than one sitting.

To claim it: comment that you are taking it, and swap `status:ready` for
`status:in-progress`.

## 3. Build it

Branch from `main`: `feat/`, `fix/`, `chore/`, `docs/` or `test/` plus a short
kebab-case description. No agent, model or tool name in it.

**The stub docstring is the specification.** Read it before writing anything and
implement what it says rather than what seems reasonable. If the docstring and
an acceptance criterion disagree, stop and say so rather than choosing.

**Tests first, and prove each one can fail.** Run them against the stub and
confirm each fails inside the method it exercises, not because something raised
`NotImplementedError` on a path the test never reaches. Say in the pull request
that you checked this, and where each test died.

Then the implementation, and no more than the criteria need.

These are not negotiable, and each exists because it went wrong here before:

- Absence is explicit, never a passing value. A failed fetch, an empty cache and
  a real empty result are three different answers.
- A missing input raises or returns a marked absence, never a plausible default.
- No network in tests. Use `respx`, or `DataConfig(offline=True)`.
- Historical reads filter on release date, not the period the figure describes.
- Every docstring you write states units, sign convention and missing-data
  behaviour.
- No claim of an edge, a hit rate or a measured result anywhere in the diff.

**Ask the owner before**: changing `src/fbe/types.py`, changing any existing
value in `ScoringConfig` or `RiskConfig`, or widening the issue. Those need a
ruling, and they are the three that break things quietly.

## 4. Check it, then check it again differently

```bash
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m mypy
python3 -m pytest
```

All four clean before you push.

`mypy` may report `Library stubs not installed` in a fresh environment. That is
a missing dev dependency, not a defect: `pip install -e ".[dev]"` and re-run.

Then two passes over your own diff. First as the test engineer, per
`.claude/agents/test-engineer.md`, recomputing any number rather than trusting
your implementation. Then as the code reviewer, per
`.claude/agents/code-reviewer.md`, hunting the defect classes it lists. Fix what
either finds.

A cheap and effective last pass: change your implementation to something
plausibly wrong and confirm a test catches it. A test that passes both ways
tests nothing.

## 5. The pull request

Against `main`, following `.github/pull_request_template.md`. Acceptance
criteria as a ticked checklist, each with how it was verified. Closes: #NN when
every criterion is met.

Use Refs: #NN only when you have deliberately left a criterion open, and then
say which and why. Do not downgrade to a Refs trailer merely because you noticed
something worth mentioning; put that in the body and still close the issue.

Write the trailer as plain text on its own line, never inside backticks. A
backticked trailer is a code span, so GitHub parses no keyword and the merge
closes nothing. The two trailers above are bare for that reason.

Comment the link on the issue. **Do not merge.** The owner merges.

If `gh` is not installed, push the branch and give them the compare URL.

## 6. Tell them what happened

Five lines, plain words:

- What now works that did not before.
- What you changed, in one sentence.
- Anything you could not do, and what it would take.
- Anything that needs their decision, and which of the four kinds it is.
- The pull request link.

## Stopping well

Stopping is a good outcome when the reason is real. Say which and stop:

- The issue needs a ruling that is not yours.
- It turned out bigger than one sitting.
- A test will not fail against the stub, so it is testing nothing.
- You would have to guess at something the specification does not say.

Leave the branch pushed and the issue commented, so the next session or the
scheduled desk can pick it up. Never leave an issue at `status:in-progress`
with nothing on it: put it back to `status:ready` and say why.
