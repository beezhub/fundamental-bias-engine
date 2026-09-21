# 0012. The staleness ramp is derived from the leg's own publication lag and cycle, and the hand-keyed allowance goes

Status: Accepted

## Context

`scoring.freshness` discounts an input's weight by its age. Issue #8 gave each
indicator its own allowance `S`, `IndicatorSpec.max_staleness_days`, so that a
quarterly series stopped reaching zero on the day it was published. The point at
which the discount starts was left as a global ratio:

    s0 = S * (staleness_full_days / max_staleness_days) = S / 3

Issue #126 showed what that does. A punctual quarterly print is 120 days old on
the day it becomes visible, and `S / 3` is 60 to 90 for every quarterly
indicator, so a quarterly series begins life on the falling part of the ramp and
never reaches full weight. AUD and NZD CPI enter INFLATION at 0.60 of their
declared sub-weight on publication day and fall to 0.225 by the end of the
quarter. The other six currencies' CPI enters at 1.0. That is a cross-sectional
distortion caused by a publication calendar, and it breaks the invariant that
declared weights are the operative weights.

The verification pass of 19 September, on the issue, established four more
things this record rests on:

- The condition is `lag > S / 3`, which every quarterly leg meets and which a
  monthly leg meets wherever `S < 135`. `policy_rate`, `yield_10y`,
  `equity_index` and `pmi_composite` carry `S = 75`, so their monthly legs enter
  at 0.60 too. The largest distortion is in MONETARY at weight 0.30, not in
  INFLATION at 0.15.
- Nine indicators reach `phi = 0` before their next print is due, on punctual
  data, because their allowance sits below the oldest age a punctual newest
  print reaches. Measured for this record: `policy_rate`, `yield_10y`,
  `equity_index` and `pmi_composite` (monthly legs, `S = 75` against a
  76-day cycle end), `indpro_yoy` (180), `cpi_yoy` and `core_cpi_yoy` (200),
  `current_account_gdp` (210) and `gdp_nominal_usd` (916), the last four
  against a 212-day or 918-day cycle end.
- 36 per-currency legs carry a `SeriesRef.frequency` that differs from their
  `IndicatorSpec.frequency`. Any rule keyed on the spec's frequency is wrong for
  those legs, and the existing registry-walk test already makes that
  substitution.
- Acceptance criterion 4 on the issue asserted on a quantity where the defect
  has already cancelled. `blend_components` renormalises over the sub-weight
  present, and both INFLATION components share one release, so the factor
  divides out. The quantity that carries the defect is
  `PillarScore.freshness_factor` and the effective pillar weight after
  `apply_staleness_penalty`.

Measuring the registry for this record turned up one more fact. The candidates
on the issue all define "on schedule" by `DEFAULT_PUBLICATION_LAG_DAYS`, one
number per cadence. On `VERIFIED_ON`, 2026-09-09, 46 of the 135 verified refs
have a newest observation older than that lag plus one cycle of their cadence,
and 44 of them are FRED's mirror of OECD series, which the registry's own note
says "runs two months behind". Those legs are on their source's normal
schedule. A ramp keyed on the cadence alone would discount them for their
source's routine delay, which is this defect again with "source" in place of
"cadence".

## Decision

**The ramp for one input is derived from two facts about the leg that
produced it: its publication lag and its cycle.** There is no hand-keyed
allowance and no global ratio.

For a leg with publication lag `lag` and cycle `cycle`, both in days:

    s0 = lag + cycle          the oldest a punctual newest print gets
    S  = lag + 2 * cycle      one full cycle late, and worth nothing

    phi = 1.0                 for s <= s0
    phi = (S - s) / (S - s0)  for s0 < s <= S
    phi = 0.0                 for s > S

`lag` is the leg's, resolved by `registry.publication_lag`:
`SeriesRef.publication_lag_days` where the data engineer has measured one,
otherwise `DEFAULT_PUBLICATION_LAG_DAYS[frequency]`. It is the same number
`BasePillar._visible` uses to decide when a historical run could have seen the
observation, so "when does this source publish this series" is stated once and
read twice. Issue #222 lands it.

`cycle` is `CYCLE_DAYS[frequency]`, the longest calendar gap between
consecutive periods of a punctual series:

| Frequency | `lag` | `cycle` | `s0` | `S` |
| --- | --- | --- | --- | --- |
| DAILY | 1 | 4 | 5 | 9 |
| WEEKLY | 7 | 7 | 14 | 21 |
| MONTHLY | 45 | 31 | 76 | 107 |
| QUARTERLY | 120 | 92 | 212 | 304 |
| ANNUAL | 552 | 366 | 918 | 1284 |
| IRREGULAR | 45 | 31 | 76 | 107 |

Those are the numbers under the default lag table. A leg carrying a measured
lag shifts its own `s0` and `S` by the difference and nothing else.

The frequency is the observation's, `Observation.frequency`, which every
source copies from `SeriesRef.frequency`. Never `IndicatorSpec.frequency`.

Both tables live in `src/fbe/datasources/registry.py`, because the registry is
the module that knows the release calendar and it cannot import from
`pillars/`. `DEFAULT_PUBLICATION_LAG_DAYS` moves there from `pillars/base.py`.

**What each module does.**

- `scoring.freshness(staleness_days, full_days, allowance_days)` is arithmetic
  and takes `s0` and `S` as arguments. It reads no config and no registry.
- `BasePillar.component_freshness` resolves `s0` and `S` per leg from the
  newest observation's frequency and the leg's lag, and calls `freshness`.
  `staleness_allowance(indicator, config)` goes.
- `registry.coverage_report` and `SeriesRef.stale_on` age a ref against the
  same `S` for that ref, so the registry and the scorer cannot give two answers
  about one series. `IndicatorSpec.max_staleness_days` is removed, and
  `datasources/manual.py` reads the derived allowance where it read the field.
- `ScoringConfig.staleness_full_days` is removed. It would otherwise be a
  configured number that nothing reads, which is the config-drift defect in
  reverse. `ScoringConfig.max_staleness_days` stays, read only by the absent-
  pillar sentinels (`BasePillar.staleness_days` on an empty set,
  `missing_score`, `scoring._absent_score`), and its docstring says so. It no
  longer bounds any ramp. Retiring it belongs with #173's decision on the
  sentinel, and #223 records that.
- `scoring.apply_staleness_penalty` drops the age-based fallback. Its input is
  the pillar's measured `freshness_factor`. A score arriving with `None` raises,
  because there is no longer a ramp that can be applied to an age without
  knowing the leg, and applying a monthly-shaped one was a guess. All seven
  pillars are `BasePillar` and measure the factor, so nothing in the tree
  reaches the raise.

**The `min()` over a component's inputs stays.** Issue #126's second sub-case,
`trade_trend` discounted for the age of its `gdp_nominal_usd` denominator, is
not decided here. Its trigger, a second denominator that publishes materially
behind its numerator, stands from the 18 September comment. Under this ruling
the live symptom goes dormant, since a 625-day-old annual print sits inside
`s0 = 918`, but the mechanism that produced it is unchanged.

**Two pull requests, in this order.**

1. #222, `data-engineer`: the two tables move to the registry,
   `SeriesRef.publication_lag_days` and `registry.publication_lag` land,
   `_visible` reads them, the slow legs are keyed from evidence, a registry walk
   asserts every verified leg is inside `lag + cycle` on `VERIFIED_ON`. No ramp
   change.
2. #126, `quant-analyst` with `data-engineer` on the registry side: the ramp,
   the removal of `IndicatorSpec.max_staleness_days` and
   `ScoringConfig.staleness_full_days`, `coverage_report`, `manual.py`,
   `apply_staleness_penalty`, `docs/scoring-spec.md` section 4.1, and the
   tests.

## Alternatives considered

**A per-indicator full-weight age on `IndicatorSpec`, beside the allowance.**
Candidate 1 on the issue. Lost because the unit is wrong. 36 legs differ from
their spec's frequency, so a per-indicator `s0` is right for some of an
indicator's legs and wrong for the rest, exactly as the per-indicator `S`
already is: `policy_rate` carries `S = 75` to serve its monthly legs and hands
its daily legs a 25-day full-weight window. It also adds a second hand-keyed
number that has to agree with the first, and the first is already wrong for
nine indicators in a way nobody caught for a month.

**Derive `s0` from the leg's frequency, keep the hand-keyed `S`.** Candidate 2.
Lost on two counts. Read literally, `s0 = lag` gives full weight only on the
day of first visibility and puts a punctual print on the ramp the next
morning; `s0` has to be `lag + cycle`. And with `s0` derived and `S` hand-keyed,
`S` must be re-keyed anyway for every indicator where it sits below the derived
`s0`, which is the same nine, at which point the table is being maintained to
agree with a formula and should be the formula.

**Derive both from the frequency alone.** Candidate 3 as the quant desk stated
it, and the shape adopted here. Lost only on where the lag comes from. Keyed on
the frequency table alone it discounts 44 FRED legs for their source's routine
two-to-three-month delay, and the hand-keyed allowances that go were carrying
exactly that knowledge (`trade_balance` at 150, `retail_sales_yoy` at 270). The
knowledge survives, moved from `S` onto the lag, where it also fixes the
visibility rule's look-ahead for the same legs.

**Keep `IndicatorSpec.max_staleness_days` for `coverage_report` and let the
ramp derive its own `S`.** Lost because it reintroduces two answers about one
series, which is what #8 removed. With the ramp's `S` at 304 for a quarterly
leg and the registry's at 200, a 250-day-old print scores at partial weight
while the coverage report calls it stale. `tests/test_staleness_ramp.py::
test_the_ramp_is_never_more_permissive_than_the_registry` fails on the first
run. One number, derived once, read by both.

**Make the two multipliers configurable.** Full weight for one cycle and zero
after one more cycle could sit in `ScoringConfig`. Not done, on YAGNI and on
the standard that a free parameter nobody can defend is a conditional in
disguise. "A print one release late is worthless" is a rule that can be
argued; a `ramp_cycles` of 1.5 cannot. If a case for a different shape
arrives it comes with evidence and supersedes this record.

**One pull request.** The ramp and the registry's limit cannot be split,
because the invariant that ties them fails on any intermediate `main`. But the
per-leg lag can land first and is worth more alone than it looks: it is a
look-ahead defect in backtests today, whatever happens to the ramp. Landing
the ramp before it would zero every FRED OECD-mirror leg on live runs, which
is 20 refs at `phi = 0` and 26 more discounted, on the registry as it stands.
That is a regression on the MVP set and it was measured before this was
written.

## Consequences

- A punctual print of every cadence carries `phi = 1.0` from the day it is
  visible until the day its successor is due. A print late by any amount is
  discounted linearly and a print one full cycle late is worth nothing. The
  registry-walk test asserts both at the leg's frequency, which closes the
  spec-frequency substitution the verification named.
- `DEFAULT_PUBLICATION_LAG_DAYS` becomes load-bearing for scoring, not only
  for backtests. An understated lag now costs live weight, where before it
  only flattered a backtest. That is the intended direction: it fails louder.
- The daily cycle of four days means a yield leg is discounted over a closure
  longer than a three-day weekend. At Christmas a UK gilt leg can reach six
  days and `phi = 0.75` for a day or two. Accepted, and gentler than the ramp
  it replaces, which gave 0.60 at the same age.
- The registry loses a per-indicator knob. A source whose behaviour the
  frequency and lag do not describe has nowhere to say so except
  `publication_lag_days`, and a lag is the only thing that knob should be
  saying. If a second kind of fact turns up, it wants its own field with its
  own reader, not a widened allowance.
- `ScoringConfig` loses a field. Removing it is a config change the owner sees
  in the pull request. It is not one of the four kinds of decision that are
  theirs: how a threshold is derived is the desk's, and the field being removed
  has no meaning once the ramp no longer reads it.
- `apply_staleness_penalty` raises on a score without a factor. A future
  `Pillar` implementation that is not a `BasePillar` must measure its own
  freshness before the scorer will weight it. That is the demand the standards
  make of every input, and it was the fallback that was the exception.
- The 19 September control on the issue, the ramp against the ramp patched to
  1.0 on a fully punctual fixture, must show zero difference in coverage for
  every currency after the fix. It showed 0.045 to 0.131 before. That is the
  end-to-end acceptance test, and it is the one that does not move every time
  another pillar lands.
- Nothing here has been measured against returns. The ramp is a prior about
  when a number stops being information, and the journal is where it gets
  tested.
