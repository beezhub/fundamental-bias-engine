# 0006. Where the limit inputs come from, and what a breached limit does

Status: Accepted

## Context

Issue #44 landed `LimitStatus`, `LimitCheck` and `LimitReport`, so each of the
four portfolio limits in `docs/risk-and-execution.md` section 4 now reports
clear, breached, or not performed, and a caller can no longer obtain an
all-clear without having checked anything. `check_limits` takes
`realised_pnl_today: float | None` and `equity_peak: float | None`, both
defaulting to `None`.

Issue #46 is the remainder: nothing supplies either of those two inputs, so two
of the four limits report `NOT_PERFORMED` on every run, and three documents
disagree about what a breach does. Section 4 says a daily-loss breach means the
session is over. The `size` docstring in `src/fbe/cli.py` says a
correlated-exposure collision comes back as a warning. The exit-code table in
`docs/interfaces.md` lists exit 3 for a calendar blackout only.

`docs/decisions/0002-representing-not-known.md` names `check_limits` directly
and is the convention this record works within: absence is represented
explicitly, an unchecked input produces a marker rather than a pass, and the
marker reaches the output.

## Decision

### The journal describes completed days, so every limit that needs today is not performed

**Amended 2026-09-17, after the owner answered #46's first question.** The
original decision here was that `realised_pnl_today` should be the sum of
`TradeRecord.outcome_zar` over records whose `closed_at` falls on the run date,
treated as a reading rather than an absence. That was wrong, and the correction
is recorded rather than quietly rewritten, because the reasoning that produced it
is the more useful half.

The owner does not write journal entries at entry. Trades arrive as a broker
email statement, once a day, with a weekly one as well. So the journal is
complete up to the last statement and blind to today.

Against that, the original sum returns `0.0` on every live run, because nothing
that closed today is in the file yet. Under the rule below `0.0` is a reading, so
the daily-loss limit would have reported **clear** every day while being
structurally unable to see today's losses. That is `realised_pnl_today = 0.0`
meaning "not tracked", which ADR 0002 rule 1 names by name, produced by the
decision meant to prevent it.

The rule that replaces it covers all four limits at once:

**The journal is a record of completed days. Any limit whose input must describe
today reports `NOT_PERFORMED` on a live run, and the ticket says why.**

For a run dated after the journal's last complete date, that is
`max_concurrent_positions`, `max_correlated_exposure` and `max_daily_loss`,
joining `max_drawdown_pause`, which has no source at all.

The reading-against-absence distinction survives and is keyed on coverage rather
than on emptiness:

- Run date inside the journal's coverage, nothing closed that day: `0.0`. A
  reading.
- Run date after the journal's coverage: `None`. An absence.

The same applies to the open book. A count of zero open records is a reading for
a date the statement covers and is not knowledge of today: against a next-day
statement a trade opened this morning is not in the file, and one opened and
closed today never appears as open at all.

Where the coverage date comes from is `journal.py`'s to decide, and the file's
last-written time is not it: a file written this morning may hold a statement
covering the day before yesterday. **If the journal cannot state its coverage
date, all four limits are `NOT_PERFORMED` unconditionally**, which is the safe
reading and needs no new field.

The sizing output states the journal path, the date the journal is complete to,
and the open count as of that date, per #44's requirement that a check reports
its basis.

### `equity_peak` has no source, and the documents say so

`equity_peak` stays `None` and the drawdown limit reports `NOT_PERFORMED` on
every run. `docs/risk-and-execution.md` sections 4 and 8 state that the drawdown
pause is checked by hand.

### A breached limit exits 3, and `--force` covers only the two attention limits

| Limit | Breached | `--force` applies |
| --- | --- | --- |
| `max_concurrent_positions` | exit 3 | yes |
| `max_correlated_exposure` | exit 3 | yes |
| `max_daily_loss` | exit 3 | no |
| `max_drawdown_pause` | exit 3 | no |

`NOT_PERFORMED` is not a breach. A run with limits not performed exits 0 and
prints each one.

The three documents that currently disagree are brought into line in the same
change that implements this.

## Why

**On the daily profit and loss.** The original reasoning was that a limit firing
correctly whenever the journal is complete beats one that never fires, and that
the failure would be bounded and visible because the ticket names what it
summed. The premise was that the journal is usually complete and occasionally
behind. It is never complete for today, so the limit would never have fired
correctly and the visible basis would have read "summed 0 closed trades" on a
day with three losses. A bounded failure and a structural one look the same in a
docstring and are not the same thing, and the way to tell them apart is to ask
where the data actually comes from, which is a question only the owner could
answer.

**On what is left.** `NOT_PERFORMED` on every live run is not caution dressed as
rigour: it is the true statement, and it is what proposal #3's own falsification
called for, that if entries are not written at entry the position limits stay
manual. The limits block still earns its place by printing the journal path, the
coverage date and the open count as of that date, so the trader does the last
step by hand knowing exactly what was and was not checked. It also defines the
feature that turns the limits on, which is something that reads the daily
statement, with no threshold or guesswork in between.

**On the equity peak.** Each alternative produces a plausible wrong number or a
chore. Deriving a peak from `account_balance_at_entry` is wrong across a
withdrawal, and it is the only balance series that exists. A configured value in
`RiskConfig` must be raised by hand after every new high, and a stale one
silently understates drawdown. A hand-maintained snapshot in `data/manual/` is a
chore, and the owner rejected a chore on proposal #5 on the ground that one done
on most days and missed on some is worse than none. Reporting honestly that the
limit was not checked is the only option that does not risk a wrong number, and
it is what the code already does; the change is the documentation admitting it.

**On the breach behaviour.** Section 4 already draws the line this follows: three
positions open is a wait, down 4% today is a stop for the day. Exit 3 is defined
in `docs/interfaces.md` as a guard rule refusing, "not an error, a decision",
which is what all four are. Exit 1 would say the result should not be trusted,
and a full book is not an untrustworthy result. Exit 0 with a warning repeats the
defect #44 fixed, since a pass is what authorises a trade. `--force` splits
because the two capital limits are the only rule protecting the account from a
bad day, and an override on those turns a stop into a prompt, while the two
attention limits are about the trader's capacity to manage open tickets, which
is theirs to judge on a given afternoon.

## Consequences

All four limits report not performed on a live sizing run, until something reads
the broker's daily statement. `fbe size` therefore checks nothing today and says
so on every ticket, which is the point rather than a shortcoming: the block it
replaces reported four limits passed on no evidence.

The drawdown limit stays unperformed until a balance history exists. Phase 6's
journal evaluation is the natural home, and this record does not pre-empt how it
is built. When a source appears, the limit turns on with no further decision.

No value in `RiskConfig` changes and none is added. A threshold for "the journal
looks stale" would be a new `RiskConfig` value and needs the human gate, so this
record does not introduce one.

Reopened if a balance history lands, if the journal turns out not to be written
at entry in practice, in which case the honest answer is to make the position
limits manual and say so, or if `--force` on an attention limit is used often
enough to suggest the cap is set wrong rather than the trade being wrong.
