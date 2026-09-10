---
name: engineering-standards
description: Code writing standards for fundamental-bias-engine. SOLID, KISS, YAGNI, DRY and when each does not apply, plus error handling, typing, docstrings, testing and the review checklist. Load before writing or reviewing any Python in this repository, before designing a module, and before deciding whether a function should raise or return a default.
---

# Engineering standards

Read this before writing code here. It is not a general style guide. Every rule
below exists because something in this repository went wrong in that exact way,
and the concrete cases are named so the rule is arguable rather than obeyed.

## The prime directive

**A wrong number that looks right is the worst possible outcome.**

This is not a normal codebase. It computes position sizes for a real account and
a directional bias someone will act on. Almost every serious defect found here so
far produced a plausible value rather than an exception:

| What happened | What it produced |
| --- | --- |
| A pair built as USDEUR instead of EURUSD | A confident, backwards bias |
| A missing conversion rate defaulting to 1.0 | Positions roughly 18x too large, every trade |
| Position size rounding up to the lot step | The risk cap breached slightly, forever |
| Filtering data on its period rather than its release date | Backtests that see the future and flatter |
| The risk ladder hardcoding 1% and 2% next to a config that held them | Every high conviction trade refused |
| Re-standardising by the current run's own dispersion | A pillar amplified exactly when its inputs disagreed |

Not one of those raises. Not one fails a naive test. Every rule below is downstream
of this table.

Corollary: **prefer a loud failure to a quiet default.** If you are about to write
`or 0`, `except: pass`, `.get(key, 1.0)` or a fallback that lets the program
continue with a number you did not verify, stop. That is the failure mode.

## SOLID, as it applies here

SOLID is about where change lands. Take it as a set of questions, not
commandments.

**Single responsibility.** A module has one reason to change. `datasources/`
changes when a provider changes its API. `pillars/` changes when the model's
view of economics changes. Those are different reasons, so a source that knows
what a pillar wants is wrong. The concrete test in this repo: sources emit
canonical indicator keys and never their own vocabulary, so replacing FRED with
the OECD touched one registry and no pillar.

**Open for extension, closed for modification.** Adding an eighth pillar should
mean adding a file, not editing seven. `BasePillar` and the `Pillar` protocol
exist for this. If adding a pillar requires touching `scoring.py`, the seam is
in the wrong place.

**Liskov substitution.** Any `Pillar` must be usable wherever the protocol is
accepted. The risk pillar overrides `_normalise` to skip cross-sectional
standardisation, which is legitimate because it still honours the contract:
one score per currency, on the same band, same sign convention. Overriding to
return a different scale would not be.

**Interface segregation.** `DataSource` asks for `fetch` and `available`, and
nothing else. A calendar source that cannot meaningfully implement a pillar
feed should not be forced to. Where a caller needs only part of a type, pass
the part.

**Dependency inversion.** Depend on the contract, not the implementation. The
live example is `bias.py`, which takes a calendar guard as an injected callable
rather than importing `calendar_guard`. That import edge is deliberately
absent in both directions, and it stays absent.

## KISS

The simplest thing that is correct. Not the shortest, not the cleverest.

- A conditional is a free parameter wearing a disguise. The inflation gate was
  withdrawn partly because `0.10pp` and the pivot rule are two numbers nobody
  can defend yet.
- If a function needs a comment to explain what it does, rather than why, it is
  too clever. Rewrite it, do not annotate it.
- Prefer boring data structures. A frozen dataclass beats a dict with an
  understanding.

Simple is not the same as short. `convert_rate` is longer than the inline
lookup it replaced and much simpler, because the resolution order is now stated
once instead of implied in three places.

## YAGNI

Do not build for a requirement nobody has stated.

The counterweight, specific to this project: **auditability is a stated
requirement**, not speculative generality. Recording which code path produced a
number, keeping the config digest on a report, snapshotting the bias at entry
in the journal, these are not YAGNI violations. They are the requirement. If in
doubt, ask whether the thing being built answers "how did we get this number",
and if it does, build it.

## DRY, and when it is wrong

Duplicated knowledge is the problem. Duplicated code is sometimes fine.

The rule that matters here: **a number that exists in two places will disagree,
and the disagreement will be silent.** Every threshold lives in
`ScoringConfig` or `RiskConfig`. If you are typing a literal float that also
appears in config, you are writing the config-drift bug again.

Where DRY is wrong: two functions that look alike but change for different
reasons should stay apart. Merging them couples two unrelated futures.

## Error handling

- **Raise on a missing input you cannot compute without.** `MissingRateError` is
  the reference: its own type so it cannot be swallowed by a stray `except
  KeyError`, and no fallback to 1.0, no stale rate, no last known value.
- **Never substitute a default for absent data.** Missing data yields a neutral
  score with reduced coverage, and the `None` markers on `raw` and `z` let the
  aggregator tell absence of evidence from evidence of neutrality. Those are
  different facts and the code must be able to say which one it has.
- **Warn, do not adjust.** `PositionSize.warnings` exists so the module can size
  what was asked for and say what is wrong with it, rather than silently
  fixing it.
- **A stub raises.** `NotImplementedError` pointing at the roadmap. Never make a
  stub return zeros to get a test green.

## Typing and contracts

- `from __future__ import annotations`, full hints on every function, line
  length 88.
- `types.py` imports only the standard library. No I/O, no computation, no third
  party. Every module imports from it, so a dependency there is a dependency
  everywhere.
- **Changing a type is a breaking change.** Update every consumer in the same
  commit. Additive with a default is the only cheap change, and even that needs
  a docstring saying what the field means and who reads it.
- If a field's meaning changes at a pipeline stage, say so in the contract.
  `PillarScore.weight` means the configured weight before the staleness penalty
  and the effective weight after it, and the docstring says exactly that
  because four consumers depend on knowing which they hold.

## Docstrings

The signature says what. The docstring says why, and three specific things the
signature cannot:

1. **Units.** Percentage points or basis points. Account currency or quote
   currency. A number without a unit is how the notional bug happened.
2. **Sign convention.** Positive means currency-strengthening, everywhere. State
   it wherever a sign could be read either way, especially where intuition
   inverts, such as falling unemployment being currency-positive.
3. **Missing-data behaviour.** What this returns when an input is absent.

For a stub, the docstring is the deliverable. A stub whose docstring omits any
of those three is incomplete, not merely unimplemented.

## Testing

- **Tests must not hit the network.** Use `respx`, or `DataConfig(offline=True)`.
- **Test the wire, not just the value.** A test that passes because a constant
  happens to equal the config default proves nothing. Override the config and
  confirm the answer changes.
- **A worked example in the docs is a fixture.** If the spec publishes numbers,
  a test reproduces them to the published precision. An example that does not
  reproduce is worse than no example.
- **Perturb, do not just assert.** The most useful test written here shifted one
  currency's inputs and checked what happened to a currency that did not move.
- Skip-guard modules that are still landing with `pytest.importorskip` so the
  suite grows teeth as they arrive.

## Naming

- Canonical indicator keys carry their units: `cot_net_pct_oi`, not
  `cot_net_position`. `current_account_gdp`, not `current_account`.
- Market convention is load-bearing. EURUSD, never USDEUR.
- No model, agent or tool names anywhere, except a model used as a product
  dependency in `reasoning/`.

## Before you open a pull request

- [ ] Every new number is in config, or is genuinely local and documented as such.
- [ ] Every money value states its currency in the docstring.
- [ ] Every sign rule is stated where a reader could read it either way.
- [ ] Nothing defaults a missing input. It raises, or it returns a marked absence.
- [ ] Rounding that affects risk rounds down.
- [ ] Anything reading historical data filters on release date, not period.
- [ ] No claim of an edge, hit rate or backtested result that was not measured here.
- [ ] `ruff check`, `ruff format --check`, `mypy` and `pytest` all clean.
- [ ] The diff is the minimum that answers the request. Nothing widened on your
      own initiative.

## When a rule here conflicts with the trading plan

`docs/trading-plan.md` wins. It is the specification, and this document is about
how to implement it well.
