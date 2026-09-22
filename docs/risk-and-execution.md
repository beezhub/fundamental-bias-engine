# Risk and execution

This is the operator's document. It covers how a bias becomes a ticket: what to
risk, how many units that is, which trades the limits refuse, when to stand
aside for news, and what to write down afterwards.

The engine never sends an order. It produces a directional lean and a size. The
entry stays where the trading plan puts it, on the trendline or channel touch on
the 1h and 4h charts.

Everything here assumes the plan's account: R2,000, ZAR denominated, 1-2% risk
per trade, which is R20 at 1% and R40 at 2%.

---

## 1. Why pip value is the hard part

The account is in rand. The tradeable universe is 28 G10 crosses. Not one of
them has ZAR on either leg.

That means every position's risk is created in a foreign currency and has to be
carried back to rand before it can be compared to the R20-R40 band. A EURUSD
position earns and loses in dollars. A USDJPY position earns and loses in yen.
Neither is directly comparable to a rand risk budget.

There are three cases in general, and this account is always in the third.

| Case | Example | Conversion |
|---|---|---|
| Quote currency is the account currency | USD account, EURUSD | None needed |
| Account currency is the base of the pair | USD account, USDJPY | Divide by the pair's own price |
| Account currency is in neither leg | **ZAR account, any G10 cross** | Multiply by a third rate: quote currency to ZAR |

The third case needs market data the pair itself does not supply.

### Finding that third rate

`convert_rate` resolves it in a fixed order, first match wins.

1. **Identity.** Same currency both sides, factor 1.0. The only time this module
   ever produces a 1.0.
2. **Direct.** `USDZAR` is present, so USD to ZAR is 18.50.
3. **Inverted.** `ZAR` to `USD` is `1 / 18.50`.
4. **One hop through the US dollar.** Tried only when neither direct form
   exists.

Rule 4 is not an edge case on this account, it is the normal route. A feed that
gives you `{"USDZAR": 18.50, "USDJPY": 155.00}`, which is exactly what the
pre-trade checklist asks for, has no `JPYZAR` and no `ZARJPY`. Rules 2 and 3
both miss. Rule 4 resolves JPY to USD as `1 / 155.00`, then USD to ZAR as
`18.50`, and multiplies:

```
JPY to ZAR = 18.50 / 155.00 = 0.119355 rand per yen
```

The pivot is the dollar because it is the one currency with a liquid quoted leg
against every G10 currency **and** against the rand. Your feed will have
`USDZAR`. It will not have `ZARCHF`.

No second pivot is attempted. If a one-hop dollar route cannot be built, the
rate set is not fit for sizing the trade and `convert_rate` raises
`MissingRateError`. It does not fall back to 1.0, it does not reuse a stale
rate, it does not carry forward the last successful conversion.

The reason is worth being blunt about. A wrong conversion factor produces no
visible symptom. The size function still returns a number, the ticket still
fills, the stop still sits exactly where the chart said it should. The only
thing that changes is how much money is at risk, and nothing on the screen
reports it. Treating a missing USDZAR as 1.0 would size roughly eighteen times
too large, turning a 1% trade into an 18% trade, on every trade, until someone
noticed. A loud failure that refuses to size the trade is the only acceptable
behaviour.

`position_size` refuses four malformed inputs for the same reason, before it
computes anything: an entry or a stop that is not a finite positive price, a
lot step that is not positive, and a balance that is not a finite positive
amount. None is a fact about the trade, so none comes back as a warning, which
`docs/risk-and-execution.md` section 8 lets the owner read as a pass. The
balance is the newest of the four and nothing else checks it:
`Config.validate` does not, so `RiskConfig(account_balance=0.0)` is
constructible, and unchecked it reaches `realised_risk_fraction` as a
denominator.

---

## 2. Position sizing, worked with real numbers

The formula, in one line:

```
units = risk_amount / (stop_distance_in_pips x ZAR value of one pip per unit)
```

Rates used in both examples, which you must replace with live rates:

* USDZAR = 18.50
* USDJPY = 155.00
* Contract size = 100,000 units per standard lot

### Example A: EURUSD long, 1% risk

Setup: price has broken above a descending trendline on the 4h and retested.
Entry 1.0850, stop 1.0825, just beyond the retested line. Target 1.0925 at the
prior swing high.

1. **Risk amount.** R2,000 x 0.01 = **R20.00**
2. **Stop distance.** (1.0850 - 1.0825) / 0.0001 = **25.0 pips**
3. **Pip value per unit.** The quote currency is USD, so one pip on one unit is
   0.0001 USD. Convert to rand at USDZAR:
   0.0001 x 18.50 = **R0.00185 per pip per unit**
4. **Units.** 20.00 / (25.0 x 0.00185) = 20.00 / 0.04625 = **432.4 units**
5. **Lots.** 432.4 / 100,000 = **0.0043 lots**

Now the broker gets a say. If the minimum is 0.01 lots, which is 1,000 units,
this trade **cannot be taken**. Not "round up to the minimum": at 1,000 units
the same 25 pip stop costs 1,000 x 0.00185 x 25 = **R46.25**, which is 2.3% of
the account and outside the plan's band entirely.

If the broker offers 0.001 nano lots, which is 100 units, round **down** to 400
units.

6. **Realised risk.** 400 x 0.00185 x 25 = **R18.50**, against an intended
   R20.00. Inside the band, below the target, which is the correct direction to
   miss in. This is the figure the checklist and the journal read, not the
   R20.00.
7. **Notional, in rand.** Face value is 400 x 1.0850 = 434.00 **USD**, which is
   not a rand figure and cannot be shown as one. Carry it back through the same
   leg as the risk: 434.00 x 18.50 = **R8,029.00**, about 4.0 times the account
   balance. That is the leverage number.
8. **Reward to risk.** (1.0925 - 1.0850) / (1.0850 - 1.0825) = 0.0075 / 0.0025 =
   **3.0R**. Clears the 2.5R minimum required at LOW conviction.

### Example B: USDJPY short, 2% risk

Setup: price rejected the upper channel line on the 4h with a bearish engulfing
bar. Entry 155.00, stop 155.30, just beyond the channel. Target 154.10 at the
lower channel line.

1. **Risk amount.** R2,000 x 0.02 = **R40.00**
2. **Stop distance.** (155.30 - 155.00) / 0.01 = **30.0 pips**. Note the pip
   size: JPY pairs are quoted to two decimals, so a pip is 0.01, not 0.0001.
3. **Pip value per unit.** This is the conversion leg, shown explicitly. One pip
   on one unit is 0.01 JPY. Rand per yen is not quoted directly, so derive it:

   ```
   JPY to ZAR = USDZAR / USDJPY = 18.50 / 155.00 = 0.119355 rand per yen
   ```

   Then: 0.01 x 0.119355 = **R0.00119355 per pip per unit**
4. **Units.** 40.00 / (30.0 x 0.00119355) = 40.00 / 0.0358065 = **1,117.1 units**
5. **Lots.** 1,117.1 / 100,000 = **0.0112 lots**

At a 0.01 lot minimum, round down to 1,000 units. **This trade is tradeable at
a standard micro-lot broker and the EURUSD trade above was not.**

6. **Realised risk.** 1,000 x 0.00119355 x 30 = **R35.81**, which is 1.79% of
   the account. Inside the band, against an intended R40.00.

   That percentage is `PositionSize.realised_risk_fraction`. The plan states
   its rule as 1-2% of balance rather than as R20 to R40, so the share is the
   form the check is actually made in, and the engine holds it rather than
   leaving a report to divide two money fields at render time.

   That gap is about 10.5%, and it is the reason `realised_risk_amount` exists as its
   own field. Journal this trade against R40.00 and a R71.61 win reads as
   +1.79R when it was actually +2.00R. Every R-multiple in the file would be
   wrong by the same ratio, and those R-multiples are the only evidence the
   conviction model is ever judged on.

7. **Notional, in rand.** 1,000 x 155.00 = 155,000 **JPY**. Through the same
   conversion leg: 155,000 x 0.119355 = **R18,500.00**, or 9.25 times the
   account balance. Leaving this in yen would have printed "155,000" on a ticket
   whose money fields are all supposed to be rand.
8. **Reward to risk.** (155.00 - 154.10) / (155.30 - 155.00) = 0.90 / 0.30 =
   **3.0R**.

At a 0.001 nano-lot minimum, round down to 1,100 units instead: realised risk
1,100 x 0.00119355 x 30 = **R39.39**, notional R20,350.00.

### Why the two examples differ

A yen pip is worth less per unit than a dollar pip, about R0.0012 against
R0.0019 here. Less value per pip means more units for the same rand risk, and
more units clears a broker minimum that fewer units does not.

The practical consequence on this account: **JPY-quoted pairs size larger and
are the ones most likely to be tradeable.** That is a mechanical fact about
quoting conventions, not a view on the yen, and it should not be mistaken for a
reason to prefer those pairs on merit.

### Rounding is always down

Never nearest, never up. On a R2,000 account the gap between one micro lot and
two is a doubling of risk, not a rounding error. Rounding down costs a few cents
of expected profit and keeps the rule intact.

---

## 3. Conviction to size ladder

Conviction modulates size. It does not gate entry, except at NONE.

| Conviction | Position in band | Risk fraction | On R2,000 | Minimum reward to risk |
|---|---|---|---|---|
| HIGH | top | 2.0% | R40.00 | 1.5R |
| MEDIUM | midpoint | 1.5% | R30.00 | 2.0R |
| LOW | bottom | 1.0% | R20.00 | 2.5R |
| NONE | no trade | no trade | R0 | no trade |

The risk fractions are **derived, not fixed.** Each level holds a position in
the band, and the band's ends are `risk_per_trade_min` and `risk_per_trade_max`
in config. With the plan's 1% and 2% that produces the column above.

This matters when you change the band. If a review sends you to lower
`risk_per_trade_max` to 1.5%, the whole ladder moves down with it: HIGH becomes
1.5%, MEDIUM 1.25%, LOW stays 1%. A hardcoded ladder would keep asking for 2%,
`position_size` would clamp it and attach a warning, and the checklist below
would then read that warning as a refusal. Every high-conviction setup would be
rejected because two numbers disagreed.

Three things about this table.

**Size rises with conviction, and the reward requirement falls.** They run in
opposite directions on purpose, and the reason is an assumption rather than a
finding: that hit rate rises with conviction, so a strong case can pay off at a
nearer target while a thin one has to pay more on the occasions it works.

**That assumption is untested.** It is exactly what `evaluate` measures, and by
that function's own standard it needs roughly 30 closed trades per bucket before
anyone should believe it. Confirmation looks like `hit_rate` rising from LOW
through MEDIUM to HIGH. Refutation looks like hit rate flat or inverted across
buckets, and if that is what the journal shows, the reward ladder has no basis
and should collapse to one minimum applied to every trade. The numbers above are
a starting position chosen because it is the conservative one: if the assumption
is wrong, demanding more reward on the trades the model is least sure of costs
missed trades rather than lost money.

**LOW conviction is still a trade.** Refusing everything below HIGH sounds
disciplined and is not: it would leave the account idle for weeks and push you
toward taking marginal setups out of boredom, which is the overtrading the plan
warns about. A LOW conviction pair with a clean channel touch is a 1% trade.

NONE means the engine has no view. There is no bias layer to add to the chart,
so there is no trade this system is entitled to an opinion on.

---

## 4. Limits, and what each one is for

`check_limits` reports one outcome per limit, not a yes or no, because each limit
means something different about what to do next.

| Limit | Default | What it is actually protecting |
|---|---|---|
| `max_concurrent_positions` | 3 | Attention, not capital. The plan runs on 1h and 4h charts managed by hand. A fourth open ticket is where management degrades and stops get moved. |
| `max_correlated_exposure` | 4% per currency | Long EURUSD and long GBPUSD is one short-USD bet wearing two tickets. Both lose together on any dollar rally. Counting them as two independent 1% trades understates the real exposure by half. |
| `max_daily_loss` | 4% (R80) | Revenge trading, given a number. When it bites, the session is over. Realised losses only: an open trade sitting underwater is not yet evidence of anything. |
| `max_drawdown_pause` | 10% (R200 from peak) | The model, not the trader. A drawdown this deep on a fundamental bias engine more likely means the weights are wrong than that variance was unkind. Stop and review before resizing. |

On correlated exposure, the full risk of a position is attributed to **both** of
its legs, not split between them. A pair trade genuinely does stake the whole
amount on each leg's behaviour. This is conservative and it will occasionally
overstate a genuinely hedged book. On a R2,000 account that is the right error
to make.

### Three outcomes per limit, because two is not enough

Each limit comes back **clear**, **breached** or **not performed**.

Not performed means an input the limit needs was not supplied, so nothing was
compared. It is not a pass. `check_limits` used to return an empty list of
refusals in that case, which read as every limit cleared, and the only caller
that could have produced one supplied no open positions, no daily profit and
loss and no equity peak. An empty answer from a check with nothing to check
against is the shape of defect `docs/decisions/0002-representing-not-known.md`
exists to stop.

What each outcome does to the ticket:

* **Clear.** The limit was compared and there is room. The detail line carries
  the measured figure and the ceiling, so the number can be read rather than
  trusted.
* **Breached.** The limit was compared and there is not. It prints on the ticket
  with the figure, and for correlated exposure with the currency named, because
  "too much exposure" does not tell you which ticket to drop. Whether a breach
  also changes the exit code is open in issue #46.
* **Not performed.** Printed as not performed, never as clear. It does not block
  the ticket: two of these four limits have no automated source at all today, so
  refusing on absence would refuse every trade, and a gate that refuses
  everything is a gate that gets switched off along with the checks that were
  working. It does mean the run is not all clear, and it is your cue to check
  that one limit by hand in the terminal before the box in section 8 is ticked.

### What is automated today, stated plainly

`fbe size` reads the journal, counts the records that are still open, and prints
the count, the journal path and the time the file was last written. On that
basis the concurrent and correlated limits are performed. If the journal cannot
be read or a line will not parse, the open book is **not known**, which is
different from empty, and both position limits report not performed rather than
clear.

**The daily-loss and drawdown limits report not performed on every run.** Nothing
supplies today's realised profit and loss, and nothing anywhere in this
repository records an equity peak: `TradeRecord` holds
`account_balance_at_entry`, which is a balance at an entry time, not a peak, and
is wrong across a withdrawal. Until a source exists, those two limits are
checked by hand. Issue #46 decides whether the journal is an honest enough source
for the first and where the second could come from. No part of this document
claims either limit is automated.

The journal is also only as current as the last entry written. The plan's
routine allows the journal entry to be written in the evening, and a position
opened this morning and not yet journalled is invisible to the count. That is why
the ticket prints the basis rather than only the verdict. Policy for a stale
journal is issue #46's first question.

---

## 5. Blackout policy

The plan says avoid trading during high-impact news. The guard makes it
checkable.

**Window: 30 minutes before a release, 60 minutes after.**

The asymmetry is deliberate. Thirty minutes before covers the pre-positioning
drift and the liquidity thinning. Sixty minutes after is longer because **the
first move is frequently wrong.** Price spikes on the headline, then reverses as
the detail is read, the revisions are noticed and the algorithmic flow unwinds.
Entering on the spike means entering at the worst price of the hour, in the
direction about to fail. Standing aside through the reversal, not just through
the release, is the whole point.

**Either leg blocks the pair.** A EUR event blocks EURUSD regardless of what the
dollar is doing. The euro is half the price. Checking only the quote currency is
the easy mistake, because the quote currency is where the pips are, and it would
leave every EUR, GBP, AUD and NZD release unguarded on exactly the dollar pairs
the plan trades most.

**The ten categories from the plan** are encoded in `HIGH_IMPACT_KEYWORDS` and
matched against event titles, on top of whatever impact rating the feed
publishes: NFP, interest rate decisions, GDP, CPI and PPI, retail sales, UK and
Canada employment, trade balance, central bank speeches and press conferences,
FOMC minutes and ECB accounts, geopolitical events and summits. Feeds mislabel.
An unscheduled ECB remark tagged medium impact still moves a pair forty pips.
The keyword match catches those.

**Clear and unknown are different answers, and only one of them is clear.**
`is_blacked_out` can return three things: blocked, clear, or unknown. Unknown
means the calendar it was given does not reach the moment being checked at
all, most often because a fetch failed or a cached week does not extend to
today, and it is not a quieter version of clear. Before this distinction
existed, both cases returned the same value, `(False, None)`, so a broken
scrape and a genuinely quiet morning were indistinguishable on the page. The
pre-trade checklist below only lets the news box be ticked on a real clear
answer; on unknown, it says to check the calendar by hand and to write down
that the guard could not.

### Holding through an event is a different decision

Entering into a window and holding through one are not the same choice, and the
guard treats them separately.

Declining an entry is free. The setup either survives the window or it does not,
and skipping it costs a missed trade.

Exiting is not free. Closing to dodge a release pays the spread twice, abandons
a stop placed on structure, and gives up the trade's remaining expectancy on the
strength of an event that might not touch it. A rule that flattens everything
before every high-impact print bleeds the account through costs alone.

The distinction is buffer, measured against `TIGHTEN_BUFFER_R`, which is
**1.0R**. Inside a window there are two branches and no gap between them:

* **At or above 1.0R: TIGHTEN.** The position can absorb a full stop-distance
  move against it and still be at breakeven, which is the size of adverse move a
  high-impact release routinely produces. Manage it with the plan's own tools.
  Take partial profit at the nearest support or resistance, or pull the trailing
  stop in.
* **Below 1.0R: FLATTEN.** This includes a position in modest profit, not only
  one at or below breakeven. Up 0.5R with a rate decision ten minutes out is
  less than half a stop of cover, and a spike through a structural stop is
  precisely the loss the news rule exists to prevent. Close it and re-enter
  after the window if the setup survives.

The comparison is inclusive: exactly 1.0R holds. Open profit is measured in R
against `realised_risk_amount`, the same denominator the journal uses, so the
number on the screen and the number in the file mean the same thing. 1.0R is a
threshold, not a measurement. Revisit it once the journal can group outcomes by
`exit_reason` and show what holding through windows has cost or saved.

This connects to the plan's time-based exit. A trade that has stalled and is
drifting toward a scheduled release is not waiting for its thesis, it is waiting
for a coin flip. The stall and the approaching event are the same signal.

---

## 6. Journal

Storage is `data/journal/trades.jsonl`, one JSON object per line, append-only.

JSONL rather than a spreadsheet because a crash damages one line instead of the
file, there is no cell to fat-finger and no formula to break, it diffs and
versions cleanly, and it holds the nested bias snapshot natively. A record
cannot be quietly edited after a bad week, which matters when the file is
evidence about your own discipline. Export to CSV when you want to look at it.
That is a viewing format, not a storage format.

Corrections are appended with the same `trade_id`. Readers keep the last line
per id and the superseded line stays in the file.

### What each record holds

**Execution:** `trade_id`, `pair`, `direction`, `opened_at`, `closed_at`,
`entry`, `exit_price`, `stop`, `target`, `units`, `lots`, `broker`.

**Risk:** `risk_amount` and `risk_fraction` in ZAR, populated from
`PositionSize.realised_risk_amount` and never from the intended `risk_amount`,
plus `account_balance_at_entry` and `account_currency`.

**Outcome:** `outcome_zar` net of costs, and `r_multiple`, which is
`outcome_zar` divided by the realised risk. R multiples are the only comparable
measure across different sizes and balances. A +2R on R2,000 and a +2R on
R20,000 are the same trade well executed. Divide by the intended risk instead
and every R-multiple in the file reads high by the rounding ratio, which
flatters the model rather than the trader and is invisible in the output.

**Technicals:** `setup` (`channel_bounce`, `trendline_break_retest`,
`double_bottom_neckline`), `timeframe` (`1h` or `4h`), and `exit_reason`, which
is one of the plan's five exits: `target`, `stop`, `trailing_stop`, `partial`,
`structure_break`, `time_exit`. Grouping by `exit_reason` is how you find out
whether the time-based exit saves money or cuts winners short.

**The bias snapshot, which is the part that cannot be reconstructed later:**
`base_score`, `quote_score`, `spread_score`, `conviction`, `base_pillars` and
`quote_pillars` (all seven pillar scores for each leg), and `config_digest`.

Reconstructing that after the fact is impossible. Macro series get revised, the
cross-sectional normalisation depends on the whole universe on that day, and the
weights may have changed since. A snapshot at entry is the only version that is
true. Without it there is no way to ever answer whether the model said anything
useful, and the weights stay wherever they were first guessed, forever.

**Discipline:** `agreed_with_bias` and `blackout_checked`.

### Weekly review routine

Run this once a week, at the same time, away from the market.

1. Load the week's records.
2. Run `evaluate`. Read expectancy by conviction bucket. **Expectancy should
   rise from LOW through MEDIUM to HIGH.** If it does not, the conviction model
   is wrong, and the ladder is actively harmful because it is putting more money
   on the worse trades. The response is to re-weight the pillars or flatten the
   ladder until the model earns the difference back. Flatten it by lowering
   `risk_per_trade_max` toward `risk_per_trade_min` in config, not by editing
   the ladder itself, so every rung moves together and nothing ends up clamped.
   Read `hit_rate` while you are there: it is the evidence for or against the
   reward-to-risk ladder in section 3.
3. Check sample size before believing any of it. At five trades a week, thirty
   closed trades per bucket is roughly where a difference in expectancy becomes
   worth acting on. Below that the report is a record, not evidence. Reading it
   as evidence is how a working model gets tuned into a broken one.
4. Run `discipline_flags`. Read every revenge, overtrading and against-bias flag
   without arguing with it.
5. Group by `exit_reason`. Which exit is making money and which is leaking it.
6. Group by `setup`. Which technical patterns actually work for you.
7. Count the overrides, the trades where `agreed_with_bias` is false. If they
   consistently beat the model, the model is the problem. If they consistently
   lose, the discipline is.
8. Write the conclusion into the `notes` of the trades it applies to.

---

## 7. The account size constraint, stated honestly

At R2,000 with R20 at risk and a channel stop 25 pips wide, several G10 crosses
size below any retail broker's minimum lot. Example A above is one of them.

This is not a defect to engineer around. It is arithmetic. The available
responses are:

1. **Trade fewer pairs.** Concentrate on the pairs that do size cleanly at this
   balance, which on a micro-lot account skews toward JPY-quoted pairs and the
   tighter-spread majors.
2. **Wait for setups with tighter stops.** A stop 15 pips beyond a 1h trendline
   sizes larger than one 40 pips beyond a 4h channel. This must not become an
   excuse to place stops closer than structure justifies.
3. **Confirm whether your broker offers 0.001 nano lots.** That single fact
   changes which pairs are tradeable more than anything else in this document.
4. **Grow the account.** The constraint dissolves on its own above roughly
   R10,000.

The response that is not available is taking the minimum lot anyway. That
silently converts a 1% trade into a 2-4% trade, and at that point the plan has
stopped being a plan.

**Broker values must be confirmed.** `DEFAULT_BROKER` in `src/fbe/risk.py` holds
typical retail figures, not a quote from any specific broker. Read `min_lot`,
`lot_step` and `contract_size` off your broker's contract specification, place
one minimum-size trade to confirm, then replace the constant. Sample the spreads
from your own terminal during the hours you actually trade.

---

## 8. Pre-trade checklist

Run this before every ticket. It takes about two minutes.

**Bias, from the engine**

- [ ] Pair is on today's shortlist with a direction and a conviction.
- [ ] Conviction is not NONE.
- [ ] `tradeable` is true, and any strings in `blockers` are `:unchecked` or
      `:unknown` markers only, not a real block. Read every one of them: a
      tradeable pair can still carry `event:unknown`, and that is exactly the
      case the News section below exists to catch.
- [ ] Spread score is meaningful, not a rounding difference between two flat
      currencies.

**News**

- [ ] `is_blacked_out` returns clear, `(False, None)`, for the pair, right now.
      This box may only be ticked on that exact answer.
- [ ] If `is_blacked_out` returns unknown, `(None, reason)`, this box stays
      unticked regardless of anything else on the page. Unknown means the
      guard tried to check and could not, most often because the calendar
      fetch failed or the cached week does not reach today; it is not a
      quieter version of clear. Check the calendar by hand for **both** legs
      before doing anything else, and write in the journal that the guard
      could not check and why, using the reason it gave.
- [ ] No high-impact event on **either** leg within the next few hours that would
      catch the trade mid-flight.
- [ ] If something is scheduled, note `blackout_until` and decide now what
      happens to the position when it arrives.

**Technical, your own rules**

- [ ] Trendline or channel drawn from at least two significant swings.
- [ ] Price is **at** the line, not chasing it from halfway across the channel.
- [ ] Confirmation is present: engulfing bar, hammer, doji, or a retest of a
      broken line.
- [ ] The 4h agrees with the 1h. If they disagree, there is no trade.
- [ ] Your technical direction matches the engine's bias. If it does not, you can
      still take it, but mark `agreed_with_bias` false so it is counted
      separately.

**Levels**

- [ ] Stop is just beyond the opposite side of the channel or the key line.
      Structure decides the stop. The account does not.
- [ ] Target is at a real support or resistance level, not a round pip count.
- [ ] Reward to risk clears the minimum for this conviction: 1.5R HIGH, 2.0R
      MEDIUM, 2.5R LOW.

**Size**

- [ ] `position_size` run with the **current** USDZAR rate, not this morning's,
      and with whatever second leg the pair needs (`USDJPY` for a yen cross).
- [ ] `warnings` is empty. If the size is below the broker minimum, the trade
      does not happen. Do not round up.
- [ ] **`realised_risk_amount`**, not `risk_amount`, is between R20 and R40.
      That is the money actually on the book after rounding down.
- [ ] `notional` reads as a rand figure and the leverage it implies is one you
      are willing to carry.

**Limits**

- [ ] Every limit on the ticket reads **clear**. A limit reading **not
      performed** is not a pass: check that one by hand against the terminal
      before ticking its box below. Today that is the last two every time.
- [ ] Fewer than 3 positions open. The ticket prints the count, the journal path
      and when the journal was last written. If a position is open that you have
      not journalled yet, the count is wrong and you are the one who knows it.
- [ ] No currency leg exceeds 4% total exposure once this trade is added.
- [ ] Not down 4% or more on the day.
- [ ] Not in a 10% drawdown from peak.

**Record**

- [ ] Journal entry written at entry, with the bias snapshot, before you walk
      away from the screen.
- [ ] Time-based exit decided now: how long does this trade get before a stall
      closes it.

If any box is unchecked, there is no trade. The best trades are often the ones
you did not take.
