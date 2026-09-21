# CLAUDE.md

Read this before changing anything in this repository.

## What this project is

`fundamental-bias-engine` answers exactly one question:

> Which G10 currency is fundamentally stronger than which, and by enough to be
> worth trading?

It is a directional-bias layer, not a trading system. The owner already trades
technically: trendlines, trend channels, 1h and 4h charts, candlestick
confirmation. Those decisions stay with the owner. This engine supplies the
fundamental lean that tells them which side of a pair to be looking for a setup
on. It never places an order and it has no execution code path.

The method is the Anton Kreil / Institute of Trading and Portfolio Management
relative-value macro framework: score each currency on its own fundamentals,
then difference the two legs of a pair. There is no such thing as a strong
currency in isolation, only a currency stronger than the one it is quoted
against.

The owner's trading plan is transcribed at `docs/trading-plan.md`. Its appendix
records which parts of the plan this engine automates and which it does not. If
a change to this repo would contradict a rule in that plan, the plan wins.

## Architecture and data flow

Data flows in one direction. Nothing downstream calls back upstream.

```
data sources        pillars           scorer            bias layer        output
-----------         -------           ------            ----------        ------
FRED, CFTC,   -->   7 pillars   -->   weighted    -->   difference   -->  CLI table
Stooq/Yahoo,        compute           sum per           the two legs      HTML dashboard
Forex Factory,      one score         currency          of each pair      JSON report
manual YAML         per currency

Observation    -->  PillarScore  -->  CurrencyScore --> PairBias     -->  BiasReport
```

- **`src/fbe/datasources/`** produces `Observation`s. Each source owns its own caching and translates its own series identifiers into canonical indicator keys. Sources know nothing about pillars.
- **`src/fbe/pillars/`** turns `Observation`s into `PillarScore`s. A pillar receives the whole universe's observations at once, not one currency's, because normalisation is cross-sectional by design. Pillars know nothing about weights or pairs.
- **`src/fbe/scoring.py`** aggregates `PillarScore`s into one `CurrencyScore` per currency: a weighted sum on the `-3..+3` band, plus dispersion and coverage.
- **`src/fbe/bias.py`** differences `CurrencyScore`s into `PairBias` rows, one per entry in `ALL_PAIRS`, and assigns `Direction` and `Conviction` from the spread, the pillar agreement, and data freshness.
- **`src/fbe/risk.py`** and **`src/fbe/calendar_guard.py`** decorate a `PairBias` into a `TradeIdea`: position size from a trader-supplied stop, and a blackout flag if a high-impact release is near either leg.
- **`src/fbe/report.py`**, **`src/fbe/dashboard/`** and **`src/fbe/cli.py`** render. They compute nothing. If a renderer needs a number that is not on a dataclass, the number belongs upstream, not in the template.

## What is implemented and what is not

The vocabulary, the universe, the config and the scoring chain are real.
Everything else is scaffolding at varying stages. Each module is written from
the repository root so the row can be checked against the file it names, which
`tests/test_module_status_table.py` does.

| Module | Status |
| --- | --- |
| `src/fbe/types.py` | Implemented. The shared vocabulary. |
| `src/fbe/universe.py` | Implemented. G10, metadata, the 28 pairs. |
| `src/fbe/config.py` | Implemented. Defaults, file and environment. |
| `src/fbe/scoring.py`, `src/fbe/bias.py` | Implemented. Composite and pair rows. |
| `src/fbe/datasources/*` | Implemented. Seven sources, the registry, the cache. |
| `src/fbe/pillars/*` | In progress |
| `src/fbe/risk.py`, `src/fbe/calendar_guard.py`, `src/fbe/journal.py` | In progress |
| `src/fbe/report.py` | Implemented. The dated Markdown, its JSON sidecar and the run-to-run diff. |
| `src/fbe/cli.py`, `src/fbe/dashboard/*` | In progress |

**How to tell without checking this table:** a stub raises `NotImplementedError`
with a message naming the fully qualified callable and pointing at the phase of
`docs/roadmap.md` that delivers it, in one form everywhere:

```python
raise NotImplementedError(
    "fbe.scoring.composite is scaffolded; see docs/roadmap.md Phase 2"
)
```

An abstract method on a base class, such as `BaseDataSource.fetch`, keeps a
bare `raise NotImplementedError`. Reaching it means a subclass is incomplete,
not that a phase is unfinished, and the message would say the wrong thing.
`tests/test_stubs.py` walks the package and enforces both halves of this rule.

A function that returns plausible but fabricated numbers is worse than one that
refuses to run, so stubs raise rather than return placeholder data. Do not "fix"
a stub by making it return zeros.

The tests reflect this. `tests/test_universe.py` and `tests/test_config.py`
test real behaviour. `tests/test_smoke.py` uses `pytest.importorskip` so that
modules still landing do not break the suite, and starts covering them
automatically as they arrive.

## The rule about `src/fbe/types.py`

`src/fbe/types.py` is the shared vocabulary. Every other module in the package
imports from it, and it imports nothing but the standard library. Keep it that
way: no I/O, no computation, no third-party dependency.

**Changing a type in `types.py` is a breaking change.** Renaming a field,
changing its type, reordering positional arguments, or adding a required field
breaks every consumer at once. If you change it, update every consumer in the
same commit. Do not land a types change and leave the fixes for later, and do
not add a compatibility shim to avoid touching consumers. Additive changes with
a default are the only ones that are cheap, and even those need a docstring
explaining what the field means and who reads it.

The same discipline applies to `src/fbe/universe.py`: `ALL_PAIRS` ordering and
quoting convention are load-bearing. A report that silently inverts a pair
silently inverts its bias.

## House style

Before writing or reviewing Python here, load the `engineering-standards` skill
in `.claude/skills/`. It carries SOLID, KISS, YAGNI and DRY as they apply to
this codebase, the error handling and docstring rules, the testing rules, and
the pre-pull-request checklist. Every rule in it exists because something here
went wrong in that exact way, and the cases are named so the rules are arguable
rather than obeyed.

Work enters as a GitHub issue and leaves as a merged pull request. The
`issue-workflow` skill carries the lifecycle, the labels, the definition of
ready, what an agent running unattended may and may not do, and the one rule
with no agent override: a proposal never becomes work without a human
approving it. `docs/team.md` shows the flow and who owns what.

Settled cross-cutting decisions are recorded in `docs/decisions/`. A decision
that lives only in a commit message is a decision that will be quietly
reversed.

Before writing anything a person is expected to read and act on, a pull request
description above all, load the `writing` skill in `.claude/skills/`. The rules
below are mechanical and say nothing about audience, and the failure they do not
catch is writing that is entirely correct and unreadable by the one person who
has to merge it. That skill covers who each thing is for and what that costs
when it is guessed wrong.

The rest of this section is the writing style, which is strict and applies to
code, comments, docstrings, docs, and commit messages.

- **No em dashes. No en dashes as sentence punctuation.** Use a comma, a colon, or a new sentence. Hyphenated numeric ranges like `1-2%` are fine.
- **Plain, direct sentences.** No filler adjectives, no marketing tone, no exclamation marks. State what a thing does and what it does not do.
- **Never describe any of this work as AI-generated or AI-assisted**, anywhere in the repo, in any file, comment, or commit message. No model, agent, or tool names in branch names, code, comments, or docs. The one exception is a model used as a product dependency: `src/fbe/reasoning/` calls a language model the way the data layer calls FRED, and naming it there is a dependency, not a disclosure. See `docs/reasoning-layer.md`.
- **Conventional Commits.** `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`. Imperative subject, 72 characters or less. See `CONTRIBUTING.md`.
- **Python:** `from __future__ import annotations` at the top of every module, full type hints on every function, Google-style docstrings. Line length 88.
- **Docstrings explain why, not what.** The signature already says what. Say why the threshold is 0.6, why the convention is EURUSD and not USDEUR, why a pillar normalises cross-sectionally.

## Running things

```bash
pip install -e ".[dev]"

python3 -m pytest                # tests
python3 -m ruff check .          # lint
python3 -m ruff format --check . # formatting
python3 -m mypy                  # types (config in pyproject.toml)
```

All four run in CI on Python 3.11 and 3.12 (`.github/workflows/ci.yml`). All
four must pass before pushing.

Tests run from `src` via the `pythonpath` setting in `pyproject.toml`, so the
suite works without an editable install. Tests must not hit the network. Use
`respx` to mock `httpx`, or `DataConfig(offline=True)` to force cache-only
reads.

## The `data/` directory

```
data/
  cache/     Source responses, keyed by source and request. Git-ignored except .gitkeep.
             Lifetime is DataConfig.cache_ttl_hours. Safe to delete at any time.
  manual/    Hand-maintained YAML inputs for series with no free API. Committed,
             because losing them means re-keying them by hand.
  reports/   Dated BiasReport JSON and rendered Markdown. Committed on purpose:
             they are the audit trail, and --compare reads the previous run.
  journal/   Append-only JSONL trade records written by src/fbe/journal.py.
             Git-ignored except .gitkeep, and deliberately so: see below.
```

Anything written to `cache/` must be reproducible from source data plus
config, and is safe to delete at any time. `reports/` is the exception and is
committed, because a report is precisely what cannot be reproduced later: macro
series get revised, cross-sectional scores depend on the rest of the universe
on the day, and the weights may have changed since. Re-running last week's date
does not recover last week's call.

**The journal is the exception to the exception, and it is not an oversight.**
The argument for committing `reports/` applies to `data/journal/` more strongly,
not less: `src/fbe/journal.py` snapshots the bias at entry precisely because
reconstructing it later is impossible. It is still not committed. `reports/`
holds the model's opinion, which is publishable. The journal holds the owner's
entry prices, position sizes and realised profit and loss on a real account,
which is a private financial record, and this repository is public. That
asymmetry is the whole reason, and it is written here so the next reader treats
the `.gitignore` rule as a decision rather than a gap to be fixed.

The consequence follows from it and is easy to miss: **a fresh clone has no
journal.** The routine hosts clone `main` on every run, so every one of them
sees an empty one, and `journal.evaluate` reads a file that only ever exists on
the owner's own machine. The journal is therefore the owner's to back up.
Nothing else in this repository will do it, and Phase 6 of `docs/roadmap.md` is
the point at which its absence would be discovered too late to fix.

A known gap, recorded rather than resolved: `DataConfig` carries `cache_dir`,
`manual_dir` and `reports_dir`, so those three can be relocated, while
`JOURNAL_PATH` is a module constant with no `journal_dir` beside them. An
operator who moves the data tree moves three directories and leaves the fourth
behind, and it is the one holding data no rerun can recreate. Whether to add the
field is a separate decision and is not taken here.

If deleting `data/cache/` loses information, that information was in the wrong
place, and it belongs in `data/manual/`, in `reports/`, or in the journal. Of
those three, the journal is untracked, so choosing it means choosing to back the
file up yourself.

## Standing instruction on claims

**No file in this repository may claim a backtested edge that has not been
measured.** Not the README, not a docstring, not a dashboard tooltip, not a
commit message.

The pillar weights in `ScoringConfig` are priors. They are reasoned, they are
documented in `docs/scoring-spec.md`, and they are not evidence. Nothing in
this project has been validated against out-of-sample returns yet. Phase 6 in
`docs/roadmap.md` is the first point at which anyone can honestly say whether
the model works.

Until then, permitted language is "the model's prior is", "the weight reflects
the view that", "this has not been measured". Forbidden language is "proven",
"backtested", "high win rate", "edge", and any number presented as a historical
result that was not computed from real data in this repo. If you catch such a
claim while working on something else, remove it.

## Related documents

- `docs/trading-plan.md` The owner's plan, plus what the engine automates.
- `docs/methodology.md` The relative-value framework and why seven pillars.
- `docs/scoring-spec.md` Normalisation, weights, conviction bands.
- `docs/data-sources.md` Sources, indicator registry, caching.
- `docs/risk-and-execution.md` Sizing, blackout windows, journal.
- `docs/interfaces.md` CLI commands and report and dashboard contracts.
- `docs/reasoning-layer.md` The written brief: hot list, grounding gates, cost.
- `docs/roadmap.md` Phases, definitions of done, open questions.
- `docs/decisions/` Architecture decision records.
- `docs/team.md` The specialist roster and who owns what.
- `CONTRIBUTING.md` Branches, commits, checks.
