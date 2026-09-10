# The desk

Work on this project is split across eight named specialists, defined in
`.claude/agents/`. Each one owns a part of the pipeline and carries its own
standing instructions, so a request goes to the specialist who is accountable
for that area rather than to a generalist who has to rediscover the context.

Call one by name, for example "ask risk-manager to check the sizing on a R2000
account", or let the coordinator route the work.

## Roster

| Agent | Role | Owns | Ask them about |
|---|---|---|---|
| `macro-strategist` | Macro Strategist | `docs/methodology.md`, the pillar definitions | Why the model believes what it believes; adding or retiring a pillar; reading a live macro situation |
| `quant-analyst` | Quant Analyst | `src/fbe/pillars/`, `scoring.py`, `bias.py` | Transformations, z-scores, weighting, the conviction decision table, whether a change is an improvement or a curve fit |
| `data-engineer` | Data Engineer | `src/fbe/datasources/`, `docs/data-sources.md` | Series IDs, coverage gaps, release lags, caching, a number that looks wrong |
| `risk-manager` | Risk Manager | `src/fbe/risk.py`, `RiskConfig` | Position sizing, ZAR pip value, exposure limits, anything that could breach the 1-2% rule |
| `execution-desk` | Execution Desk | `src/fbe/calendar_guard.py` | News blackouts, broker constraints, spread cost, whether a pair is tradeable right now |
| `interface-dev` | Interface Developer | `src/fbe/cli.py`, `report.py`, `dashboard/` | Commands, the dated report, the run-to-run diff, the phone dashboard |
| `performance-analyst` | Performance Analyst | `src/fbe/journal.py` | Hit rate and expectancy by conviction, discipline flags, the review routine |
| `code-reviewer` | Code Reviewer | Nothing. Read-only. | Reviewing a diff before it is committed |
| `architect` | Architect | `docs/decisions/` | Cross-cutting design, arbitrating a disagreement, anything touching `types.py` |
| `product-analyst` | Product Analyst | Proposal and requirement issues | Whether an idea is worth building, and writing it so someone else can build it |
| `developer` | Developer | One ready issue at a time | Implementing an issue end to end, branch through pull request |
| `test-engineer` | Test Engineer | `tests/` | Coverage, fixtures, and verifying acceptance criteria independently |

## Why these boundaries

The split follows the direction data flows, so each handoff is a contract
rather than a conversation:

```
data-engineer      Observation
      |
quant-analyst      PillarScore -> CurrencyScore -> PairBias
      |
risk-manager       PositionSize
execution-desk     tradeable / blockers
      |
interface-dev      BiasReport -> report, dashboard
      |
performance-analyst  TradeRecord -> evaluation -> back to macro-strategist
```

`macro-strategist` sits above the flow and decides what the model should
believe. `performance-analyst` closes the loop by measuring whether it was
right. `code-reviewer` sits outside it and reviews everything.

Two agents hold a veto in their own area. `risk-manager` can refuse any change
that could breach the per-trade risk cap. `execution-desk` can refuse any change
that would turn a directional bias into an entry trigger.

## Standards

Technical agents load the `engineering-standards` skill before writing code. It
carries SOLID, KISS, YAGNI and DRY as they apply here, the error handling and
docstring rules, and the table of defects that motivated each one. Every rule in
it exists because something in this repository went wrong in that exact way.

The architect holds the team to it and records settled decisions in
`docs/decisions/`.

## How work flows

Work enters as an issue and leaves as a merged pull request. Nothing skips a
stage, and one stage cannot be skipped by an agent at all.

```
  audit, review, or idea
          |
          +--> type:defect / type:debt --> questions? --> status:needs-decision
          |                                                      |
          |                                          architect answers
          |                                                      v
          |                                              status:ready
          |                                                      |
          +--> type:proposal --> status:needs-approval           |
                                        |                        |
                                 HUMAN APPROVES                  |
                                        |                        |
                                        v                        |
                               type:requirement ----------------->
                                                                 |
                                                                 v
                              branch -> tests -> code -> pull request -> review -> merge
```

The approval gate is the one rule with no agent override. A proposal never
becomes work without a human, including when it looks obviously correct.
Obviousness is the feeling that reliably precedes an unwanted change.

`status:ready` is the only state a developer may claim, and it means every open
question on the issue has been answered. An issue that fails the definition of
ready goes back to the architect rather than being started carefully in the
wrong direction.

The full lifecycle, the labels, what an unattended agent may and may not do, and
the definitions of ready and done are in the `issue-workflow` skill.

## Standing rules every agent carries

- The engine never emits an entry price. Fundamentals set direction, technicals
  set timing, and the trader supplies the entry.
- No file in this repo claims a backtested edge that has not been measured.
  Until the journal holds enough trades, every weight in the config is a prior.
- `src/fbe/types.py` is the shared vocabulary. Changing it is a breaking change
  and every consumer updates in the same commit.
- House writing style: no em dashes, no en dashes as sentence punctuation,
  plain direct sentences, no filler.
