---
name: status
description: Where this project actually is, in plain language, in under a minute. Use when picking the project up after time away, before deciding what to work on, or when you want to know whether anything is broken or waiting on you. Reads the repository and GitHub and reports; changes nothing.
---

# Status

You are reporting to the owner, who is a working developer picking this project
up in their own time. They have been away from it. They are not an economist
and not a trading specialist. Assume they remember the shape of the project and
nothing about last week's details.

**Change nothing.** No branch, no label, no comment, no file. This reads.

## Gather

```bash
git fetch origin --quiet
git log --oneline origin/main -8
git status --short
python3 -m pytest -q 2>&1 | tail -3
grep -rc "raise NotImplementedError" src/fbe/ --include=*.py | awk -F: '{s+=$2} END {print s" stub sites"}'
```

Then from GitHub: open pull requests with their checks, open issues by status
label, and anything at `status:needs-decision` older than two days.

## Report

Six short sections. No table unless it genuinely reads better than a sentence.
Aim for something they can read standing up.

**Is it broken?** Does `main` pass its own tests, yes or no. If no, that is the
whole report until it is fixed: say what is failing and offer to fix it.

**What landed since they last looked.** Two or three lines from the commit log,
in plain words. "The cache now works" beats the commit subject.

**What is waiting on them.** Pull requests ready to merge, and any decision that
is genuinely theirs. The `issue-workflow` skill lists the only four kinds of
decision that are: a fact only they hold, their own habits or appetite, whether
to build a thing at all, and what merges. **If a question is not one of those
four, it is not theirs and it does not go in this section.** Say instead that
the desk owes a ruling on it.

**What the desks are doing.** One line. Which issues are claimed and by which
desk, from the `run:` labels.

**How far along.** Stub sites remaining against the last figure you can find,
and which modules are now real. This is the number that tells them whether the
project is moving.

**What you would do next**, if they have an hour. One suggestion, with the
issue number, and why that one.

## Rules

- No jargon. If a term would send them to a search engine, rewrite the sentence.
- Numbers you have actually run, never remembered or estimated. If you did not
  run it, say so.
- Never report a test suite as passing without running it.
- If nothing has changed since the last time, say that in one line rather than
  padding.
