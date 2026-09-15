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

### `realised_pnl_today` comes from the journal

Sum `TradeRecord.outcome_zar` over records whose `closed_at` falls on the run
date. The field is documented as realised profit and loss in the account
currency net of costs, and `RiskConfig.max_daily_loss` is a fraction of balance
in the same currency, so no conversion is involved.

Two states must stay distinct, and this is the load-bearing half of the
decision:

- The journal was read and no trade closed on the run date: `0.0`. That is a
  reading. The limit runs and reports clear.
- The journal is absent, unreadable, or was not supplied: `None`. That is
  absence. The limit reports `NOT_PERFORMED`.

Collapsing those two produces `realised_pnl_today = 0.0` meaning "not tracked",
which is the example ADR 0002 rule 1 cites by name.

The sizing output states what it summed, in the form "summed 3 closed trades,
total -R58.40", per #44's requirement that a check reports its basis.

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

**On the daily profit and loss.** The sum has two gaps, a trade never journalled
and a balance movement that was not a trade, and both understate the loss, which
is the dangerous direction. It is still the right answer, because the
alternative is not caution: `NOT_PERFORMED` forever means the daily loss limit
does not exist. A limit that fires correctly whenever the journal is complete,
and states what it counted, beats a limit that never fires. The failure is
bounded and visible, which is the difference between this and substituting a
default: the ticket names the trades it summed, so a person who knows about a
fourth can see the sum is short. "Warn, do not adjust."

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

The daily loss limit becomes real on days the journal is complete and silent on
days it is not, and which of those a given run is depends on the owner's own
habit of writing an entry at entry. That habit is a question for the owner and is
outside this record; whichever way it goes, the limit reports what it did rather
than a pass.

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
