# 0002. Absence is represented explicitly, never as a passing value

Status: Accepted

## Context

Almost every serious defect found in this repository produced a plausible value
rather than an exception. The `engineering-standards` skill opens with the table
and states the corollary: prefer a loud failure to a quiet default.

One layer already does this well. A pillar that cannot score a currency returns
`missing_score`, which is `0.0` with `raw` and `z` both `None`, and the scorer
detects the absence through those `None` markers, excludes the weight from the
composite, and reports the shortfall as reduced `coverage`. Absence of evidence
and evidence of neutrality are different facts and the contract can say which one
it holds.

Three other places cannot. A product analyst pass filed three separate proposals
which, read together, are one defect wearing three coats:

- **The calendar.** `bias.apply_filters` takes an optional `CalendarGuard`. A
  guard that returns an empty sequence because there are genuinely no events and
  a guard that returns an empty sequence because the scrape returned an error
  page are the same value. `docs/answers/data.md` question 7 records that the US
  Bureau of Labor Statistics, the source for CPI and Non-Farm Payrolls, returns
  HTTP 403 to automated requests, so the failing case is not hypothetical.
- **The limit checks.** `risk.check_limits` takes `open_positions`,
  `realised_pnl_today` defaulting to `0.0`, and `equity_peak` defaulting to
  `None`. An empty position list means "no positions open" and also means "the
  caller does not track positions". A `realised_pnl_today` of `0.0` means "flat
  today" and also means "not tracked". Both readings return an empty refusal
  list, which the caller reads as every limit cleared.
- **Broker constants.** `DEFAULT_BROKER` carries plausible retail figures under a
  docstring shouting that the owner must confirm every value. Nothing in the type
  records whether they were confirmed. A sized position built on an unconfirmed
  `min_lot` is indistinguishable from one built on a verified one.

`registry.SeriesRef` shows the pattern working outside the pillars:
`stale_on` returns `True` when `last_observed` is unknown, and says why in its
docstring, so unverifiable freshness counts as stale rather than as fresh.

Settling this three times separately would produce three different conventions
for one question, and the fourth instance would arrive with no convention at all.

## Decision

Anywhere the engine can hold a value that was not checked, "not known" is
represented in the type, distinctly from "known to be fine", and the distinction
survives to the output.

Four rules, in force across the pipeline.

**1. Absence is a value in the contract, not a sentinel in the range.** Use
`None`, a separate field, or a distinct return type. Never a value inside the
normal range that a consumer could mistake for a reading. `PillarScore.raw` and
`z` set to `None` is the reference. `realised_pnl_today = 0.0` meaning "not
tracked" is the anti-pattern: zero is a real profit and loss figure.

**2. An unchecked input produces a marker, not a pass.** A check that could not
run says so and the marker reaches the report. `apply_filters` already appends
`event:unchecked` and `cost:unchecked` for this reason, and its docstring states
the principle: silence and an all-clear must not look the same.

**3. A marker that is not rendered does not exist.** The two renderers currently
print `blockers` only when `tradeable` is false, so `event:unchecked` on a
tradeable pair is invisible. A convention that stops at the dataclass boundary
protects nobody. Issue #17 carries this.

**4. A source that failed is distinct from a source that returned nothing.** A
guard, fetch or check that errored must be able to say so rather than returning
an empty result. This is the rule the calendar breaks today.

What this decision does not do: it does not choose whether the calendar blackout
fails open or fails closed. That is the execution desk's call and is open in
issue #24. This decision only requires that whichever direction is chosen, the
engine can tell the two cases apart, because a fail-open policy and an
undetectable outage are not the same thing even though they produce the same
table.

## Alternatives considered

**Raise on every unchecked input.** Consistent with `MissingRateError`, which is
the right behaviour when a rate is missing because sizing without it is not
possible. Rejected as a general rule because it is wrong for the bias layer. An
offline run with no calendar should still produce biases: fundamentals are the
slow half of the model, the trader reviews the calendar themselves as part of the
daily routine, and refusing to score because a scraper broke would throw away the
part that still works. The distinction that matters is between an input the
engine cannot compute without, which raises, and an input the engine can note the
absence of and continue, which marks.

**Let each layer choose its own convention.** Cheapest, and each specialist knows
their own layer best. Rejected because the failure crosses layers. A `TradeIdea`
carries a `PairBias` whose calendar was unchecked, a `PositionSize` built on
unconfirmed broker constants, and a `blackout_until` that may be `None` for two
different reasons. A reader holding that object needs one rule to apply, not
four.

**Add a single `confidence` or `quality` score to every type.** Compresses the
question into one number, which reads well on a dashboard. Rejected because it
loses the thing that matters. "The calendar was not checked" and "the broker
constants are unconfirmed" call for different actions, and a blended 0.7 tells
the reader neither. The same argument section 4.5 of the scoring spec makes about
rank: a scalar discards the structure that was the signal.

## Consequences

Good. One rule to apply and one rule to review against, so the fourth instance of
this problem arrives with a convention already in place. The `code-reviewer`
checklist gains a question with a yes or no answer: can this value be produced by
an input that was never checked, and if so, can the consumer tell.

Bad, and worth naming. More `None` in the contracts, which means more branching
at the consumers, and `mypy` will find every site rather than the important ones.
Markers accumulate: a fully offline run will carry several per pair, and a report
showing four unchecked markers on all 28 rows is close to showing none, which is
banner blindness arriving by a different route. The renderers will need to
aggregate rather than repeat, and that is an interface problem this decision
creates rather than solves.

It also costs a `types.py` change if the broker confirmation state lands on
`PositionSize` or on `Broker`, which is a breaking change under the rule in
`CLAUDE.md` and needs every consumer updated in the same commit.

And it does not make anything correct. A marked absence is still an absence. This
decision buys the ability to see the gap, not the gap being filled, and the risk
is that a visible marker feels like a fix and the underlying integration never
gets built.

## References

- `.claude/skills/engineering-standards/SKILL.md`, the prime directive and the
  error handling section.
- Issues #2, #3 and #4, the three proposals this decision generalises. They
  remain proposals and remain subject to the human approval gate: this record
  fixes the convention they should be built against, and does not approve any of
  them.
- Issue #17, the renderer half of rule 3.
- Issue #24, the open question on the calendar fail direction.
- `src/fbe/pillars/base.py::missing_score` and
  `src/fbe/datasources/registry.py::SeriesRef.stale_on`, the two places the
  convention already holds.
