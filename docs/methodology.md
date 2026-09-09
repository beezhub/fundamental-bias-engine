# Methodology

## What this document is

This is the reasoning layer of the engine. It explains why the model is built the
way it is. The exact arithmetic lives in `docs/scoring-spec.md`; nothing here
should be read as an implementation instruction.

One statement of provenance, made once and not repeated. The framework described
here is a reconstruction, assembled from publicly available descriptions of the
Anton Kreil and Institute of Trading and Portfolio Management approach (public
talks and interviews, published course outlines, and secondary write-ups) and
from general macro-FX practice that is common property across the industry. It
is not a reproduction of paid course material, and no claim is made that any
specific weight, threshold or transformation in this repository matches anything
taught privately. What is borrowed is the shape of the process: fundamentals
decide direction, technicals decide timing, every currency is judged against
other currencies rather than in isolation, and positions are held as a book
rather than collected as tickets. Where this document states a number, that
number is ours and is defended on its own merits in the scoring spec.

## The problem

The owner's trading plan (`docs/reference/trading-plan-source.txt`) is a
complete technical execution system. It specifies how to draw a trendline, how
many touches make it credible, which candlestick patterns confirm a bounce, where
the stop goes, how position size follows from stop distance, and how to exit. It
is disciplined about risk: 1-2% per trade, stop always beyond the structure,
never trade into high-impact news.

It has one hole, and the plan names it itself without solving it. Point 15 of
"Keep in mind" says: do not rely solely on technical analysis, consider
incorporating fundamental analysis and market sentiment. Point 7 of the
high-probability guidelines says: trade with the trend. Neither tells you how to
decide which way the trend should be, or which of the twenty-eight G10 crosses
is worth putting on a chart this week.

That is the gap. A trendline tells you where price has been respecting a level.
It does not tell you whether the currency on the left of the pair is being bought
by people who have to buy it. A descending trendline break on GBPJPY looks
identical whether the pound is repricing a hiking cycle or drifting on nothing.
The technical picture is the same; the odds are not.

The consequence of leaving the gap open is not random performance, it is
selective performance. A trader with no directional prior takes every clean setup
that appears, which means taking the counter-trend ones with the same size and
the same enthusiasm as the with-trend ones. The plan already knows this is a
problem, which is why "be selective" appears three times. Selectivity needs a
criterion. This engine supplies one.

## The top-down chain

The chain runs from the largest thing that can move an FX rate to the smallest.
Each stage narrows the field, and each stage answers a question the stage below
it cannot answer for itself.

| Stage | Question it answers | Rules in | Rules out | Output |
| --- | --- | --- | --- | --- |
| 1. Global risk regime | Is capital seeking return or seeking safety? | The direction in which high-beta and haven currencies should be leaning | Positions that fight the regime, for example long AUD in a genuine risk-off | A single regime reading applied to all eight currencies through their `risk_beta` |
| 2. Country macro | What is happening to each economy on its own terms? | Rate path, inflation against target, growth, labour market, external balance | Currencies whose story is one stale data point | Seven pillar scores per currency |
| 3. Currency relative strength | Which currencies are strong compared with the other seven? | The top and bottom of the ranking | The middle of the ranking, where the model has no view | One composite score per currency |
| 4. Pair selection | Which cross expresses the view most cleanly? | Crosses with a wide score spread, tight dealing spread, no imminent event | The other twenty-odd crosses, and any pair whose legs disagree with themselves | A shortlist with direction and conviction |
| 5. Technical entry | Where and when do I get in? | A specific price, a specific stop, a specific size | Setups pointing against the bias, and good setups that arrive at a bad price | A trade, or no trade |

The order matters and it is not reversible. Stage 5 cannot correct a mistake made
at stage 1. If the regime call is wrong, the cleanest channel bounce in the world
is a bounce inside a market that is repricing in the other direction. Conversely
stages 1 to 4 cannot produce a trade on their own, because they contain no price.

The narrowing is the point. Twenty-eight crosses is more than any person can
watch on two timeframes with any care. The plan's own answer to this is to trade
majors only, which cuts the field to seven but does so by a rule that is blind to
opportunity. The chain cuts it by a rule that is not: pairs reach the chart when
the two legs actually disagree about something.

## Why relative value

There is no such thing as a strong currency. A currency has no price of its own.
Every quoted FX rate is the exchange ratio between two claims, and a rate can
only move because the market's view of one claim changed relative to its view of
the other. "The dollar is strong" is shorthand for "the dollar is strong against
the basket I happen to be looking at", and the basket is doing most of the work
in that sentence.

This has a consequence that is easy to say and hard to act on: every FX trade is
a spread trade, whether or not the trader thinks of it that way. Buying EURUSD is
being long the euro and short the dollar in equal notional. It is not a bet on
Europe. It is a bet on Europe against the United States, and it will lose money
in a strong European economy if the American one is stronger.

The engine is built around that fact rather than working against it. It scores
currencies, not pairs, and it produces a pair view only by subtraction:

    spread(base, quote) = composite(base) - composite(quote)

Scoring pairs directly would be both more work and less informative. There are 28
crosses and only 8 currencies, so a pair-level model estimates 28 things from the
same underlying 8 stories, and the 28 estimates are then free to contradict each
other. A currency-level model cannot contradict itself: if USD outranks JPY and
JPY outranks NZD, then USD outranks NZD, automatically, with no reconciliation
step. Transitivity comes free.

Differencing also cancels what is common. When every economy slows at once, every
growth pillar falls at once, the cross-sectional normalisation absorbs it, and
the spread barely moves. This is correct. A global slowdown is not a reason to be
long any particular currency. It is only when one economy slows faster than
another that an FX rate has a reason to move, and the difference is exactly what
survives the subtraction.

The same logic explains why the spread, and not the level, drives conviction. A
currency sitting at +2.0 against a universe that averages zero is interesting.
Two currencies both sitting at +2.0 are, as a pair, nothing at all.

## Fundamentals set direction, technicals set timing

The division of labour is strict, and it is strict for a reason.

Fundamentals answer "which way". They operate on the horizon over which capital
actually reallocates, which for a rate-differential story is weeks to months. A
central bank that has begun a hiking cycle does not finish it on Tuesday. A
current account deficit does not close because the daily candle closed red.
Fundamental information is slow, persistent, and therefore useless for choosing a
minute.

Technicals answer "where and when". A trendline is a record of where participants
have recently been willing to transact. That is genuine information about the
price at which a position can be opened with a small, definable loss if the read
is wrong. It is not information about the next three weeks, and treating it as
such is how a stop-loss becomes a prediction.

Keeping them separate is a defence against overfitting, and this is the part
worth being explicit about. The moment the two layers are allowed to talk to each
other, the natural next step is to make the fundamental rules conditional on the
technical state: "weight monetary policy higher when the 4h chart is trending",
"ignore positioning when price is inside the channel". Each of those conditions
is a free parameter. Each one is fitted on a sample that, at one trade a week, is
tiny. A model with seven pillars and a handful of technical conditions has more
degrees of freedom than the owner will generate trades in a year, and it will
describe the past beautifully and forecast nothing.

The separation makes each layer falsifiable on its own. If the bias is wrong the
composite scores were wrong, and the pillar table shows which one. If the entries
are bad the bias may have been right and the timing poor, which shows up as
correct direction and stops taken out before the move. Those are two different
problems with two different fixes, and a blended model cannot tell them apart.

There is a second, blunter argument for the ordering. Public statements from the
approach this engine draws on put fundamentals at roughly four fifths of a
professional process and technicals at the remaining fifth, used for timing and
never to decide whether to buy or sell. Whatever the exact ratio, the direction
of the claim is the useful part: the analysis that decides the side is macro, and
the chart decides the moment. This repository implements only the first part. The
owner already has the second.

## Running a book, not taking tickets

A retail account tends to be operated one ticket at a time. Each trade is
conceived, sized, entered and judged in isolation, and the account's state is
whatever happens to be left after all the individual outcomes are added up. A
professional book is operated the other way round: the desired state of the book
is decided first, and individual tickets are the means of getting there.

The difference is not stylistic. It changes what a position even is.

Suppose the model likes the dollar. Two trades are available: short EURUSD and
short GBPUSD, each risking 1% of the account. Ticket-thinking sees two
independent trades, 2% at risk, adequately diversified across two pairs.
Book-thinking sees one position: long USD against a blend of European currencies,
2% at risk on a single dollar view, with a modest hedge between EUR and GBP that
was never intentional. If the dollar view is wrong both lose together. The
account has taken a 2% dollar bet and called it diversification.

This is why `RiskConfig` carries `max_correlated_exposure` alongside
`risk_per_trade_max`: the per-trade limit governs the ticket, the correlated
limit governs the book. Net currency exposure, not trade count, is the quantity
that must be kept inside a limit.

Thinking in net exposure also changes which pairs are worth trading. If the book
is already long dollars through a short EURUSD, then adding short GBPUSD adds
almost no new information and considerable concentration, while adding a EURGBP
position expresses something the book does not yet own. The cross pairs, which
retail trading tends to ignore in favour of the majors, are precisely the
instruments that let a view be expressed without doubling an existing exposure.
The engine scores all 28 crosses for this reason, even though the plan's low-cost
rule will usually pull execution back toward the majors.

The alpha and beta distinction is the same idea stated in return terms. Beta in
an FX context is exposure to a common factor: the dollar cycle, the global risk
regime, the carry trade. A book of long AUD, long NZD, long CAD against USD is
almost entirely beta. It has one bet in it, and it will be right or wrong as a
unit, driven by something the trader has no edge in forecasting. Alpha is what
remains after the common factor is stripped out: the part of the position that
depends on this currency being mispriced relative to that one. Long AUD against
NZD is a small, unglamorous position with almost no factor exposure, and it is
where a relative-value model's actual edge lives, if it has one.

For a R2000 account, none of this is about portfolio optimisation. It is about
not accidentally holding three copies of the same trade.

## Research cadence

The engine's work is split by how often the underlying information can actually
change. Refreshing a quarterly series every morning produces no new knowledge and
a good deal of false activity.

**Daily.** Prices, volatility, the risk-regime reading, the economic calendar for
the next 48 hours, and the composite recomputed on whatever arrived. In practice
only the RISK pillar and the market-derived part of MONETARY move day to day. The
daily run is a check, not a re-decision: it answers "has anything broken the view
I already have", and most days the answer is no. This maps onto the plan's
existing morning routine, which already calls for reviewing the calendar and
checking open positions before the session. The bias report is read at the same
point, before charts are opened, so the shortlist is known before the eye has a
chance to find a pattern it likes.

**Weekly.** Positioning, since the CFTC Commitments of Traders report is
published Friday afternoon for the preceding Tuesday. External balances and any
monthly macro that landed during the week. This is the run where the currency
ranking is genuinely allowed to change, and it is the natural point to review
whether the shortlist from last week still stands. It fits the plan's weekend
journal review.

**On a central bank meeting.** The MONETARY pillar is the largest weight in the
model and a policy decision is the only scheduled event that can move it far.
Around a meeting for either leg of a live pair, three things happen: the pair is
blacked out for execution across the announcement, the pillar is recomputed
afterwards on the new rate path, and the bias is re-derived rather than carried
forward. A view formed under the old guidance is not evidence about the new one.

**Never.** Weights and thresholds do not change on a schedule and do not change
in response to a losing trade. See the re-weighting section of the scoring spec.

The asymmetry is deliberate. Most of the plan's discipline problems come from
having time and wanting to use it. A cadence that says "there is nothing to do
today" out loud, in writing, is doing useful work.

## The handoff

The engine hands over a `BiasReport`. For each pair on the shortlist it contains
a direction, a conviction, the score spread, both legs' pillar tables, the
agreement figure, coverage and staleness, and any blockers that made the pair
untradeable. That is the whole of it.

What the engine decides:

- Which of the 28 crosses are worth attention this week, and in which direction.
- How strongly it backs each one, which the risk layer turns into a size band.
- Which pairs are ruled out, and for which specific reason.

What the trader still decides, entirely:

- The entry price. The engine has never seen a chart and produces no level.
- The stop, which comes from the channel or trendline exactly as the plan
  specifies, not from the engine.
- Whether a setup exists at all. A high-conviction bias with no clean structure
  is not a trade, it is a pair to keep watching.
- Whether to take the trade. The report is an input, not an instruction.

The engine never produces an entry price, a target, or a time. It cannot: it
holds no intraday price data, and every horizon it reasons over is longer than
the horizon the owner trades. If a future version of this repository starts
emitting levels, that is a bug in the design and not a feature.

The clean test of whether the handoff is working: the trader should be able to
disagree with the engine and say why, in one sentence, using the pillar table.
"I am not shorting NZDUSD because the whole spread is coming from growth and
growth is one stale quarterly print" is a legitimate override. "It feels wrong"
is not, and the report exists partly to make that distinction possible.

## What this model deliberately does not do

**It does not generate signals.** There is no entry trigger anywhere in this
repository. A conviction of HIGH is a statement about relative fundamentals, not
an instruction to transact.

**It makes no claim to a backtested edge.** Nothing here has been validated on
out-of-sample returns, and the weights were chosen by reasoning about what drives
G10 FX rather than by fitting. That is a deliberate choice, not an oversight: with
one trade a week and eight currencies, any weight set fitted on available history
would be fitted on noise. The honest position is that the weights are priors, they
are documented, and they should be changed rarely and for stated reasons.

**It does not execute.** No broker connection, no order, no automation.

**It does not predict individual data prints.** The engine consumes releases
after they happen. It has no forecast of Friday's payrolls and no view on whether
a number will beat consensus. Where "surprise versus consensus" appears in the
scoring spec it means a surprise that has already occurred and is still being
digested, not one being anticipated.

**It does not know about politics, positioning squeezes, or intervention.** A
Swiss National Bank intervention, an election, a fiscal announcement, or a
disorderly unwind will move a rate through a channel the model cannot see. The
model will be confidently wrong on those days. Coverage and dispersion flag
thin or contradictory information; they do not flag information the model was
never shown.

### The horizon mismatch, stated plainly

This is the model's most important limitation and it is structural, not fixable.

Fundamental bias operates over multi-day to multi-week horizons. Rate
differentials reprice over weeks. Current account positions shift over quarters.
Positioning extremes unwind over a month or two. The owner trades 1h and 4h
charts and holds for hours to a few days.

These are not the same timescale, and pretending otherwise would be the single
easiest way to lose money with this tool. A currency can be fundamentally strong
for three months and fall for nine consecutive days inside that period. On the 1h
chart, those nine days are the entire visible history.

The reconciliation is to use the bias as a filter and a size modifier, never as a
trigger:

1. **As a filter.** Setups that agree with the bias are taken. Setups that
   contradict a MEDIUM or HIGH bias are skipped, not reversed. Skipping is cheap;
   the plan already says the best trades are often the ones not taken.
2. **As a size modifier.** Within the plan's fixed 1-2% band, a HIGH conviction
   with-trend setup earns the top of the band and a LOW conviction one earns the
   bottom. The band itself is never widened. The bias changes how much of an
   already-bounded risk is used, not the bound.
3. **As a shortlist.** The bias decides which four or five charts get drawn on at
   all. This is where most of the value is, and it costs nothing.
4. **Never as an entry.** No position is opened because the score spread is wide.
   A setup is still required, at a price, with a stop beyond the structure.

Held that way, the horizon mismatch stops being a contradiction. The fundamental
layer is a standing prior that changes slowly. The technical layer samples that
prior at moments when the price offers a good place to be wrong cheaply. Nothing
requires the two clocks to tick at the same rate.

One practical corollary: a bias that flips direction week to week is not a bias,
it is noise, and the correct response is to widen the neutral band rather than to
follow it. The `min_spread_low` threshold of 0.75 exists to make most pairs, most
weeks, say nothing.

## Glossary

**Agreement.** The share of pillar weight whose own base-minus-quote difference
points the same way as the pair's overall spread. High agreement means the legs
differ for many reasons; low agreement means one pillar is carrying the trade.

**Base and quote.** In a pair `ABCXYZ`, ABC is the base and XYZ the quote. A LONG
direction always refers to the base. Quoting convention is fixed in
`fbe.universe` and is never inverted in a report.

**Beta (FX).** Exposure to a common factor such as the dollar cycle or the global
risk regime. Several positions sharing a factor are one bet.

**Bias.** A directional lean on a pair, held over days to weeks, expressed as a
Direction plus a Conviction. Not a signal and not a trade.

**Blackout.** A period around a scheduled high-impact release during which a pair
is marked untradeable. Windows are set in `DataConfig`.

**Composite.** A currency's single weighted score across the seven pillars, on
the -3 to +3 band. Positive means fundamentally strong relative to the other
seven currencies in the same run.

**Conviction.** NONE, LOW, MEDIUM or HIGH. Derived from spread size, agreement,
coverage and dispersion. Gates the shortlist and the size band.

**Coverage.** The fraction of total pillar weight that had usable, fresh data in
this run. Below 1.0 the composite rests on a partial picture.

**Cross-sectional normalisation.** Scoring a currency against the other seven in
the same run, rather than against its own history. The reason the model has no
opinion when every economy moves together.

**Dispersion.** The spread of pillar scores within one currency. High dispersion
means the pillars disagree and the composite is an average of arguments rather
than a consensus.

**G10.** Here, the eight currencies in `fbe.universe.G10`: USD, EUR, GBP, JPY,
CHF, CAD, AUD, NZD. The FX-market convention minus SEK and NOK, which carry wider
retail spreads.

**Observation.** One published data point, tagged with the period it describes
and, where available, the moment it was released. Release time is tracked so that
historical work cannot use a number before it existed.

**Pillar.** One of the seven fundamental dimensions scored for every currency:
MONETARY, INFLATION, GROWTH, EMPLOYMENT, EXTERNAL, POSITIONING, RISK.

**Relative value.** The principle that a currency can only be judged against
another currency, and that every FX position is therefore a spread.

**Risk beta.** A currency's rough behaviour in a risk-off shock, from -1 (haven,
rallies when equities fall) to +1 (high beta, falls with them). Held in
`CurrencyMeta.risk_beta`.

**Spread (score).** `composite(base) - composite(quote)`. The engine's raw
directional quantity. Distinct from dealing spread.

**Spread (dealing).** The broker's bid-ask cost. Enters the model only as a hard
filter, compared against the expected move.

**Staleness.** The age in days of the newest input behind a pillar. Old inputs
lose weight and eventually stop counting, per `max_staleness_days`.

**Surprise.** The gap between a published number and the consensus that preceded
it. Backward looking only.

**Terms of trade.** The price of what a country exports against what it imports.
Reaches the model through `CurrencyMeta.commodity_link` for CAD, AUD and NZD.
