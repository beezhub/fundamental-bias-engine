# Scoring specification

This document is the implementation contract. Everything needed to build the
scorer is here: inputs, transformations, signs, weights, thresholds and a worked
example whose arithmetic can be checked by hand. The reasoning behind these
choices is in `docs/methodology.md`; this file states what the code must do.

Types referenced below are defined in `src/fbe/types.py`. Weights and thresholds
are defined in `ScoringConfig` in `src/fbe/config.py`. Currency metadata is in
`src/fbe/universe.py`. Where this document quotes a number that also exists in
code, the code is authoritative and this document is the explanation.

## 1. Conventions

### 1.1 Sign convention

Stated once, applied everywhere without exception:

> **Positive means currency-strengthening.**

This holds at every level. A positive `Observation` transformation, a positive
sub-indicator z-score, a positive `PillarScore.score`, a positive
`CurrencyScore.composite`, and a positive `PairBias.spread` all mean the same
thing: the thing being described argues for the currency (or, for a spread, for
the base currency) appreciating.

Any indicator whose natural direction runs the other way is sign-flipped at the
point of transformation and never again. The unemployment rate is the standard
case: a rising unemployment rate is currency-negative, so the transformation
emits the negative of the change. There must be exactly one sign flip per such
indicator, applied inside its pillar, documented in that pillar's table.

### 1.2 The score band

All scores live on `-3.0 .. +3.0`, set by `ScoringConfig.score_clip`. The band is
symmetric, unitless, and comparable across pillars. It is a ranking band, not a
probability and not a forecast of basis points.

### 1.3 Notation

For a set of values `x` over the currencies present in a run:

- `mean(x)` and `sd(x)` are the cross-sectional mean and standard deviation.
- `sd` uses the **population** denominator (divide by `n`, not `n-1`). With
  `n = 8` fixed by the universe, the sample correction adds nothing but a
  scaling factor and makes hand-checking harder.
- `z(x_i) = (x_i - mean(x)) / sd(x)`.
- If `sd(x) < 1e-9`, every `z` is set to `0.0`. All eight currencies agreeing
  exactly is not information.
- `clip(v, k) = max(-k, min(k, v))`.

## 2. The pipeline

Seven stages. Each is a pure function of the previous stage plus config.

| # | Stage | Input | Output | Where it lives |
| --- | --- | --- | --- | --- |
| 1 | Collection | Source APIs and cached files | `Observation` records | `fbe.datasources` |
| 2 | Transformation | `Observation` records | One raw number per sub-indicator per currency | Each pillar |
| 3 | Cross-sectional normalisation | Raw sub-indicator values across 8 currencies | Sub-indicator z-scores | Each pillar |
| 4 | Blend, re-standardise, clip | Sub-indicator z-scores plus sub-weights | `PillarScore` with `raw`, `z`, `score` | Each pillar |
| 5 | Weighting and aggregation | 7 `PillarScore` per currency plus weights, staleness | `CurrencyScore` with `composite`, `coverage`, `dispersion` | Scorer |
| 6 | Pair spread | 8 `CurrencyScore` | `spread` per cross | Bias layer |
| 7 | Direction, conviction, filters | `spread`, agreement, coverage, calendar, cost | `PairBias` | Bias layer |

### 2.1 Stage 2: transformation

Each pillar defines its sub-indicators and how a published value becomes a raw
number. Four transformation shapes are used across the model:

- **Level.** The published value, used directly. Appropriate when the level is
  itself comparable across countries, such as a policy rate.
- **Momentum.** The change over a stated window, such as the 3-month change in
  the 2-year yield. Captures the direction of travel, which for FX often matters
  more than where a series currently sits.
- **Surprise.** Published value minus the consensus that preceded it. Backward
  looking: it measures a shock that has already landed and is still being priced,
  never an expectation of a future print.
- **Deviation from target.** Published value minus a country-specific reference,
  such as the central bank's inflation target from
  `CurrencyMeta.inflation_target`. Used where the same number means different
  things in different countries.

Where a series is unavailable for a currency, the sub-indicator is **absent**,
not zero. Absence propagates to coverage (section 5). Zero is a real value on
these scales and must never stand in for missing data.

### 2.2 Stage 3: cross-sectional normalisation

Every sub-indicator is z-scored across the currencies in the run, not against its
own history. This is the core design choice of the model and it follows directly
from relative value: a 4.5% policy rate is high or low only compared with the
other seven policy rates on the same day.

Two pillars are exceptions, and they are exceptions for stated reasons:

- **POSITIONING** normalises against its own history, because positioning is
  meaningful relative to how crowded that currency usually is, not relative to
  how crowded other currencies are. It then applies a non-monotonic shape
  function that already emits on the score band, so no cross-sectional step
  follows.
- **RISK** is constructed directly from a single global regime reading and each
  currency's `risk_beta`. It is already signed and already comparable across
  currencies, and z-scoring it would only rescale a construction that has
  meaning in its own units.

Both exceptions produce values on the same `-3 .. +3` band as the other five
pillars and are clipped identically.

### 2.3 Stage 4: blend, re-standardise, clip

For a pillar with sub-indicators `j` carrying sub-weights `u_j` that sum to 1.0:

    blend(c)    = sum over available j of ( u_j' * z_j(c) )
    z_pillar(c) = ( blend(c) - mean(blend) ) / divisor
    score(c)    = clip(z_pillar(c), score_clip)

where `u_j'` is `u_j * phi_j` renormalised over the sub-indicators actually
available for currency `c`, `phi_j` is that component's freshness factor from
section 4.1, `mean` is taken across the currencies that have this pillar at all,
and `divisor` comes from `BasePillar.blend_divisor` in
`src/fbe/pillars/base.py`. The mean is run-local and the divisor is not. That
asymmetry is deliberate and is the subject of the second half of this section.

**Why the blend is re-standardised.** Averaging several imperfectly correlated
z-scores shrinks the variance of the result. Two sub-indicators that agree
perfectly blend to something with a standard deviation of 1.0; two that are
unrelated blend to something nearer 0.7. So a pillar built from five components
arrives systematically quieter than a pillar built from one, purely as an
artefact of how many series it happens to draw on.

Left uncorrected this silently reweights the model. MONETARY at a stated weight
of 0.30 would contribute less than that, and POSITIONING at 0.10, being a single
component, would contribute more, whatever `ScoringConfig` says. The weights in
section 3 are meant to be the whole of the model's opinion about relative
importance, and they can only be that if every pillar reaches the aggregator on
the same scale. The re-standardisation pass corrects for that, so the declared
weights are the operative ones. It targets a cross-sectional standard deviation
of 1.0 for every pillar, and it hits exactly 1.0 only on the fallback path
described below. On the rolling path a pillar lands near 1.0 and is left free to
be louder or quieter on a day when its own components agree or disagree more
than usual, which is the point of taking the divisor from history.

**What the guarantee covers, and what it does not.** It is a guarantee about
pillars. Every pillar reaches the aggregator on the same scale, so a pillar's
declared weight is the share of the composite that pillar actually carries. It is
not a guarantee about any underlying series. A series that appears in two pillars
carries a loading from each, and where those loadings have opposite signs they
partly cancel, so the model's total response to that series is neither pillar's
declared weight. That happens once in the current model: `real_policy_rate` is
`policy_rate - cpi_yoy`, which places a coefficient of minus one on headline CPI
inside MONETARY, while INFLATION loads on the same series positively. Sections
3.1 and 3.2 publish both loadings and the resulting cancellation, and
`scoring.series_loading` computes them. ADR 0003 records why the term was kept
rather than removed.

Section 7.1 shows the size of the effect on real numbers: the blended MONETARY
column has a standard deviation of 0.7001 and is scaled up by a factor of 1.43,
while the two-component INFLATION column has a standard deviation of 0.9711 and
is barely touched at 1.03. Without the pass, MONETARY would have spoken roughly
39% more quietly than INFLATION relative to their declared weights. Section 7 is
a single run with no stored history, so both of those figures are the fallback
path below, not the rolling one.

**Which standard deviation the blend is divided by.** Not this run's.
`blend_divisor` takes the median of the cross-sectional blend standard deviations
this pillar produced over the last `ScoringConfig.restandardisation_window_runs`
runs (60), and it engages only once `ScoringConfig.min_restandardisation_runs`
(20) of that history exist. Below that count it falls back to the run's own
`sd(blend)` and records that it did.

The reason is that the blend's dispersion is not measuring what it looks like it
is measuring. Every component is already forced to unit standard deviation
cross-sectionally one stage earlier, at stage 3, so the blend's spread does not
track how similar or how different the eight economies are on the day. That was
normalised away. What it tracks is the correlation between a pillar's own
sub-indicators. When the five MONETARY sub-indicators tell the same story the
blend's standard deviation sits near 1.0 and the pass barely touches it; when
they contradict each other it falls, and a run-local divisor scales the pillar
up hard.

That is exactly backwards. Internal disagreement between a pillar's own
components is evidence that the pillar is on shaky ground that day, and a
run-local divisor converts it into amplification: the pillar would speak loudest
on the days its own inputs are least coherent, it would do so in proportion to
how incoherent they were, and nothing downstream would reveal it. Taking the
divisor from recent runs breaks that feedback loop. The pillar is corrected for
how many parts it is built from, which is the artefact this pass exists to
remove, and not for how much those parts happen to be arguing today, which is
information the model should keep.

The median rather than the mean, so that one strange run cannot move the scale
the whole model is measured on. A divisor that does not move day to day also
means the clip in section 1.2 interacts with a fixed scaling, so a currency
cannot be pushed across the band edge by something that happened to an unrelated
pillar's internal coherence.

**The path taken is recorded.** `blend_divisor` returns the divisor and a path
label, `"rolling"` or `"run_local"`, and the label belongs in
`PillarScore.notes` on every run. This is not bookkeeping. Scores computed under
the fallback and scores computed under the rolling estimate are not on the same
scale, so a run-to-run comparison that hides the switch shows the reader a change
the market did not make. The run's own `sd(blend)` is returned to the caller for
storage as well, because it is the next run's history.

**Why the theoretical divisor was rejected.** The obvious alternative is to
divide by the standard deviation the blend would have if the sub-indicators were
independent, `sqrt(sum of u_j squared)`. It is a fixed number per pillar, it
needs no history, and it is wrong for the same reason it is convenient: it
assumes components that are visibly correlated are independent. For MONETARY it
gives `sqrt(0.15^2 + 0.25^2 + 0.20^2 + 0.25^2 + 0.15^2) = 0.4583`, against the
0.7001 actually observed in section 7.1. It would scale the heaviest pillar in
the model by 2.18x where the observed figure calls for 1.43x, erring toward
over-amplification in the one place where over-amplification costs most.

Two implementation notes. When every currency has full sub-indicator coverage the
blend's mean is exactly 0 by construction, since each `z_j` has mean 0 and the
sub-weights sum to 1, so the pass reduces to a division by the divisor. The mean
is only non-zero when per-currency sub-weight renormalisation differs across
currencies, so subtract it anyway rather than relying on the special case. And a
divisor below `1e-9`, from either path, is handled exactly as in section 1.3:
every score becomes 0.

The pass applies only to the five cross-sectionally normalised pillars.
POSITIONING and RISK are excluded for the reasons in section 2.2: both are
already constructed on the score band in units that mean something, and
re-standardising them would destroy that meaning. In particular it would force
POSITIONING to have a non-zero spread across currencies even in a run where
nothing is crowded, which is the opposite of what that pillar is for.

**Minimum available sub-weight.** A currency must hold more than half of a
pillar's total sub-weight for that pillar to be scored. The sum below is over the
components the currency has data for, before the freshness factors are applied,
because the floor asks about substitution and staleness does not substitute
anything. Section 4.1 carries that argument:

    MIN_COMPONENT_WEIGHT = 0.5
    if sum of u_j over available j  <=  MIN_COMPONENT_WEIGHT:
        the pillar is absent for that currency

The comparison is "at or below", so exactly half is absent rather than scored.
The boundary is reachable and is not a corner case: EMPLOYMENT's two components
carry 0.50 each, so a currency missing either one lands exactly on it, and under
a strict "less than" the floor could never fire for that pillar at all. GROWTH
reaches it too, on any one 0.30 component plus one 0.20 component. The rule
applies uniformly, with no per-pillar exemption, because the reason for the
floor is arithmetic rather than a judgement about any one pillar's economics.

Renormalising is a reasonable repair for one missing series out of four. It is
not a reasonable repair for three missing out of four, where it stops being a
repair and becomes an assertion that the one surviving series speaks for the
whole pillar. At or below the floor the honest output is absence, which flows
into coverage (section 4.2) and is visible in the report, rather than a
confident number resting on a fragment. If a currency has no sub-indicator at
all for a pillar, the pillar is likewise absent and contributes nothing to
coverage.

`PillarScore.raw` carries the pillar's headline number in natural units, for the
report. `PillarScore.z` carries `z_pillar` after re-standardisation and before
clipping. `PillarScore.score` is what the aggregator consumes.

## 3. Pillar specifications

Default weights, from `ScoringConfig.weights`:

| Pillar | Weight | One-line rationale |
| --- | --- | --- |
| MONETARY | 0.30 | Rate expectations dominate G10 FX at this horizon |
| INFLATION | 0.15 | The main forward input to the rate path |
| GROWTH | 0.15 | Sets the medium-term direction of the rate path |
| EMPLOYMENT | 0.10 | Slower and largely a subset of growth, but watched closely by policy |
| EXTERNAL | 0.10 | Real flow, but slow and often already in the price |
| POSITIONING | 0.10 | Says who is already on the trade, not whether it is right |
| RISK | 0.10 | Regime overlay, decisive in a shock and near irrelevant otherwise |

The weights sum to 1.0 and `Config.validate()` enforces it.

### 3.1 MONETARY (weight 0.30)

**What it measures.** The current and expected policy stance of the central bank,
in nominal and real terms, and the direction in which the market is repricing it.

**Why it moves FX.** Over a multi-day to multi-week horizon the dominant driver
of a G10 exchange rate is the relative path of short-term interest rates. Capital
moves toward the currency where the expected return on short-dated paper is
rising fastest. The 2-year yield is used rather than the policy rate alone
because it prices the expected path rather than the current setting, and the path
is what the market trades. The change in the 2-year yield carries a quarter of
the pillar on its own, because in FX the direction of repricing typically leads
the level.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| Policy rate | `policy_rate` | Level, percent | Higher is positive | 0.15 |
| 2-year government yield | `yield_2y` | Level, percent | Higher is positive | 0.25 |
| 2-year yield change, 1 month | `yield_2y_chg_1m` | Momentum, basis points | Rising is positive | 0.20 |
| 2-year yield change, 3 months | `yield_2y_chg_3m` | Momentum, basis points | Rising is positive | 0.25 |
| Real policy rate | `real_policy_rate` | `policy_rate - cpi_yoy`, percent | Higher is positive | 0.15 |

**Effective loading on headline CPI.** `real_policy_rate` is
`policy_rate - cpi_yoy`, so this pillar carries a coefficient of minus one on a
series it does not name in the table above. On the section 7 fixture's own
dispersions, the loading is **-0.0501** composite points per percentage point of
headline CPI. It is the opposite sign to INFLATION's loading on the same series,
and section 3.2 carries the full picture and the cancellation it produces.
`scoring.series_loading` computes the figure and
`tests/test_effective_loadings.py` pins it.

**Sources.** FRED for US series and for those non-US yields it carries; a manual
CSV under `data/manual` for the remainder, refreshed weekly. The registry of
indicator keys to source series is owned by `docs/data-sources.md`.

**Known failure mode.** The pillar is partly an inflation pillar, with the
opposite sign to INFLATION, and its sub-weight table does not show it. Beyond
that it is blind to the difference between a yield rising because policy is
expected to tighten and a yield rising because the market is demanding a risk
premium on that country's debt. In a fiscal or credibility event the two look
identical in the data and mean opposite things for the currency. It is also
blind to the level effect at the extremes: a move from
0.10% to 0.35% is a larger regime change than a move from 4.50% to 4.75%, and the
model treats them as roughly equal.

### 3.2 INFLATION (weight 0.15)

**What it measures.** How far consumer price inflation sits from the level the
central bank has committed to, in headline and core terms.

**Why it moves FX.** Inflation matters to an exchange rate almost entirely
through the policy channel. Above-target inflation is currency-positive because
it obliges the central bank to keep policy tight or tighten further, and the
market prices that in the front end. This is why the pillar is scored as a
deviation from target and not as a raw level.

The distinction matters more than it first appears. A 3.0% CPI print is 1.0 point
above target in the United States, the euro area, the United Kingdom, Japan,
Canada and New Zealand; 0.5 points above target in Australia, whose target is the
midpoint of a 2-3% band; and 2.0 points above target in Switzerland, where the
Swiss National Bank aims below 2%. Those are three different policy problems and
therefore three different currency implications. `CurrencyMeta.inflation_target`
carries the reference and the pillar must use it rather than assuming 2%.

**The credibility caveat.** Above-target inflation is currency-positive only
while the central bank is expected to respond to it. Where a central bank is
credibly and deliberately tolerating an overshoot, the same print is
currency-negative: inflation erodes the real return on the currency while the
nominal rate does not rise to compensate. The real policy rate term inside
MONETARY partly captures this, since a tolerated overshoot shows up as a falling
real rate. It is a partial fix and it is listed as an open question in section 10.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| Headline CPI year on year | `cpi_yoy` | Deviation from `inflation_target` | Above target is positive | 0.40 |
| Core CPI year on year | `core_cpi_yoy` | Deviation from `inflation_target` | Above target is positive | 0.60 |

Core carries the larger sub-weight because it is the series central banks act on.
Headline is retained because it drives household expectations and, through them,
the political pressure on the bank.

**Effective loading, which is not 0.15.** The weight above is this pillar's share
of the composite. It is not the model's response to an inflation print, because
MONETARY's `real_policy_rate` term is `policy_rate - cpi_yoy` and therefore
carries a coefficient of minus one on the same headline series. Per percentage
point of headline CPI, on the section 7 fixture's own cross-sectional
dispersions:

| Term | Composite loading |
| --- | --- |
| INFLATION, headline sub-indicator | +0.1008 |
| INFLATION, core sub-indicator, if core moves one for one with headline | +0.1666 |
| MONETARY, real policy rate | -0.0501 |

Two cases, and which one applies depends on the shape of the print:

| Case | Same-sign loading | Net loading | Cancelled |
| --- | --- | --- | --- |
| Headline moves alone | +0.1008 | +0.0508 | 49.7% |
| Headline and core move together | +0.2674 | +0.2174 | 18.7% |

Read that as: the model's response to inflation is between roughly a fifth and a
half smaller than the 0.15 weight implies, and a headline-only shock is the case
where it is halved.

Every column above is computed from unrounded loadings and displayed to four
places, so subtracting the displayed figures differs in the last digit:
`0.1008 - 0.0501` reads 0.0507 against the 0.0508 published. The same figures
appear in ADR 0003 and in `docs/answers/framework.md` Q1 and are the same
numbers, not a second set.

This is arithmetic from the section 7 fixture, not a measurement of anything the
model predicts. Every standard deviation in it is recomputed each run, so the
figures drift; the direction and the rough magnitude do not.
`scoring.series_loading` computes them, `tests/test_effective_loadings.py`
reproduces all five figures from the fixture, and ADR 0003 records why the
opposing term was kept rather than removed, reweighted or replaced.

**Known failure mode.** The sign assumption inverts in a stagflationary
situation, where inflation is high, growth is collapsing, and the market prices
cuts anyway. The pillar will read the inflation as currency-positive at exactly
the moment the currency is being sold. GROWTH partially offsets this by moving
the other way, which is one reason the two pillars carry equal weight.

### 3.3 GROWTH (weight 0.15)

**What it measures.** The pace and direction of real economic activity.

**Why it moves FX.** Growth is the medium-term input to the rate path: a
strengthening economy pulls policy expectations up, and the currency follows. It
also drives portfolio flow directly, since capital chases the return on real
assets. The pillar mixes one lagging and comprehensive measure (GDP), one leading
survey (PMI), and two coincident hard-data series.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| GDP year on year | `gdp_yoy` | Level, percent | Higher is positive | 0.30 |
| Composite PMI | `pmi_composite` | Level, index | Higher is positive | 0.30 |
| Industrial production year on year | `indpro_yoy` | Level, percent | Higher is positive | 0.20 |
| Retail sales year on year | `retail_sales_yoy` | Level, percent | Higher is positive | 0.20 |

PMI is not available on a free feed for every G10 country. Where it is missing
the sub-weight is renormalised across the remaining three, which hold 0.70
between them, per section 2.3, and coverage is unaffected because the pillar
still has data. The engine must not substitute a proxy silently.

**GROWTH is absent for a currency holding exactly half its sub-weight.** Any one
0.30 component plus one 0.20 component sums to 0.50, which is at the floor of
section 2.3 and therefore absent rather than scored. The live instance:
`indpro_yoy` is manual-only for CHF, AUD and NZD, and the manual PMI file holds
no values, so those three currencies hold GDP plus retail sales today and GROWTH
is absent for all three. Their composite renormalises around the gap and their
coverage falls to 0.85, which is above `coverage_demotion`. Filling either
manual file for those currencies restores the pillar.

**Known failure mode.** GDP is published quarterly and heavily revised, so a
third of the pillar can be describing a world two quarters old. The staleness
discount in section 5 handles the age but not the revision risk. Separately,
strong growth is currency-positive through the policy channel and
currency-negative through the import channel, and this pillar only models the
first.

### 3.4 EMPLOYMENT (weight 0.10)

**What it measures.** The direction of labour market slack.

**Why it moves FX.** Employment is where central banks look for confirmation that
the inflation problem is real or resolved. It is the highest-attention monthly
release in the calendar for most G10 countries. The pillar scores *direction*
rather than level, because the level of the unemployment rate is not comparable
across countries with different labour market structures, while a rise of 0.4
points means something similar everywhere.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| Unemployment rate, 6-month change | `unemployment_rate` | Momentum, percentage points, **sign-flipped** | Rising unemployment is negative | 0.50 |
| Employment change momentum | `employment_chg` | 3-month average, annualised percent | Faster hiring is positive | 0.50 |

The sign flip on unemployment is the single flip in this pillar and is applied
inside the transformation.

**EMPLOYMENT is absent for a currency holding either component alone.** The two
sub-weights are 0.50 each, so losing one leaves exactly the floor of section
2.3, which is "at or below" and therefore absent. This pillar has no partial
state: a currency has both series or it has none of the pillar. That is the
consequence of weighting the two equally, and it is intended. The unemployment
rate falls both when hiring is strong and when people leave the labour force,
and the employment series is what separates the two, so a score built on the
rate alone is the failure mode below rather than a weaker reading of the same
thing.

**Known failure mode.** Employment lags the cycle. By the time the unemployment
rate has turned, the rate market has usually finished repricing, so the pillar
tends to confirm what MONETARY already said rather than adding information. It
carries a 0.10 weight for this reason. It also cannot distinguish a falling
unemployment rate caused by hiring from one caused by people leaving the labour
force, which are opposite signals for policy.

### 3.5 EXTERNAL (weight 0.10)

**What it measures.** Whether a country is a structural net receiver or net payer
of foreign currency, and whether the price of what it sells is rising.

**Why it moves FX.** External balances are the slowest and most real of the
drivers. A persistent current account surplus is a standing bid for the currency;
a deficit is a standing offer that must be financed. Terms of trade matter for
the three commodity currencies specifically, because a rise in the export price
raises national income and, through it, the currency, usually before it shows up
in any macro series.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| Current account, share of GDP | `current_account_gdp` | Level, percent of GDP | Surplus is positive | 0.40 |
| Trade balance momentum | `trade_balance` | 3-month change, percent of GDP | Improving is positive | 0.30 |
| Terms of trade | `commodity_price` | 3-month return on the linked commodity, percent | Rising export price is positive | 0.30 |

The terms of trade term reads `CurrencyMeta.commodity_link`: crude oil for CAD,
iron ore for AUD, dairy for NZD. A currency with no dominant commodity link takes
**0.0** for this term rather than being treated as missing. This is a deliberate
modelling statement, not a shortcut: a currency whose terms of trade are not a
first-order FX driver genuinely has no view on this axis, and giving it a
neutral value keeps all eight currencies inside one cross-sectional
normalisation. Treating it as absent instead would z-score three commodity
currencies against each other, which on three points is not a z-score.

**Known failure mode.** External balances move over quarters and are largely
known to the market, so the pillar contributes little new information most of the
time. Worse, the sign can invert: a country whose current account improves
because imports collapsed in a recession is not a country whose currency should
rally. The pillar reads the improvement and calls it positive.

### 3.6 POSITIONING (weight 0.10)

**What it measures.** How crowded speculative positioning already is in each
currency, relative to that currency's own recent history.

**Why it moves FX.** Positioning is not a view on value, it is a view on who is
left to buy. A modestly extended long position is evidence that the trade is
working and that flow is behind it. A historically extreme long position is
evidence that the buyers have already bought, which makes the position fragile:
the next piece of good news has no new money behind it, and the next piece of bad
news forces liquidation.

**The transformation.** From the CFTC Commitments of Traders report, take net
non-commercial positioning as a share of open interest:

    share(c) = (non_commercial_long - non_commercial_short) / open_interest

Then z-score against that currency's own history over `lookback_years` (default
5, minimum 3 to be meaningful):

    p(c) = ( share(c) - mean_hist(share(c)) ) / sd_hist(share(c))

This is the only pillar using time-series rather than cross-sectional
normalisation, for the reason given in section 2.3.

**The shape function.** This pillar is the one whose sign is not monotonic in its
input, so the function is specified explicitly rather than described:

    f(p) = p                          for |p| <= 1.0     (momentum-confirming)
    f(p) = sign(p) * (2.0 - |p|)      for 1.0 < |p| <= 2.0  (fading)
    f(p) = -sign(p) * min( 1.5 * (|p| - 2.0), 2.0 )   for |p| > 2.0  (contrarian)

    score(c) = clip(f(p(c)), score_clip)

Properties an implementer should assert in tests:

- Continuous at both joins. At `|p| = 1.0` both branches give magnitude 1.0. At
  `|p| = 2.0` both give 0.0.
- Odd: `f(-p) = -f(p)`. There is no long or short asymmetry.
- Peak magnitude of 1.0 in the momentum region, reached at `|p| = 1.0`.
- Crosses zero at `|p| = 2.0` and turns against the crowd beyond it, saturating
  at magnitude **2.0**, reached at `|p| = 2 + 2/1.5 = 3.33`. Beyond that the
  score does not grow.

**Why the contrarian branch saturates below the clip.** The cap is 2.0 rather
than the `score_clip` of 3.0, and the difference matters. The five
cross-sectionally normalised pillars have an arithmetic maximum of `sqrt(7)`,
about 2.65, on an eight-currency universe, and after blending imperfectly
correlated sub-indicators they land nearer 1.2 to 1.8 in practice. Section 7.1
has the MONETARY pillar topping out at +1.62 in a run with a wide rate spread.

An uncapped contrarian branch reaches 1.5 at `|p| = 3.0`, which is not a rare
COT reading over a five-year window, and 3.0 at `|p| = 4.0`. That would make
POSITIONING the only pillar in the model able to saturate the band, and it is the
pillar with the weakest data behind it: futures only, one exchange only, a
Tuesday snapshot published on Friday, and silent on the EUR crosses entirely. At
`|p| = 3.4` a 0.10-weight pillar would contribute more to a composite than the
0.30-weight MONETARY pillar does at a typical reading, which inverts the weights
the model declares.

The clip is meant to be what section 1.2 and `ScoringConfig.score_clip` describe:
a backstop against a data error, not a routine operating point for the least
reliable input in the model. Capping the branch at 2.0 leaves positioning able to
argue forcefully at a genuine extreme while keeping it inside the range the rest
of the model occupies.

The reading in plain terms: mild positioning in a direction supports that
direction; positioning above one standard deviation stops helping; positioning
above two standard deviations actively argues the other way, with increasing
force. A crowded long is a reason to be less long, not more.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| CFTC net non-commercial share of open interest | `cot_net_pct_oi` | Time-series z over `lookback_years`, then `f(p)` above | Non-monotonic, see function | 1.00 |

**Known failure mode.** Three problems, all real. The COT report covers futures
positioning at the Chicago Mercantile Exchange, which is a small and unrepresentative
slice of a market that trades mostly over the counter, and it says nothing at all
about EUR-crosses or about currencies without a liquid contract. It is published
Friday for the preceding Tuesday, so it is always at least three days stale and
often more. And the boundaries at `|p| = 1` and `|p| = 2` are conventions, not
discoveries; a currency sitting at `p = 1.9` and one at `p = 2.1` are treated as
opposite in sign while being nearly identical in reality. The pillar carries a
low weight because of this, and the continuity of `f` at least keeps the
magnitudes small near the joins.

### 3.7 RISK (weight 0.10)

**What it measures.** Whether global capital is currently seeking return or
seeking safety, translated into a per-currency view through `risk_beta`.

**Why it moves FX.** In a genuine risk-off episode, correlations across G10 FX
collapse into a single factor. Funding and haven currencies (JPY, CHF, and to a
lesser extent USD) are bought regardless of their macro picture, and high-beta
commodity currencies (AUD, NZD, CAD) are sold regardless of theirs. On those
days the other six pillars are approximately noise. On all other days this pillar
should be small.

**The regime reading.** One global number, shared by all currencies:

    dd_component  = clip( equity_drawdown_pct / 10.0, 1.0 )   , upper bound 0.0
    vol_component = clip( -vol_z / 2.0, 1.0 )
    R = 0.5 * dd_component + 0.5 * vol_component

where `equity_drawdown_pct` is the current drawdown of a broad equity index from
its 52-week high, expressed as a negative percent, and `vol_z` is the z-score of
a volatility index over `lookback_years`. `R` runs from -1.0 (maximum risk-off)
to 0.0 (calm). It is bounded above at zero: euphoria is not modelled as the
mirror of panic, because a currency-market risk-on melt-up is a far weaker and
slower force than a risk-off shock.

**The currency score.**

    score(c) = clip( 2.0 * R * risk_beta(c), score_clip )

The factor of 2.0 puts a full risk-off shock against the highest-beta currency at
roughly -1.8, which is comparable to the magnitude the z-scored pillars reach.

**Why this pillar re-signs itself.** Every other pillar has a fixed sign rule: a
higher yield is always positive, a rising unemployment rate is always negative.
This one does not. Multiplying the regime by `risk_beta` means the pillar's sign
for a given currency is determined by the regime, not by the indicator. When `R`
is 0, every currency scores 0 and the pillar drops out of the composite by
construction. When `R` goes negative, AUD (beta +0.9) turns negative and JPY
(beta -0.9) turns positive, from the same input. Nothing about AUD changed; the
world did.

| Sub-indicator | Key | Transformation | Sign rule | Sub-weight |
| --- | --- | --- | --- | --- |
| Equity drawdown from 52-week high | `equity_index` | Percent below the rolling high, negative | More drawdown is risk-off | 0.50 (into `R`) |
| Volatility index | `vol_index` | Time-series z over `lookback_years` | Higher volatility is risk-off | 0.50 (into `R`) |
| Currency risk beta | `CurrencyMeta.risk_beta` | Static multiplier | Re-signs with `R` | Multiplier |

**Known failure mode.** `risk_beta` is a static constant standing in for a
relationship that is neither static nor stable. The yen's haven behaviour weakens
when Japanese rates are rising; the dollar's behaviour depends on whether the
shock originated inside or outside the United States. The pillar also fires late,
because an equity drawdown is a consequence of risk-off rather than a leading
indicator of it. And a 6% drawdown that is a healthy correction and a 6%
drawdown that is the start of a crisis produce the same reading.

## 4. Aggregation to a currency composite

### 4.1 Staleness discount

The discount is applied at two levels, and the ramp is scaled to each series'
own release calendar rather than measured in absolute days.

**The ramp.** For one input, let `s` be its age in days from `Observation.period`,
`S` be that indicator's allowance, and `s0 = S * (staleness_full_days /
max_staleness_days)`, one third of `S` on the shipped defaults:

    phi = 1.0                    for s <= s0
    phi = (S - s) / (S - s0)     for s0 < s <= S
    phi = 0.0                    for s > S

`S` is `IndicatorSpec.max_staleness_days` from `datasources/registry.py`, which
is the only module that knows the release calendar, and
`ScoringConfig.max_staleness_days` (45) where the registry has no entry for the
key. `ScoringConfig` supplies the shape of the ramp, the ratio of
`staleness_full_days` to `max_staleness_days`, not its length. `scoring.freshness`
computes it and `pillars/base.staleness_allowance` resolves `S`.

**Why the ramp is not measured in absolute days.** `Observation.period` is the
first day of the span a figure describes, so a punctual monthly print is 30 to 45
days old on the day it publishes and a punctual quarterly print is about 120 days
old. Against a single 45-day ramp, thirteen of the seventeen registered
indicators had a freshness factor of 0.0 at the age their own frequency says they
publish at: every monthly and quarterly series in the model, which is INFLATION,
GROWTH, EMPLOYMENT, EXTERNAL and the CPI leg of MONETARY. Quarterly CPI for AUD
and NZD was the visible case, at 162 days against a ramp that ended at 45, but it
was not the only one.

Worked: AUD headline CPI is stamped 2026-04-01 for 2026Q2. On a run dated
2026-09-10 that is 162 days. `cpi_yoy` carries an allowance of 200 days, so
`s0 = 200 * 15 / 45 = 66.67` and

    phi = (200 - 162) / (200 - 66.67) = 38 / 133.33 = 0.285

against 0.0 under a 45-day ramp. The INFLATION pillar's effective weight for AUD
is `0.15 * 0.285 = 0.0428`.

**One rule, two modules.** `registry.coverage_report` already ages every series
against the same allowance, with `SeriesRef.stale_on` comparing `age > S`. The
ramp reaches zero at `s = S`, the last day the registry still counts a series as
usable, so the scoring side is never the more permissive of the two. Before this
they gave two answers about the same series: the registry reported AUD CPI as
covered while the scorer gave it no weight.

**Two levels.** Inside a pillar each component is aged against its own
indicator's allowance by `BasePillar.component_freshness`, and a component built
from two series takes the lower of their factors. The components enter the blend
of section 2.3 at `u_j * phi_j`, so a GROWTH blend holding a 162-day-old GDP
print at `phi = 0.600` and a 5-day-old retail sales print at `phi = 1.000` gives
GDP 0.474 of the blend where the sub-weights alone would give it 0.600. The
pillar's own factor is the sub-weighted mean of its components' factors, from
`BasePillar.pillar_freshness`, and that is what multiplies the pillar weight in
section 4.2. A single pillar-level age cannot do this: GROWTH would report the
age of whichever series updated last and carry the other at full weight.

The per-component factors are published in `PillarScore.diagnostics` under
`freshness.<component>`. Nothing in `scoring.py` reads them. `PillarScore.staleness_days`
keeps its meaning, the age of the freshest input the pillar saw, and is reported
rather than used to compute the discount.

**Why a ramp rather than a cliff.** A hard cutoff would let a composite jump on a
day when no data changed and nothing happened except the calendar turning over.
The floor in section 2.3 is judged on the sub-weight present, before the factors
are applied, for the same reason: every component of INFLATION shares one
release, so judging the floor on the discounted total would reintroduce the cliff
at `phi = 0.5`.

### 4.2 Effective weights and coverage

    w_eff(p) = weight(p) * phi(p)          , and 0.0 if the pillar has no data
    coverage = sum over p of w_eff(p)

where `phi(p)` is the pillar's own factor from section 4.1, the sub-weighted mean
over the components it has.

Since the weights sum to 1.0, `coverage` is directly the fraction of pillar weight
that had usable, fresh data. It is stored on `CurrencyScore.coverage`.

### 4.3 Composite

    composite = ( sum over p of w_eff(p) * score(p) ) / coverage

Dividing by coverage renormalises, so a currency missing one pillar is scored on
the pillars it has rather than being dragged toward zero by an implicit zero.
This is the right treatment: a missing pillar is an absence of evidence, not
evidence of neutrality. The cost of renormalising is that a low-coverage
composite is a confident number resting on thin data, and that is handled where
it belongs, at the conviction stage (section 6.3) and the hard filter (section 7),
not by quietly shrinking the score.

If `coverage` is 0.0 the currency has no composite and every pair using it is
marked untradeable with the blocker `no_coverage`.

### 4.4 Dispersion

    w_tilde(p) = w_eff(p) / coverage        (these sum to 1.0)
    dispersion = sqrt( sum over p of w_tilde(p) * (score(p) - composite)^2 )

This is the weighted standard deviation of the pillar scores about the composite.
It answers a question the composite hides: is this currency's score a consensus
or an average of arguments? A composite of +0.5 built from seven pillars all near
+0.5 is a different object from one built from +2.5 and -1.5, and only the second
should make a trader nervous. Stored on `CurrencyScore.dispersion`.

### 4.5 Rank

Currencies are ranked 1 to 8 by descending composite and the rank is stored on
`CurrencyScore.rank`. Rank is presentational. Nothing downstream consumes it,
because ranks discard the size of the gaps and the gaps are the whole signal.

## 5. Pair layer

### 5.1 Spread

For each of the 28 crosses in `universe.ALL_PAIRS`, in market quoting convention:

    spread = composite(base) - composite(quote)

Stored on `PairBias.spread`, with the two legs echoed in `base_score` and
`quote_score`.

### 5.2 Direction

| Condition | `Direction` |
| --- | --- |
| `spread >= +min_spread_low` (+0.75) | `LONG` |
| `spread <= -min_spread_low` (-0.75) | `SHORT` |
| otherwise | `NEUTRAL` |

`LONG` and `SHORT` always refer to the **base** currency, in the quoting
convention fixed by `universe.ALL_PAIRS`. A report must never invert a pair for
display convenience.

### 5.3 Agreement

Agreement measures whether the two legs differ for many reasons or for one.

    has_data(p) = z(base, p) is not None and z(quote, p) is not None
    d(p)        = score(base, p) - score(quote, p)
    w_pair(p)   = ( w_eff(base, p) + w_eff(quote, p) ) / 2
    considered  = { p : has_data(p) and |d(p)| > 1e-9 }
    agreeing    = { p in considered : sign(d(p)) == sign(spread) }

    agreement = sum of w_pair over agreeing / sum of w_pair over considered

Pillars where the two legs score identically are excluded from both numerator and
denominator: they express no opinion on this pair and should neither support nor
oppose it. If no pillar is considered, agreement is 0.0. Stored on
`PairBias.agreement`.

`has_data` is the other exclusion and it is not a refinement of the first one.
A pillar with no usable data for a currency still arrives at this calculation:
`BasePillar.compute` returns one `PillarScore` per currency including those,
carrying `missing_score`'s neutral score of 0.0 with `raw` and `z` both `None`,
and the staleness penalty then takes its effective weight to zero. Without
`has_data` its `d(p)` is `0.0 - score(other leg)`, a difference whose sign is
decided entirely by the leg that does have data, and it is counted as an opinion.
It agrees with the headline about half the time, which raises agreement, and the
agreement cap is the one demotion that can move a pair a whole tier. On a
worked case a EUR leg missing POSITIONING reads 0.6154 against `min_agreement`
of 0.60 and returns MEDIUM, where excluding it reads 0.5833 and returns LOW:
1.5% of the account at risk rather than 1.0%, on the strength of a pillar that
had nothing to say.

`z is None` is the marker rather than a zero effective weight, because those are
different facts. A pillar can be present, fresh and genuinely neutral, and a
pillar whose weight has decayed to zero through staleness still had data once.
`z` is what `missing_score` sets and what the aggregator already reads to tell
absence of evidence from evidence of neutrality.

Weighting by `w_pair` rather than counting pillars is deliberate. MONETARY at 0.30
disagreeing is a materially worse sign than POSITIONING at 0.10 disagreeing, and
a headcount would treat them as equal.

### 5.4 Conviction

A base tier from the size of the spread, then demotions. Never a promotion.

**Base tier:**

Every threshold below is a `ScoringConfig` field. The number in parentheses is
that field's default, shown so the table can be read without opening the config,
and the field name is what an implementation reads.

| `abs(spread)` | Base `Conviction` |
| --- | --- |
| `< min_spread_low` (0.75) | `NONE` |
| `min_spread_low <= x < min_spread_medium` (1.50) | `LOW` |
| `min_spread_medium <= x < min_spread_high` (2.50) | `MEDIUM` |
| `>= min_spread_high` | `HIGH` |

**Demotions**, applied in order down the ladder `HIGH -> MEDIUM -> LOW -> NONE`:

| Test | Effect | Reason |
| --- | --- | --- |
| `agreement < min_agreement` (0.60) | Cap at `LOW` | One pillar is carrying the whole spread |
| `min(coverage_base, coverage_quote) < coverage_demotion` (0.80) | Demote one step | The view rests on partial data |
| `max(dispersion_base, dispersion_quote) > max_dispersion` (1.20) | Demote one step | A leg's own pillars contradict each other |
| A high-impact `CalendarEvent` for either leg within the next 24 hours | Cap at `LOW` | The rate path could be repriced before the trade matures |

The 24-hour horizon in the last row is the one threshold in this section with no
`ScoringConfig` field behind it. It is written into the design rather than
configured, and `bias.conviction_for` takes it as the boolean
``event_within_24h`` rather than reading a number. Changing it means changing
code, not config.

Demotions compound. A pair with weak agreement, thin coverage and a central bank
meeting due can fall from `HIGH` to `NONE`.

If the final conviction is `NONE`, `Direction` is forced to `NEUTRAL` regardless
of the spread. The two fields must never disagree.

`max_dispersion` is a judgement, not a fitted value. On the score band, a
weighted dispersion above `max_dispersion` (1.20) means the pillars are
typically more than a full band unit apart from the composite, which in practice
means at least one pillar is arguing hard in the opposite direction.

## 6. Hard filters

These append a string to `PairBias.blockers`, and most of them set
`PairBias.tradeable = False`. The blocking ones are hard: a `HIGH` conviction
pair that trips one does not reach the shortlist. Conviction is a statement about
the model's belief; tradeability is a statement about whether the trade can be
executed sensibly, and the two are kept separate so a blocked pair still shows
its reasoning.

The table below is every kind of blocker the engine emits. `fbe.bias.BLOCKERS`
is the same list in code, and `tests/test_blockers.py` asserts the two match, so
a blocker cannot be added to one and not the other.

Kinds, not literal strings, because of two rows. Six of the eight are emitted
as the name given here. `event` is emitted as the reason `CalendarGuard`
returns, which names the event, its currency and its scheduled time, so that a
reader sees "EUR CPI at 09:00 UTC" rather than a bare flag. `event:unknown` is
emitted the same way, as `"event:unknown: <reason>"`, where the reason names
why the guard could not check: a failed calendar fetch, a cached week that
ends before the run's date, or a date beyond the horizon of the data supplied.
Anything reading `blockers` should therefore not match on exact equality for
either of those two rows. Neither reason's format is settled: `apply_filters`
and the guard are both still scaffolded.

| Blocker | Blocks | Test | Rationale |
| --- | --- | --- | --- |
| `cost` | yes | `cost_ratio > max_cost_ratio` (0.05) | Dealing cost eats too much of the plausible move |
| `event` | yes | The intended execution time falls inside a blackout window for either leg | The plan says do not trade around high-impact news |
| `coverage` | yes | `min(coverage_base, coverage_quote) < min_coverage` (0.60) | Under `min_coverage` of pillar weight, the composite is a guess |
| `no_coverage` | yes | Either leg has `coverage == 0.0` | No composite exists |
| `no_edge` | yes | `direction` is `NEUTRAL` or `conviction` is `NONE` | Nothing to act on, whatever the spread |
| `cost:unchecked` | no | No `cost_ratio` was supplied | The check did not run, so its silence is not an all-clear |
| `event:unchecked` | no | No `CalendarGuard` was supplied | The same, for the calendar |
| `event:unknown` | no | A `CalendarGuard` was supplied and could not determine either leg's status | A check that ran and failed is a different fact from a check that never ran, and the trader's response differs |

**The two `:unchecked` markers do not block, and neither does `event:unknown`.**
An offline run has no calendar and no cost input, and refusing to produce biases
would throw away the fundamentals, which are the slow half of the model and
still valid. So the run continues and records what it could not check. A guard
that was supplied and tried and failed is the same principle applied to a
different cause: refusing every pair on one bad fetch would cost a full trading
day over an outage that may resolve on the next run. The suffix is a convention
rather than a closed pair of names: `event:unknown` is that convention's second
use, added under `UNKNOWN_SUFFIX` for exactly the case ADR 0002 rule 4
anticipated, a check that ran and failed getting its own name rather than
sharing `:unchecked` with a check that never ran at all.

`event:unknown` not blocking is provisional, not settled. Whether unknown
calendar coverage should instead refuse the pair, for some or all currencies,
is open on issue #24 and is decided by the child of #41 that consumes this
marker. What is settled here is only that the marker exists and is
distinguishable, which is issue #43; the fail direction is a separate decision
built on top of it.

Both renderers must show a marker on a tradeable pair rather than printing an
unqualified "yes". A marker that reaches `PairBias` and stops there protects
nobody, which is ADR 0002 rule 3 and was the defect in issue #17: an offline run
showed `yes` in the tradeable column of all 28 rows, and a calendar that was
never consulted looked exactly like a calendar that was consulted and found
nothing.

**Cost ratio.** Compare the round-trip dealing cost against the move the pair can
plausibly produce over the bias horizon. Both thresholds here are
`ScoringConfig` fields, `horizon_days` and `max_cost_ratio`, with their defaults
in parentheses:

    expected_move_pips = atr_20d_pips * sqrt(horizon_days)      , horizon_days = 10
    cost_ratio = (typical_spread_pips + commission_pips) / expected_move_pips
    blocked if cost_ratio > max_cost_ratio                      , max_cost_ratio = 0.05

The square-root scaling is the standard random-walk approximation, which is
adequate here because the filter only needs to separate viable pairs from
obviously uneconomic ones. A ratio above `max_cost_ratio` means more than 5% of
the expected move, at the default, is paid to the broker before the position
starts, which on a R2000 account with 1-2% risk is decisive. This filter is why
exotic-ish G10 crosses will usually fail even when the score spread is wide, and
it is the mechanism by which the plan's "low spreads and trading costs" rule
enters the model.

**Event blackout.** Two timescales, handled at two different stages, and the
distinction matters. The hard filter uses only the execution window:
`DataConfig.calendar_blackout_before_min` (30) and `calendar_blackout_after_min`
(60) around a high-impact release on either leg. Inside that window there is no
order, full stop. The wider 24-hour test is a *conviction cap*, not a block
(section 5.4): a pair with a central bank decision tomorrow can still be traded,
but the model refuses to call it better than LOW, because a position opened today
would be held through a repricing the model has not seen.

Both legs are always checked. An FOMC decision blocks every dollar pair, and a
euro-area release blocks EURUSD regardless of what the dollar is doing. The
window definition, the keyword matching that catches mislabelled events, and the
separate question of whether to *hold* an open position through a release are
owned by `docs/risk-and-execution.md`. This document only decides whether a pair
reaches the shortlist.

## 7. Worked example

Invented but plausible inputs for a single run, carried through every stage. All
eight currencies are shown wherever the cross-sectional step requires them, since
a z-score cannot be computed from a subset. USD, JPY and NZD are the three
currencies followed to a pair conclusion.

Published scores are rounded to two decimals, and every later step in this
section uses those rounded values, so the arithmetic below can be reproduced with
a calculator. The re-standardisation pass of section 2.3 is applied to the five
cross-sectionally normalised pillars and its effect is shown explicitly.

### 7.1 MONETARY, in full

Raw inputs:

| Currency | `policy_rate` % | `yield_2y` % | `chg_1m` bp | `chg_3m` bp | `cpi_yoy` % | `real_policy_rate` % |
| --- | --- | --- | --- | --- | --- | --- |
| USD | 4.50 | 4.10 | +18 | +35 | 2.9 | +1.60 |
| EUR | 2.50 | 2.20 | -5 | -12 | 2.1 | +0.40 |
| GBP | 4.25 | 4.05 | +12 | +20 | 3.2 | +1.05 |
| JPY | 0.50 | 0.85 | +22 | +45 | 2.8 | -2.30 |
| CHF | 0.25 | 0.30 | -3 | -8 | 0.6 | -0.35 |
| CAD | 3.00 | 2.85 | -10 | -25 | 2.2 | +0.80 |
| AUD | 4.10 | 3.95 | +8 | +15 | 3.4 | +0.70 |
| NZD | 2.70 | 2.50 | -16 | -32 | 1.9 | +0.80 |

Cross-sectional statistics and z-scores:

| Sub-indicator | mean | sd | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `policy_rate` | 2.7250 | 1.5236 | +1.165 | -0.148 | +1.001 | -1.460 | -1.624 | +0.180 | +0.902 | -0.016 |
| `yield_2y` | 2.6000 | 1.3583 | +1.104 | -0.294 | +1.068 | -1.288 | -1.693 | +0.184 | +0.994 | -0.074 |
| `chg_1m` | 3.2500 | 12.8525 | +1.148 | -0.642 | +0.681 | +1.459 | -0.486 | -1.031 | +0.370 | -1.498 |
| `chg_3m` | 4.7500 | 26.3427 | +1.148 | -0.636 | +0.579 | +1.528 | -0.484 | -1.129 | +0.389 | -1.395 |
| `real_policy_rate` | 0.3375 | 1.1233 | +1.124 | +0.056 | +0.634 | -2.348 | -0.612 | +0.412 | +0.323 | +0.412 |

Blending with sub-weights 0.15 / 0.25 / 0.20 / 0.25 / 0.15, for USD:

    0.15*(+1.165) + 0.25*(+1.104) + 0.20*(+1.148) + 0.25*(+1.148) + 0.15*(+1.124)
    = 0.17475 + 0.27600 + 0.22960 + 0.28700 + 0.16860
    = +1.13595

Same blend for the rest of the universe:

| | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| blended | +1.136 | -0.375 | +0.793 | -0.220 | -0.977 | -0.354 | +0.603 | -0.607 |

Now the re-standardisation pass of section 2.3. This example is a single run with
no stored history, so `blend_divisor` cannot reach
`ScoringConfig.min_restandardisation_runs` and returns the `run_local` path: the
divisor is this run's own `sd(blend)`, and `PillarScore.notes` would say so.
Every currency has all five sub-indicators, so the blended column has a mean of
exactly 0 and the pass reduces to a division by that standard deviation:

    sd(blend) = 0.7001
    USD: +1.13595 / 0.7001 = +1.6227  ->  published as +1.62

On the rolling path the divisor would instead be the median `sd(blend)` over the
last 60 runs and every score below would move with it. The whole of section 7 is
the fallback path.

This is the effect the pass exists to correct, and MONETARY is where it is
largest in this run. Five sub-indicators that agree in direction but not in
detail blend down to a standard deviation of 0.70, so before scaling, the pillar
carrying 0.30 of the model's weight was speaking about 30% more quietly than its
weight implies. Section 7.2 shows the other end of the range.

| | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MONETARY score | +1.62 | -0.54 | +1.13 | -0.31 | -1.40 | -0.51 | +0.86 | -0.87 |

Note JPY at -0.31 despite having by far the fastest-rising 2-year yield in the
set. The two momentum terms score +1.459 and +1.528, the highest in the universe,
but the level terms and a real policy rate of -2.30% pull it back to slightly
negative. This is the pillar working as designed: direction of travel matters,
but it does not by itself close a 380 basis point gap to the dollar.

### 7.2 INFLATION

Targets come from `CurrencyMeta.inflation_target`: 2.0 for USD, EUR, GBP, JPY,
CAD and NZD; 1.0 for CHF; 2.5 for AUD.

| Currency | `cpi_yoy` | headline deviation | `core_cpi_yoy` | core deviation |
| --- | --- | --- | --- | --- |
| USD | 2.9 | +0.9 | 3.1 | +1.1 |
| EUR | 2.1 | +0.1 | 2.4 | +0.4 |
| GBP | 3.2 | +1.2 | 3.6 | +1.6 |
| JPY | 2.8 | +0.8 | 2.5 | +0.5 |
| CHF | 0.6 | -0.4 | 0.9 | -0.1 |
| CAD | 2.2 | +0.2 | 2.5 | +0.5 |
| AUD | 3.4 | +0.9 | 3.2 | +0.7 |
| NZD | 1.9 | -0.1 | 2.3 | +0.3 |

Deviation, not level, is doing real work here. AUD prints the highest headline
CPI in the set at 3.4% but scores the same +0.9 deviation as the US at 2.9%,
because the Australian target is 2.5%. Scoring the raw level would have made AUD
the most hawkish currency in the universe on a number that is, against its own
target, unremarkable.

Headline deviation: mean +0.4500, sd 0.5362. Core deviation: mean +0.6250, sd
0.4867. Blending 0.40 headline and 0.60 core:

| | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| blended | +0.921 | -0.538 | +1.761 | +0.107 | -1.528 | -0.341 | +0.428 | -0.811 |

Here `sd(blend) = 0.9711`, so re-standardisation scales by 1.03 and changes
almost nothing:

| | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| INFLATION score | +0.95 | -0.55 | +1.81 | +0.11 | -1.57 | -0.35 | +0.44 | -0.84 |

The contrast with MONETARY is the point. Two sub-indicators that measure nearly
the same thing, headline and core inflation against the same target, barely
diversify each other, so their blend keeps almost all of its variance. Five
sub-indicators spanning policy rates, yields, two momentum windows and a real
rate diversify a great deal, so their blend loses 30% of its. The scaling
factors differ, 1.03 against 1.43, precisely because the pillars differ in how
much internal disagreement they contain. Skipping the pass would have handed
INFLATION an advantage over MONETARY that neither the data nor the config
intended.

### 7.3 GROWTH, EMPLOYMENT, EXTERNAL

Raw inputs, then the same procedure (z-score each column across the eight, blend
with the sub-weights in section 3):

| Currency | `gdp_yoy` | `pmi_composite` | `indpro_yoy` | `retail_sales_yoy` |
| --- | --- | --- | --- | --- |
| USD | 2.4 | 53.1 | +1.2 | 3.2 |
| EUR | 0.9 | 49.8 | -1.8 | 1.1 |
| GBP | 1.1 | 51.2 | -0.6 | 1.9 |
| JPY | 0.6 | 50.4 | +0.4 | 1.4 |
| CHF | 1.3 | 48.9 | +2.1 | 0.8 |
| CAD | 1.6 | 50.9 | +0.9 | 2.2 |
| AUD | 1.8 | 51.8 | +1.4 | 2.6 |
| NZD | 0.1 | 47.6 | -1.1 | 0.2 |

| Currency | unemployment 6m change (pp) | employment momentum (% ann.) | `current_account_gdp` | trade balance 3m change | terms of trade 3m % |
| --- | --- | --- | --- | --- | --- |
| USD | +0.2 | +1.1 | -3.3 | -0.1 | 0.0 |
| EUR | 0.0 | +0.5 | +2.8 | +0.2 | 0.0 |
| GBP | +0.3 | +0.2 | -2.6 | 0.0 | 0.0 |
| JPY | -0.1 | +0.3 | +3.6 | +0.3 | 0.0 |
| CHF | +0.1 | +0.7 | +6.2 | +0.1 | 0.0 |
| CAD | +0.4 | +0.4 | -0.9 | -0.2 | -8.0 (crude) |
| AUD | +0.1 | +1.3 | +1.2 | +0.4 | +6.0 (iron ore) |
| NZD | +0.5 | -0.2 | -5.1 | -0.3 | +3.0 (dairy) |

Resulting scores:

| Pillar | `sd(blend)` | scaling | USD | EUR | GBP | JPY | CHF | CAD | AUD | NZD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GROWTH | 0.8907 | 1.12 | +1.67 | -0.82 | -0.01 | -0.38 | -0.19 | +0.51 | +0.99 | -1.77 |
| EMPLOYMENT | 0.8450 | 1.18 | +0.69 | +0.54 | -0.79 | +0.59 | +0.48 | -0.84 | +1.26 | -1.93 |
| EXTERNAL | 0.7669 | 1.30 | -0.78 | +0.61 | -0.51 | +0.90 | +0.93 | -1.45 | +1.36 | -1.06 |

The unemployment column is sign-flipped inside EMPLOYMENT, which is why JPY, the
only country whose unemployment rate fell, scores positively on that term.

### 7.4 POSITIONING

| Currency | net non-commercial, % of OI | 3-year mean | 3-year sd | `p` | `f(p)` | branch |
| --- | --- | --- | --- | --- | --- | --- |
| USD | +26.2 | +9.8 | 8.63 | +1.90 | +0.10 | fading |
| EUR | -2.4 | +3.6 | 10.00 | -0.60 | -0.60 | momentum |
| GBP | +7.9 | +3.7 | 10.50 | +0.40 | +0.40 | momentum |
| JPY | -31.5 | -6.3 | 10.50 | -2.40 | +0.60 | contrarian |
| CHF | -14.1 | -2.0 | 11.00 | -1.10 | -0.90 | fading |
| CAD | -12.5 | -4.5 | 10.00 | -0.80 | -0.80 | momentum |
| AUD | +5.4 | -1.6 | 10.00 | +0.70 | +0.70 | momentum |
| NZD | +18.4 | +4.1 | 11.00 | +1.30 | +0.70 | fading |

Two readings worth following. USD at `p = +1.90` is the most crowded long in the
set and scores +0.10, essentially nothing: the dollar bull case is well owned and
positioning has stopped adding to it. JPY at `p = -2.40` is past the contrarian
boundary and scores **+0.60**, a positive contribution to a currency the model
otherwise dislikes, computed as `-(-1) * 1.5 * (2.40 - 2.00) = +0.60`. That is
the pillar doing the one job it exists to do.

No currency in this run reaches the contrarian saturation point of `|p| = 3.33`,
so the cap added in section 3.6 does not bind on any of these values. The most
extreme reading, JPY at `|p| = 2.40`, sits well inside it.

### 7.5 RISK

A broad equity index sits 6.5% below its 52-week high, and the volatility index
is 1.2 standard deviations above its 3-year mean.

    dd_component  = clip(-6.5 / 10.0)  = -0.650
    vol_component = clip(-1.2 / 2.0)   = -0.600
    R = 0.5*(-0.650) + 0.5*(-0.600) = -0.625

A mild but real risk-off. Then `score = 2.0 * R * risk_beta = -1.25 * risk_beta`:

| Currency | `risk_beta` | RISK score |
| --- | --- | --- |
| USD | -0.5 | +0.62 |
| EUR | +0.1 | -0.12 |
| GBP | +0.3 | -0.38 |
| JPY | -0.9 | +1.12 |
| CHF | -0.7 | +0.88 |
| CAD | +0.4 | -0.50 |
| AUD | +0.9 | -1.12 |
| NZD | +0.8 | -1.00 |

### 7.6 The pillar matrix

| Currency | MON 0.30 | INF 0.15 | GRO 0.15 | EMP 0.10 | EXT 0.10 | POS 0.10 | RSK 0.10 | coverage | composite | dispersion | rank |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USD | +1.62 | +0.95 | +1.67 | +0.69 | -0.78 | +0.10 | +0.62 | 1.000 | **+0.9420** | 0.7756 | 1 |
| AUD | +0.86 | +0.44 | +0.99 | +1.26 | +1.36 | +0.70 | -1.12 | 1.000 | +0.6925 | 0.6607 | 2 |
| GBP | +1.13 | +1.81 | -0.01 | -0.79 | -0.51 | +0.40 | -0.38 | 1.000 | +0.4810 | 0.8729 | 3 |
| JPY | -0.31 | +0.11 | -0.38 | +0.59 | +0.90 | +0.60 | +1.12 | 1.000 | **+0.1875** | 0.5426 | 4 |
| EUR | -0.54 | -0.55 | -0.82 | +0.54 | +0.61 | -0.60 | -0.12 | 1.000 | -0.3245 | 0.4819 | 5 |
| CAD | -0.51 | -0.35 | +0.51 | -0.84 | -1.45 | -0.80 | -0.50 | 1.000 | -0.4880 | 0.5168 | 6 |
| CHF | -1.40 | -1.57 | -0.19 | +0.48 | +0.93 | -0.90 | +0.88 | 1.000 | -0.5450 | 0.9665 | 7 |
| NZD | -0.87 | -0.84 | -1.77 | -1.93 | -1.06 | +0.70 | -1.00 | 0.950 | **-0.9774** | 0.7056 | 8 |

**USD composite**, all pillars fresh so `coverage = 1.000`:

    0.30*(+1.62) + 0.15*(+0.95) + 0.15*(+1.67) + 0.10*(+0.69)
      + 0.10*(-0.78) + 0.10*(+0.10) + 0.10*(+0.62)
    = +0.4860 +0.1425 +0.2505 +0.0690 -0.0780 +0.0100 +0.0620
    = +0.9420 ,  divided by coverage 1.000  ->  +0.9420

**JPY composite**, also fully covered:

    0.30*(-0.31) + 0.15*(+0.11) + 0.15*(-0.38) + 0.10*(+0.59)
      + 0.10*(+0.90) + 0.10*(+0.60) + 0.10*(+1.12)
    = -0.0930 +0.0165 -0.0570 +0.0590 +0.0900 +0.0600 +0.1120
    = +0.1875  ->  +0.1875

**NZD composite**, demonstrating the staleness discount. New Zealand's external
inputs are late against their own allowances and the pillar's freshness factor
comes to `phi = 0.500`, so `w_eff = 0.10 * 0.500 = 0.050`. Section 4.1 gives the
ramp and the sub-weighted mean that produces the 0.500; the ages behind it are
not part of this fixture, because they move whenever the registry's allowances
are re-verified and the aggregation arithmetic below does not:

    coverage = 0.30 + 0.15 + 0.15 + 0.10 + 0.050 + 0.10 + 0.10 = 0.950

    weighted sum
    = 0.300*(-0.87) + 0.150*(-0.84) + 0.150*(-1.77) + 0.100*(-1.93)
      + 0.050*(-1.06) + 0.100*(+0.70) + 0.100*(-1.00)
    = -0.2610 -0.1260 -0.2655 -0.1930 -0.0530 +0.0700 -0.1000
    = -0.9285

    composite = -0.9285 / 0.950 = -0.9774

Renormalising matters here: without the division the composite would read -0.929,
understating New Zealand's weakness purely because one pillar was old. The stale
pillar was itself negative, so discounting it moved the composite the other way
and the renormalisation put it back. Coverage of 0.950 is then carried forward to
the conviction tests.

**NZD dispersion**, with `w_tilde(p) = w_eff(p) / 0.950`:

    (0.300/0.950)*(-0.87 +0.9774)^2 = 0.3158 * 0.01153 = 0.003640
    (0.150/0.950)*(-0.84 +0.9774)^2 = 0.1579 * 0.01887 = 0.002979
    (0.150/0.950)*(-1.77 +0.9774)^2 = 0.1579 * 0.62822 = 0.099200
    (0.100/0.950)*(-1.93 +0.9774)^2 = 0.1053 * 0.90745 = 0.095527
    (0.050/0.950)*(-1.06 +0.9774)^2 = 0.0526 * 0.00682 = 0.000359
    (0.100/0.950)*(+0.70 +0.9774)^2 = 0.1053 * 2.81368 = 0.296165
    (0.100/0.950)*(-1.00 +0.9774)^2 = 0.1053 * 0.00051 = 0.000054

    sum = 0.497924 ,  dispersion = sqrt(0.497924) = 0.7056

Under the 1.20 threshold, so no dispersion demotion. Most of that dispersion is
the POSITIONING pillar at +0.70 arguing against a currency every other pillar
dislikes: that single term supplies 0.296 of the 0.498 total, which is exactly
the disagreement the metric is meant to surface.

### 7.7 Pairs

Three crosses from the three followed currencies, in `universe.ALL_PAIRS`
convention (the precedence order puts NZD ahead of USD and USD ahead of JPY).

**NZDUSD**

    spread = composite(NZD) - composite(USD) = -0.9774 - 0.9420 = -1.9194

| Pillar | NZD | USD | difference | agrees with a negative spread? | `w_pair` |
| --- | --- | --- | --- | --- | --- |
| MONETARY | -0.87 | +1.62 | -2.49 | yes | 0.300 |
| INFLATION | -0.84 | +0.95 | -1.79 | yes | 0.150 |
| GROWTH | -1.77 | +1.67 | -3.44 | yes | 0.150 |
| EMPLOYMENT | -1.93 | +0.69 | -2.62 | yes | 0.100 |
| EXTERNAL | -1.06 | -0.78 | -0.28 | yes | 0.075 |
| POSITIONING | +0.70 | +0.10 | +0.60 | no | 0.100 |
| RISK | -1.00 | +0.62 | -1.62 | yes | 0.100 |

`w_pair` for EXTERNAL is `(0.050 + 0.100) / 2 = 0.075`, reflecting New Zealand's
stale data on that pillar.

    considered = 0.300+0.150+0.150+0.100+0.075+0.100+0.100 = 0.975
    agreeing   = 0.975 - 0.100 = 0.875
    agreement  = 0.875 / 0.975 = 0.8974

Conviction: `abs(-1.9194) = 1.9194`, which is at or above `min_spread_medium`
(1.50) and below `min_spread_high` (2.50), so the base tier is **MEDIUM**. Then
the demotion tests: agreement 0.8974 is above 0.60, so no cap; `min(coverage)` is
0.950, above 0.80, so no demotion; `max(dispersion)` is `max(0.7056, 0.7756) =
0.7756`, under 1.20, so no demotion; no high-impact event for either leg in the
next 24 hours, and no blackout window is active. Conviction stays **MEDIUM**.

Cost filter: typical dealing spread 1.8 pips, 20-day ATR 62 pips.

    expected_move = 62 * sqrt(10) = 62 * 3.1623 = 196.06 pips
    cost_ratio = 1.8 / 196.06 = 0.0092

Well under 0.05, so it passes.

    PairBias(pair="NZDUSD", base="NZD", quote="USD",
             spread=-1.9194, direction=SHORT, conviction=MEDIUM,
             base_score=-0.9774, quote_score=+0.9420,
             agreement=0.8974, tradeable=True, blockers=())

In words: short New Zealand dollar against US dollar. Six of seven pillars agree,
the exception is positioning, which is mildly crowded long NZD and therefore
counts against the trade. This is the model's best idea in the run.

**USDJPY**

    spread = +0.9420 - 0.1875 = +0.7545

| Pillar | USD | JPY | difference | agrees with a positive spread? | `w_pair` |
| --- | --- | --- | --- | --- | --- |
| MONETARY | +1.62 | -0.31 | +1.93 | yes | 0.300 |
| INFLATION | +0.95 | +0.11 | +0.84 | yes | 0.150 |
| GROWTH | +1.67 | -0.38 | +2.05 | yes | 0.150 |
| EMPLOYMENT | +0.69 | +0.59 | +0.10 | yes | 0.100 |
| EXTERNAL | -0.78 | +0.90 | -1.68 | no | 0.100 |
| POSITIONING | +0.10 | +0.60 | -0.50 | no | 0.100 |
| RISK | +0.62 | +1.12 | -0.50 | no | 0.100 |

    agreement = 0.700 / 1.000 = 0.7000

Conviction: `abs(+0.7545) = 0.7545`, which clears `min_spread_low` (0.75) by
0.0045 and falls short of `min_spread_medium`, so the base tier is **LOW** and
the direction is `LONG`. Agreement of 0.7000 is above `min_agreement`, so no cap
applies, though a cap to LOW would have changed nothing here. Coverage is 1.000
on both legs and `max(dispersion) = max(0.7756, 0.5426) = 0.7756`, so no
demotion.

This pair is the model's most instructive result, for two reasons.

**It is a genuine disagreement, not a strong view.** The rate story strongly
favours the dollar: MONETARY contributes +1.93 of the difference and GROWTH
+2.05. But Japan is running a current account surplus, speculative positioning is
at a two-and-a-half sigma short, and the mild risk-off is supporting the yen.
Three of the seven pillars, carrying 0.30 of the weight between them, point the
other way. The composite is the residual after that argument, and a residual of
0.75 on a band that runs to 3.0 is a weak claim. LOW is the correct label and the
`docs/risk-and-execution.md` ladder correctly makes it the smallest position with
the highest reward requirement.

**It sits on a threshold, and thresholds are conventions.** A spread of 0.7545
against a cutoff of 0.75 is decided by less than half a hundredth of a band unit.
Nothing about this pair is meaningfully different from one scoring 0.7450 and
being called NONE. That is a real weakness of bucketing a continuous quantity,
it is recorded as open question 8, and the practical defence is already in the
plan: the pre-trade checklist in `docs/risk-and-execution.md` asks whether the
spread score is meaningful rather than a rounding difference between two nearly
flat currencies. Here it is a rounding difference, and a trader who treats this
LOW as barely distinguishable from no view is reading the model correctly.

For a clean NONE in the same run, take EURCAD, both legs of which are in the
matrix above: `spread = -0.3245 - (-0.4880) = +0.1635`, far inside the neutral
band, so `Conviction.NONE` and `Direction.NEUTRAL`. Roughly two thirds of the 28
crosses should land there in a typical run.

**NZDJPY**

    spread = -0.9774 - 0.1875 = -1.1649

Every pillar except POSITIONING points the same way as the spread, giving the
same agreement as NZDUSD: `0.875 / 0.975 = 0.8974`. Base tier from
`abs(-1.1649) = 1.1649`, which sits between 0.75 and 1.50, so **LOW**. No
demotion applies. Cost: dealing spread 3.2 pips against a 20-day ATR of 88 pips,
so `expected_move = 88 * 3.1623 = 278.28` and `cost_ratio = 3.2 / 278.28 =
0.0115`. Passes.

    PairBias(pair="NZDJPY", base="NZD", quote="JPY",
             spread=-1.1649, direction=SHORT, conviction=LOW,
             agreement=0.8974, tradeable=True, blockers=())

Now look at the three results together, because this is the relative-value logic
made concrete and it is the most useful thing in the example.

NZDUSD and NZDJPY are the same New Zealand view expressed against two different
funding currencies, and the second is weaker only because the yen is itself the
fourth-strongest currency in the run rather than the strongest. Taking both is
not two trades, it is one short-NZD position at roughly double size.

The third result makes it sharper. USDJPY LONG is long dollar and short yen.
NZDJPY SHORT is short New Zealand dollar and long yen. Held together the yen legs
cancel and what remains is long USD against short NZD, which is NZDUSD SHORT, the
position the model already rates MEDIUM on its own. Three tickets, two of them
LOW conviction and carrying a dealing cost each, reconstruct one position that
was available directly at a better tier and a tighter spread.

This is what `RiskConfig.max_correlated_exposure` exists to catch and what the
"running a book" section of `docs/methodology.md` is about. The correct book here
holds NZDUSD and nothing else.

### 7.8 What the trader receives

Shortlist for the run: NZDUSD short at MEDIUM, NZDJPY short at LOW and USDJPY
long at LOW, with a note that the three collapse into a single short-NZD,
long-USD exposure and that only one of them should be held.

The engine stops there. It has produced no entry price, no target, and no time.
The trader now looks at the NZDUSD 4-hour chart, and if and only if there is an
ascending trendline to break, or a channel top to sell into, with a stop that can
sit beyond the structure at a distance the 1-2% rule can fund, there is a trade.
If the chart shows a clean uptrend with no structure to sell, there is no trade,
and the bias simply stands until next week.

## 8. Re-weighting

The weights are priors. They can be changed, but the bar is high and the
procedure matters more than the outcome.

**Constraints that must hold.**

1. Weights sum to 1.0. `Config.validate()` enforces this and a run must refuse to
   proceed otherwise.
2. No pillar exceeds 0.40. Above that, a single data pipeline becomes a single
   point of failure for every currency.
3. No pillar goes below 0.05. A pillar worth less than that should be deleted,
   not diminished, because it still costs a data dependency and a maintenance
   burden.
4. MONETARY stays the largest. If a re-weighting demotes it, the claim being made
   is that rate expectations no longer dominate G10 FX, and that claim needs an
   argument, not a spreadsheet.

**How to change a weight.**

Change one pillar at a time. Record the reason in the commit message in the form
"this pillar should matter more because X", where X is a statement about how
markets work rather than a statement about recent results. Re-run the engine over
the last 26 weeks of stored reports and inspect what changed: which pairs moved
tier, which currencies changed rank, and whether the pairs that changed are ones
you can now explain better or worse than before. The config digest recorded on
every `BiasReport` makes this comparison possible, which is why it exists.

**How to tell an improvement from a curve fit.**

This is the question that matters, and there is no test that settles it. There
are, however, four reliable warning signs:

- **The change was prompted by an outcome.** A losing NZDUSD trade is not
  evidence that EXTERNAL deserves more weight. It is evidence of one trade. If
  the argument for the new weight cannot be made without reference to a specific
  position, it is not an argument.
- **The improvement is concentrated.** If the new weights change the verdict on
  three pairs and two of them are the three you were annoyed about, the model has
  been fitted to a memory.
- **The reason is a number.** "0.22 works better than 0.30" is a curve fit
  wearing a lab coat. "Rate differentials matter less when every G10 central bank
  is at the same rate, because the differential is near zero and second-order
  drivers dominate" is a mechanism, and a mechanism can be argued with.
- **It cannot be stated in advance.** A genuine improvement can be written down
  before the data is re-run, as a prediction about which pairs will change and
  why. Write it down first. If the prediction fails, the reasoning was wrong even
  if the new weights look better.

The safest default is to change nothing. The model's value is in producing a
consistent, documented prior week after week, and a prior that is retuned every
month is not a prior. A reasonable review cadence is once a year, or after a
structural regime change such as a G10 central bank formally abandoning an
inflation target.

**Sensitivity check.** Before adopting any new weight set, re-run the previous
26 weeks under the old and new weights and count how many pair-weeks changed
conviction tier. If more than roughly 20% changed, the model is more sensitive to
weights than to data, which is itself a finding and an argument for widening the
neutral band rather than for adopting the new weights.

## 9. Test obligations

An implementer should assert, at minimum:

- Weights sum to 1.0; `Config.validate()` returns empty for the defaults.
- Every pillar returns a score for every currency in the universe, or marks the
  pillar absent. No silent zeros for missing data.
- All scores lie within `+/- score_clip` after clipping.
- Cross-sectional z-scores over a run have mean 0 and population sd 1, to within
  floating point tolerance, for any sub-indicator with full coverage.
- Every pillar's `z` has cross-sectional mean 0 after the re-standardisation
  pass of section 2.3, for the five pillars that use it, and the pass is **not**
  applied to POSITIONING or RISK. Population standard deviation is exactly 1 only
  where `blend_divisor` took the `run_local` path, which is the path a test with
  no supplied history gets. Under the `rolling` path the standard deviation is
  1 divided by the ratio of the run's own `sd(blend)` to the historical median,
  and asserting exactly 1 there would be asserting the bug section 2.3 exists to
  avoid.
- `blend_divisor` returns `"run_local"` below
  `ScoringConfig.min_restandardisation_runs` of history and `"rolling"` at or
  above it, and the path it returned reaches `PillarScore.notes` in both cases.
- A pillar is absent for a currency holding at or below `MIN_COMPONENT_WEIGHT`
  (0.5) of that pillar's sub-weight, and that absence lowers coverage rather than
  producing a score. The boundary itself is the case to assert, since it is
  where the two readings of the rule differ: EMPLOYMENT with one of its two
  components, and GROWTH with one 0.30 component and one 0.20 component, are
  both absent rather than scored. A currency missing only `cpi_yoy` holds 0.85
  of MONETARY and still scores, which is what keeps the widened comparison from
  sweeping up the ordinary missing-series case.
- No registered indicator is born stale. For every entry in the registry, a print
  at the age its own frequency publishes at, `DEFAULT_PUBLICATION_LAG_DAYS`,
  carries a freshness factor above zero. An indicator that is admitted by the
  visibility rule and then given no weight can never contribute to a score.
- `scoring.freshness` and `registry.coverage_report` agree about every series on
  every date, in the direction that matters: a factor above zero implies the
  registry calls the series usable, and a series the registry calls stale has a
  factor of exactly zero. Assert it for `cpi_yoy` on AUD, `gdp_yoy` on USD and
  `yield_2y` on GBP at minimum.
- Components of one pillar are aged separately, so a 162-day-old GDP print and a
  5-day-old retail sales print produce two different factors and the older enters
  the blend at a reduced sub-weight rather than at 0.30.
- `f(p)` in POSITIONING is continuous at `|p| = 1.0` and `|p| = 2.0`, is odd, and
  has the sign changes stated in section 3.6.
- `f(p)` saturates at magnitude 2.0 from `|p| = 3.33` upward, so it never reaches
  `score_clip`. `abs(f(p)) <= 2.0` for every finite `p`.
- With `R = 0`, every RISK score is exactly 0.
- `spread(base, quote) == -spread(quote, base)` for every pair, and the
  composites are transitive by construction.
- `direction == NEUTRAL` whenever `conviction == NONE`, in both directions of the
  implication where the spread is inside the neutral band.
- A pair with a stale leg has lower coverage and, at the boundary, a lower
  conviction tier than the same pair with fresh data.
- The worked example in section 7 reproduces to two decimal places. It is a
  fixture, not an illustration.
- The effective loadings on headline CPI in sections 3.1 and 3.2 reproduce from
  the section 7 dispersions, to the precision published, and the sub-weights and
  pillar weights they are computed from are read from the pillars and from
  `ScoringConfig` rather than restated. A sub-weight edit that moves the
  cancellation must fail the build, since that is the way the opposing loading
  went unnoticed in the first place. Assert an equality, not a tolerance: the
  arithmetic is exact given the fixture, and a tolerance wide enough to absorb a
  sub-weight edit is wide enough to hide the defect.

## 10. Open questions

Each question below carries its current status and points at the evidence behind
it. Four specialists worked these; the findings are in
`docs/answers/framework.md`, `docs/answers/scoring-maths.md`,
`docs/answers/data.md` and `docs/answers/product.md`. The statuses are the four
verdicts those files use:

| Status | Meaning |
| --- | --- |
| **Answered** | Settled, with evidence cited. "Change nothing" is a legitimate answer and several of these are. |
| **Narrowed** | Not settled. Options eliminated or the answer bounded, with the remaining range stated. |
| **Blocked on trade data** | Cannot be settled until the journal holds closed trades. The measurement and the sample it needs are named. |
| **Judgement call** | No empirical answer exists. It is a preference, stated as one. |

Recording a status is not resolving a question. Nothing below has been applied
to the pillar definitions, the sub-weights or `ScoringConfig`, and none of it may
be silently resolved in code without updating this section first. Where an item
says a change is recommended, the change is still a proposal and the body of this
document is unchanged.

One correction has been carried into the body since these questions were
written, and it was never one of them: section 2.3 now describes the
re-standardisation divisor the code actually uses. That was a divergence between
the specification and `src/fbe/pillars/base.py`, not an open question.

No number anywhere below is a measured result about returns. The measurements
cited are sensitivity measurements, how far the model's own output moves when one
of its own choices moves, and the published research cited is about the FX market
in general and was not run on this model. Phase 6 remains the first point at
which anyone can say whether the engine works.

### 1. Inflation credibility

Section 3.2 assumes above-target inflation is currency-positive through the
policy channel. When a central bank is credibly ignoring an overshoot the sign
inverts, and the model has no switch for this. The idea behind any fix is to let
the rate market decide whether the bank is expected to respond, rather than
assuming it will.

**Status: answered on the double-counting objection, narrowed on the
instrument.** Evidence: `docs/answers/framework.md` Q1. Section 3.2 stands as
written, headline at 0.40 and core at 0.60, no gate.

**Candidate: a front-end response gate.** Define a gate from the same 3-month
change in the 2-year yield that MONETARY already consumes:

    g = clip( yield_2y_chg_3m / 0.10pp , 1.0 )       , so g is in -1 .. +1

A **positive** inflation gap is then scored `gap * g`, and a **negative** gap is
left ungated and stays currency-negative. In words: inflation above target only
helps the currency to the extent the front end is actually repricing toward a
response, and if the front end is repricing the other way the same overshoot
counts against the currency. Inflation below target is currency-negative either
way, since a bank undershooting its target has no reason to tighten regardless of
what the curve is doing. The rule pivots continuously at `gap = 0`, where both
branches give 0, so there is no jump at the target. It answers the question with
a series already in the model rather than a new data dependency, and it degrades
gracefully: at `g = 0`, a flat front end, inflation simply stops contributing
instead of contributing the wrong sign.

**Why it is still not in the pillar.** One of the two reasons this item
originally gave survives and one does not. The surviving reason is that the gate
adds two free parameters, the `0.10pp` scale and the pivot rule, and conditionals
are what the methodology's section on separating fundamentals from technicals
warns about. The reason that does not survive is the claim of double-counting,
which was wrong as stated and is replaced by the three findings below.

**What was found.**

- **Not literal double-counting.** `real_policy_rate` is `policy_rate - cpi_yoy`
  and contains no yield-change term. The gate would introduce a product,
  `gap x front-end change`, and no such interaction exists anywhere in the model.
  A product of two variables does not double-count either of them linearly.
- **The overlap is real in effect.** The one clear within-G10 instance of a
  credibly tolerated overshoot is the Bank of Japan through 2022, where `g` would
  have been near zero and `real_policy_rate` was deeply negative at the same time
  and for the same reason. The two corrections fire on the same episodes and push
  the same way. Section 7.1 shows the mechanism at a smaller scale: JPY has the
  fastest-rising 2-year yield in the universe, at +1.459 and +1.528, and still
  finishes MONETARY at -0.31, pulled there by a real policy rate of -2.30%.
- **A third and larger overlap, which this item did not previously mention.**
  `real_policy_rate` carries an explicit coefficient of minus one on headline
  CPI. INFLATION carries a positive loading on the same series. The model
  therefore holds an inflation term with two opposing signs that partly cancel by
  construction. Computed from the worked example's own cross-sectional
  dispersions, per 1pp of headline CPI:

  | Term | Composite loading |
  | --- | --- |
  | INFLATION, headline sub-indicator | +0.1008 |
  | INFLATION, core sub-indicator, if core moves one for one with headline | +0.1666 |
  | MONETARY, real policy rate | -0.0501 |

  If headline moves alone, the real policy rate term cancels **49.7%** of
  INFLATION's response. If headline and core move together it cancels **18.7%**.
  This is arithmetic from one run's dispersions, not a measurement, and the exact
  figures move run to run because every `sd` in them is recomputed daily. The
  direction and the rough magnitude do not move.

  This matters beyond the gate. Section 2.3 exists so that the section 3 weights
  are the whole of the model's opinion about relative importance. The `-cpi_yoy`
  inside `real_policy_rate` breaks that guarantee for the one pillar it touches.
  **Ruled on in ADR 0003**: the term stays, no sub-weight and no `ScoringConfig`
  value changes, and the loadings are published in sections 3.1 and 3.2 with a
  test that reproduces them. Section 2.3 now states that its guarantee is per
  pillar and does not extend to a series appearing in two pillars with opposing
  signs. The choice between this term and the gate is still open and still
  belongs with item 2.

**On the base sign and the credibility conditioning.** The base sign is supported
by published work on the FX market in general: Clarida and Waldman, studying
10-minute windows around inflation announcements for 10 countries from 2001 to
2005, find higher than expected inflation appreciates the currency, through the
near-term policy rate rather than through beliefs about the long-run price level
([NBER w13010](https://www.nber.org/papers/w13010)). The credibility conditioning
is also supported, but mostly out of sample for this universe: the documented
cross-country variation in that response is between advanced and emerging
economies ([Dallas Fed, 2024](https://www.dallasfed.org/research/economics/2024/0903)),
and all eight currencies here sit at the transparent, inflation-targeting end of
that scale. The gate would be harvesting an effect whose measured dispersion
comes from a sample this model does not trade.

**On a cleaner instrument, narrowed.** Three candidates eliminated. Market-based
inflation expectations, breakevens or inflation swaps, fail on coverage, since
after US TIPS and UK index-linked gilts the G10 runs out of free series and
`MIN_COMPONENT_WEIGHT` would leave the term absent for most of the universe, and
on sign, since a rising breakeven can mean the bank is expected to hike or that
the market has stopped believing the target. Substituting a real 2-year yield for
the nominal adds nothing `real_policy_rate` does not carry and enlarges the
cancellation above. A hand-set credibility flag in `CurrencyMeta` is a free
parameter with no update process and interacts badly with item 9. What remains is
a choice rather than an addition: the gate and the `real_policy_rate` term are
two implementations of one correction and should be chosen between rather than
stacked, which dissolves the double-counting objection because you cannot
double-count if you only run one. Choosing between them requires measurement that
does not exist.

### 2. Sub-weights are undefended

The seven pillar weights are argued for. The sub-weights inside each pillar
(0.15 / 0.25 / 0.20 / 0.25 / 0.15 in MONETARY, 0.40 / 0.60 in INFLATION, and so
on) are reasonable but essentially asserted. Equal weighting within each pillar
is a defensible alternative that would remove eleven free parameters.

**Status: answered for the measurable part, blocked on which scheme is better.**
Evidence: `docs/answers/scoring-maths.md` Spec 2. The sub-weights stand as
specified in section 3.

**What was measured.** On the section 7 fixture, replacing all eleven sub-weights
with equal weights inside each pillar changes no currency's rank, no pair's
direction and no pair's conviction tier. The largest pair spread moves 0.1187 and
the mean move is 0.0386, against a cross-pair spread standard deviation of 0.9538
in that run, so eleven parameters are worth about 4% of the signal's own scale.
Per pillar, the two blends correlate between 0.99265 for MONETARY, the least
internally agreeing pillar, and 1.00000 for EMPLOYMENT, whose two components are
already equally weighted. Over 5,000 synthetic runs and 140,000 pair
observations, equal sub-weights change 3.42% of conviction tiers and 0.00% of
directions, against 6.07% of tiers for a single 0.05 shift between two pillar
weights. Section 8's own sensitivity bar is roughly 20%. Sub-weights can only
matter to the extent that a pillar's components disagree, and inside these
pillars they mostly do not.

**What is blocked.** Which scheme is better. Two constructions whose blends
correlate between 0.993 and 1.000 across an eight-name cross-section are the same
construction as far as any test on that cross-section is concerned, and because
the two schemes never disagree on direction, a hit-rate comparison has nothing to
count. This does not become answerable by waiting for more forward record.

**Two findings filed here rather than acted on.** The level-versus-momentum split
inside MONETARY, currently 0.40 on the two level terms against 0.45 on the two
momentum windows, is the one sub-weight division carrying real economic content,
and is the comparison worth running first
(`docs/answers/framework.md` Q7). EXTERNAL's flat 0.30 terms-of-trade sub-weight
assumes the commodity-to-currency transmission is identical for CAD, AUD and NZD,
which is the weak assumption behind item 9's `commodity_link`
(`docs/answers/framework.md` Q9). Item 1's real-rate cancellation is to be ruled
on with this item.

### 3. The POSITIONING boundaries

The joins at `|p| = 1.0` and `|p| = 2.0` and the contrarian slope of 1.5 are
conventions. The shape is right in principle; the constants are guesses.

**Status: narrowed.** Evidence: `docs/answers/scoring-maths.md` Spec 3. Section
3.6 stands as written.

**Constrained by definition rather than by fit.** `p` is a time-series z-score,
so it has unit variance by construction and only the tail shape is an assumption.
Under a normal `p`, a flip at 2.0 puts 4.55% of currency-weeks in the contrarian
branch, about one currency every third run in an eight-name universe. A flip at
1.5 fires on one currency in every single run, and a condition that holds for one
of eight names every week is not what "extreme" means. A flip at 2.5 fires on one
currency every tenth run, which is too rare to justify carrying the branch at
all. The saturation point is not free either, since it is `flip + cap / slope`:
at a slope of 1.5 the cap binds about once per 146 runs, which is what a backstop
should do, at 1.0 it never binds and is decoration, and at 3.0 it binds monthly
and becomes the operating point section 3.6 argues against.

**Genuinely unidentified: the slope.** Over 4,000 synthetic runs and 112,000 pair
observations, moving the slope between 1.0 and 2.5 changes at most 0.17% of
conviction tiers. Deleting the pillar outright changes 4.64%. The model cannot
tell a slope of 1.0 from a slope of 2.5, so the constant is a convention and
naming it as one is more useful than defending it.

**A quantity this document never stated.** `f(p)` emits at a cross-sectional
standard deviation of about 0.5885 under a normal `p`, and 0.6437 on the section
7 fixture, against exactly 1.0 for the five pillars that pass through section
2.3. POSITIONING's declared weight of 0.10 therefore buys roughly 0.059 of
effective influence, and the shape constants are what make it so. That is a
consequence of the exclusion in section 2.2 and 2.3, not an oversight, and it
should not be repaired by re-standardising POSITIONING, which section 2.3 rules
out for a stated reason. Whether 0.10 was ever the intended influence is a
question this document has not answered either way.

**What would settle the constants, and what blocks it.** Bin CFTC `p` against the
subsequent pair return and find where the relationship changes sign. The
contrarian bin holds 4.55% of currency-weeks, so roughly 100 observations in it
needs about 2,200 currency-weeks, around seven years across the six G10
currencies with a liquid contract. COT history runs back to 1986, so this is
blocked on the data layer rather than on trade data: it becomes possible when
`src/fbe/datasources/cot.py` lands, and it needs no journal.

### 4. Cross-sectional versus time-series normalisation

The model normalises cross-sectionally almost everywhere, which means it has no
view when all eight currencies move together. That is intentional, but it also
means a universe-wide hawkish repricing shows up as nothing at all, when in
reality it changes which currencies are worth funding with.

**Status: answered, negative.** Evidence: `docs/answers/scoring-maths.md` Spec 4.
The hybrid was designed and rejected. Section 2.2 stands.

**The blind spot is real and definitional.** Adding a constant to every
currency's input changes every cross-sectional z-score by at most `6.66e-16`. The
shift cancels in the mean, so it cancels in every z and in every pair spread.

**What a time-series leg puts back is not the common move.** Writing the hybrid
as `lambda * z_xs + (1 - lambda) * z_ts`, a common shift of size `d` contributes
exactly

    (1 - lambda) * d * ( 1 / sd_hist(base) - 1 / sd_hist(quote) )

to the pair spread, where `sd_hist` is each currency's own historical standard
deviation. Simulated with per-currency historical volatilities spanning 0.60 for
JPY to 1.35 for NZD, a universe-wide move of 1.75 units moves the pure
time-series AUDJPY spread by 0.7849, which the formula reproduces to four decimal
places, so this is a derivation and not an observation. At `lambda = 0.5` that is
0.3925, which is 52% of the 0.75 neutral band, arriving from an event in which
nothing happened to the relative position of the two currencies. Every large
entry in that table is a yen cross, because the yen has the quietest history in
the set. A hybrid would go systematically long every low-volatility currency
against every high-volatility one whenever the G10 repriced together, at a size
set by the ratio of their historical standard deviations. It would also cost
`lookback_years` of history for every indicator in every pillar for every
currency, which the data layer does not have and which `docs/data-sources.md`
gives no free route to for several series.

**The real question underneath is not a scoring question.** A universe-wide
repricing does change something, and what it changes is not the ordering but how
much ordering there is. That quantity, the cross-sectional dispersion of the raw
inputs before z-scoring, is already computed inside every pillar and then thrown
away. A run in which the eight policy rates span 25 basis points and one in which
they span 400 produce identical z-scores, and only the second is a market where a
relative-value view is worth much. Carrying that dispersion forward as run
metadata would let a report say the G10 rate spread has narrowed and let a reader
discount the whole run. It changes no score. Where it lives is a `types.py`
decision, either a field on `PillarScore` or an entry in `PillarScore.notes`, and
belongs to whoever owns that file. Anyone reopening the hybrid carries the burden
of explaining why the difference in two currencies' historical volatilities is a
fundamental fact about them.

### 5. The eight-currency sample

Eight points is a small sample for a z-score. One outlier moves the mean and the
standard deviation together, which can flip the sign of a mid-ranked currency.
Winsorising the inputs, or using a median and median absolute deviation, would be
more robust.

**Status: answered.** Evidence: `docs/answers/scoring-maths.md` Spec 5. Section
1.3 stands: mean and population standard deviation, no winsorising, no median and
MAD.

**The effect is real and now has a number.** Shift the Swiss franc's entire
MONETARY block by 2 standard deviations of each component's own cross-section, a
plausible Swiss National Bank regime change taking the policy rate from 0.25% to
3.30%. JPY, ranked fourth, moves from **+0.1869 to -0.0117** and changes sign.
Nothing about Japan changed. That perturbation study works from unrounded pillar
scores, so its clean JPY composite reads +0.1869 where section 7.6 publishes
+0.1875 from the rounded ones; the difference is presentational and does not
touch the finding. Two mitigations belong next to that finding. The
ordering of the untouched currencies is preserved by construction on any single
component, because a z-score is an affine map and an affine map does not reorder;
what an outlier destroys is not the ordering but the spacing, compressing the
rest of the cross-section toward each other until the differences are numerically
meaningless. And the pair layer, which is what is traded, moves far less than the
composite layer, because part of the contamination is common to both legs: at
this perturbation none of the 21 pairs containing neither leg of the franc
changes conviction tier and the largest spread move is 0.1257. At 5 standard
deviations, four of them change.

**Median and MAD: rejected.** Over 20,000 clean draws and 20,000 contaminated
ones, it is 1.83 times noisier than mean and standard deviation on clean data,
RMSE 0.8101 against 0.4419, and no more robust in expectation, which is not what
its reputation suggests. The reason is `n = 8`: the median of eight points is the
average of the fourth and fifth order statistics and the MAD is the median of
eight absolute deviations, so both are step functions that jump when a point
crosses an order statistic. Its worst single change across those draws was 47.6,
a run where the MAD collapsed toward zero and took the divisor with it. It is
also the only one of the three rules that changes a conviction tier on a clean
fixture with no outlier in it, and adopting it would invalidate every threshold
in `ScoringConfig`, since a MAD-based z is not on the scale 0.75, 1.50 and 2.50
were chosen for.

**Winsorising: rejected, for a specific reason.** It works as advertised,
removing the contamination completely at the component level and cutting
contamination RMSE by 33% for 23% more estimation noise. But winsorising one
point at each end of an eight-point cross-section replaces the largest value with
the second largest, which on the clean section 7 fixture ties the top two
currencies on **16 of 16 components**. `bias.shortlist` works by pairing the top
of the ranking against the bottom, so this deletes the sharpest information the
model has, in every run, to defend against a rare event. It also mutes the
dislocation itself: the yen's composite under a 5 standard deviation MONETARY
shock reads +0.995 under the current rule and +0.631 under winsorising, so the
one currency that genuinely did something gets understated.

**Recommended instead, not adopted here.** The current rule's problem is not that
it produces a wrong number but that it produces a compressed one silently, and
compression is exactly computable. For population z-scores over `n` names the
squared z-scores sum to `n`, so once the largest absolute z is `m` the remaining
names satisfy `residual RMS = sqrt( (n - m^2) / (n - 1) )`, with no estimation
and no parameter. The section 7 fixture is already in that territory without
anyone having noticed: Japan's real policy rate at `|z| = 2.3481` has compressed
the other seven currencies' real-rate scores to 0.5960 of their natural spread,
and EXTERNAL's terms of trade to 0.6707. Surfacing that diagnostic changes no
score. Where it lives is the same `types.py` decision as item 4, and the level at
which a report should complain about it is a convention rather than a finding;
0.70, a largest absolute z above roughly 2.15, is a reasonable starting point.

Note also that the divisor correction now in section 2.3 helps here. A run-local
divisor re-inflates a cross-section that an outlier has just compressed, so the
historical divisor is measurably less contaminated by a dislocated currency: at a
3 standard deviation CHF shift, 0 tier changes among the untouched pairs against
2 under a run-local divisor.

### 6. The 10-day horizon in the cost filter

`horizon_days = 10` is a stand-in for "the bias horizon". The actual holding
period is the trader's, is shorter, and is variable.

**Status: answered on the structure, blocked on trade data for the value.**
Evidence: `docs/answers/scoring-maths.md` Spec 6. Section 6 stands.

**The filter has one free parameter, not two.**

    cost_ratio = cost / ( atr_20d * sqrt(horizon_days) ) <= max_cost_ratio

rearranges to

    cost / atr_20d <= max_cost_ratio * sqrt(horizon_days) = k

Only `k` is identified. `max_cost_ratio` and `horizon_days` cannot be argued
about separately, because every pair of values with the same product describes
the same filter. At the defaults `k = 0.05 * sqrt(10) = 0.1581`, so a pair with a
62-pip 20-day ATR may cost up to 9.80 pips round trip. The concern recorded in
this item is right in substance and slightly off in size: moving the horizon from
10 to 3 without touching `max_cost_ratio` tightens the filter by
`sqrt(10/3) = 1.83`, not 2, and takes that allowance to 5.37 pips.

**Blocked on trade data.** The measurement is the median holding period, taken
from `opened_at` and `closed_at` on `journal.TradeRecord`, which already carries
both, so no new field is needed. Pinning the mean to plus or minus one day at a
plausible dispersion of three days needs about 35 closed trades:
`(1.96 * 3 / 1)^2 = 35`. There are none. Nothing else in this repository measures
a holding period, and both cost examples in section 7 pass at every horizon in
the plausible range, so the fixture cannot demonstrate a pair changing status
either. Once the hold is measured, set `horizon_days` to it and set
`max_cost_ratio` so that `k` lands where the trader wants it, rather than moving
either alone.

Two further points that do not depend on the measurement. The square-root scaling
assumes a random walk, which understates the move if the bias has any content and
overstates it if the pair mean-reverts, so `k` is not a physical constant even
once the hold is known. And the quantity a trader actually cares about is cost as
a share of the money at risk, `cost / stop_distance`, which needs no horizon at
all. That check belongs at the sizing layer, which sees the stop, and not at the
bias layer, which by design is computed before a chart is looked at, so the ATR
screen stays here as the coarse filter it is.

### 7. No FX-specific carry term

Carry is currently implicit, sitting inside MONETARY as a rate level. Whether it
deserves its own pillar, particularly for the funding currencies, was
unaddressed.

**Status: answered. No carry pillar.** Evidence: `docs/answers/framework.md` Q7.
Section 3.1 stands.

**The overlap is near-total by construction.** An asset's carry is its expected
return assuming its price does not change (Koijen, Moskowitz, Pedersen and Vrugt,
[JFE 2018](https://pages.stern.nyu.edu/~lpederse/papers/Carry.pdf)); in FX that
quantity is the interest rate differential and there is no other input. MONETARY
already scores rate levels cross-sectionally at 0.15 on `policy_rate` and 0.25 on
`yield_2y`, which is 0.40 of the pillar, and in a differenced relative-value
model the cross-sectional z of a rate level is the carry rank up to a monotone
transform. A carry pillar would fail the roadmap's own Phase 3 test, that two
pillars correlated above 0.9 across the cross-section are double-counting, on the
first run.

**Carry as income does not rescue it at this horizon.** At an annualised
differential of 2%, wide for the G10, ten days accrues 0.055%, against a plausible
move of `atr_20d_pips * sqrt(10)`. The trading plan holds hours to a few days, so
the realised figure is smaller again.

**And the carry factor's downside is already the RISK pillar's subject.** The
high-minus-low interest rate slope factor accounts for most of the
cross-sectional variation in average excess returns
([RFS 2011, NBER w14082](https://www.nber.org/papers/w14082)), and its returns
are negatively skewed because of sudden unwinds when risk appetite and funding
liquidity fall ([NBER w14473](https://www.nber.org/papers/w14473)). That unwind is
precisely what section 3.7 describes. A carry pillar would double-count
MONETARY's level terms on the upside and RISK on the downside, and under section
8 constraint 3 a pillar that cannot justify 0.05 should not be created.

**One caveat surfaced by this work, recorded and not applied.** The positive sign
on the rate *level* is regime-dependent in a way the sign on the rate *change* is
not. Bussière, Chinn, Ferrara and Heipertz find the classic rejection of
uncovered interest parity "does not survive into the period during and in the
decade after the financial crisis", where the coefficient on the interest
differential becomes large and positive
([NBER w24342](https://www.nber.org/papers/w24342)), which means high-rate
currencies depreciating. That is an argument for stating the asymmetry in section
3.1's known failure mode, not for removing the level terms, and it is consistent
with the pillar already putting 0.45 of its sub-weight on the two momentum
windows against 0.40 on levels. Section 3.1 is unchanged pending a ruling. Worth
recording alongside it that the carry factor is weakest in exactly this universe:
published work finds including emerging market currencies substantially raises
the carry Sharpe ratio relative to a developed-market-only portfolio.

### 8. Conviction does not distinguish quality from magnitude

A spread of 2.6 built from a single pillar and one built from seven agreeing
pillars both reach HIGH before the agreement test demotes the first. The demotion
ladder handles this, but coarsely.

**Status: narrowed, and the empirical route is effectively closed.** Evidence:
`docs/answers/scoring-maths.md` Spec 8. Section 5.4 stands.

**The ladder's defect is real and now stated precisely.** The demotions act on an
ordinal ladder, so an identical trigger costs wildly different amounts depending
on where the pair started. A pair at `abs(spread) = 2.55` loses about 0.05 of
effective spread to a one-step coverage demotion. A pair at 0.76 loses its
direction entirely. Worth writing down whether or not the rule ever changes.

**The alternative was built and compared.** It introduces no new constants;
every anchor is an existing `ScoringConfig` field:

    m_agreement  = min(1, agreement / min_agreement)      , 1 at and above 0.60
    m_coverage   = min(1, coverage / 0.80)                , 1 at and above 0.80
    m_dispersion = min(1, max_dispersion / dispersion)    , 1 at and below 1.20

    Q = |spread| * m_agreement * m_coverage * m_dispersion

bucketed at the same 0.75, 1.50 and 2.50, with the 24-hour event test still
capping at LOW. It agrees with the ladder on all 28 pairs of the section 7
fixture, and disagrees on 3.05% of 112,000 synthetic pair observations, 85% of
which is a pair just over 0.75 that the ladder demotes to NONE and the continuous
score haircuts but leaves above the line. That is the boundary section 7.7
already calls a rounding difference between two nearly flat currencies. Under a
0.05 input jiggle the continuous form is about 20% more stable, 2.91% against
3.64%, which is not the same as being more correct.

**This will not be settled by measurement at this cadence, and should stop being
described as though Phase 6 will settle it.** Separating a 50% hit rate from a
60% one at 80% power needs about 389 observations per arm. The two schemes
disagree on roughly 0.85 pair observations per run, and over a 10-day bias
horizon consecutive daily runs re-observe the same episode, so six months of
daily running yields on the order of ten independent disagreeing episodes rather
than 111. The gap between ten and 389 is not closable by waiting a little longer.
Computing `Q` and recording it alongside the ladder without letting it decide
anything would cost three lines, make the inconsistency visible on the pairs
where it bites, and build the forward record. It needs a field on `PairBias`,
which is a `types.py` decision.

### 9. Currency metadata is static

`risk_beta`, `inflation_target` and `commodity_link` are hard-coded constants
standing in for relationships that drift. There is no process for reviewing them
and no record of when they were last correct.

**Status: answered, and it splits three ways.** Evidence:
`docs/answers/framework.md` Q9 and `docs/answers/scoring-maths.md` Roadmap 4. All
three fields stay static, for different reasons.

**`inflation_target`: static is correct.** Targets in this universe change on a
decade scale and by announcement, so a change is applied deliberately rather than
detected. The Federal Reserve's 2025 framework review removed the
average-inflation-targeting language and left the 2% longer-run goal untouched,
and the RBNZ remit was amended twice in three years with the 1-3% band and its 2%
midpoint surviving both. The pattern to expect is that the framework around the
target churns and the target does not.

The field has a different problem, larger than drift: it holds a point where
three of the eight do not have one. AUD's 2.5 is the midpoint of a 2-3% band,
NZD's 2.0 the midpoint of 1-3%, and the SNB runs no point target at all but
defines price stability as inflation below 2%, so CHF's 1.0 is a modelling choice
standing in for an asymmetric objective rather than a published number. A 3.1%
print is at the top of the Australian band and outside the New Zealand one, and
section 3.2 cannot tell those two policy problems apart. Whether to add an
optional band field is a separate decision and is not recommended yet, because
nothing would consume it.

**`commodity_link`: static is correct, and the field is not where the error is.**
Dairy was 29.9% of New Zealand's total goods exports in the twelve months to June
2025, and Australia supplied 53.9% of world iron ore exports in 2025. Neither
identity flips inside a review cycle. What does move is the strength of the
transmission from the commodity to the currency, which EXTERNAL currently assumes
is identical across the three by giving each a flat 0.30 sub-weight. That is a
sub-weight question and sits under item 2.

**`risk_beta`: not fine as a constant, and still the right thing to use.** The
failure is recent and specific. In April 2025 the dollar depreciated alongside
equities: the ECB's November 2025 Financial Stability Review records a 12%
depreciation against the euro from January with roughly 7 percentage points of it
after 2 April, and the correlation between US Treasury yields and the dollar
turning negative for a period. Run that through section 3.7. A broad equity index
was deep below its 52-week high and volatility was far above its mean, so `R`
would have been strongly negative, and with `risk_beta(USD) = -0.5` the pillar
would have scored the dollar strongly positive while it was falling. Not a
magnitude error, a sign error, on the most-traded currency in the universe, on
exactly the days the pillar exists to handle. The same instability is documented
for the yen, whose haven behaviour rests on repatriation flows and its funding
role rather than on anything permanent about Japan, and weakens when Japanese
rates rise.

The obvious repair does not work. A trailing regression describes the regime that
just ended: it would have carried -0.5 into April 2025 unchanged and turned the
sign some weeks after the episode it was needed for, converting a wrong constant
into a lagging variable at the cost of a fitted parameter. Simulated against a
drifting true beta, a rolling estimate beats the static constant only once the
true beta's drift standard deviation exceeds roughly 0.25 of the `-1..+1` band,
and even at a drift of 0.35 the best window improves RMSE by 13%. A 60-day
window, the one people reach for because it responds quickly, is the worst
performer at every drift level tested. What a beta error costs is bounded and
regime-dependent: at `R = -0.625` a beta error of 0.30 is 0.075 of pair spread,
10% of the neutral band, and changes 5.25% of conviction tiers over 2,000 draws
on the fixture. In a calm run, where `R` is near zero, it costs nothing by
construction.

**Recommended and not adopted here:** keep the static values; compute a rolling
realised beta and report it next to the static one as a diagnostic that feeds no
score, saying so on the page when the two disagree in sign; add a `last_reviewed`
date to `CurrencyMeta`, which is the record half of this item's complaint; and
review on section 8's cadence, annually or on an announced framework change. Any
rolling beta must be measured against a per-currency index, for example the
geometric mean of that currency's seven crosses, rather than against a dollar
pair: regressing EURUSD on an equity index measures the difference between two
betas, and the dollar has a large one of its own, so a beta estimated off USD
pairs would be confidently and invisibly wrong for every currency at once.
`CurrencyMeta` changes belong to `src/fbe/universe.py`.

### 10. No treatment of intervention or capital controls

The model assumes eight freely floating currencies with central banks that target
inflation. That is a good approximation for G10 and it is still an approximation.

**Status: open, unworked.** None of the four specialist passes took this up. It
stands exactly as written, with no evidence behind it in either direction.

### Findings from the same passes that are not items in this section

Recorded here so they are not lost, and because two of them bear on the body of
this document. None has been applied.

- **A free business-confidence series covering all eight currencies exists**, in
  the OECD Business Tendency Surveys dataflow, monthly for USD, EUR, GBP and CHF
  and quarterly for JPY, CAD, AUD and NZD, which bears on section 3.3's statement
  that PMI is unavailable on a free feed for every G10 country. It is not a
  drop-in: the unit is a percentage balance centred on zero, not a diffusion index
  centred on 50, so it cannot sit under `pmi_composite` without misscoring every
  observation. Whether GROWTH takes it as a separate component is a scoring
  decision that has not been made. Evidence: `docs/answers/data.md` question 5.
- **GDP revisions are large enough to move the cross-sectional ranking.** The
  current cross-sectional standard deviation of `gdp_yoy` across the G10 is 0.636
  percentage points with adjacent currencies as close as 0.004, against a US
  advance-to-latest average revision without regard to sign of 1.2 percentage
  points on the quarterly annualised rate. Live scoring should use the latest
  vintage, which is what the registry does; first-print data is for a backtest,
  which is what `Observation.released_at` and `revision` exist for. Evidence:
  `docs/answers/data.md` question 8.
- **A free forward curve exists for four of the eight currencies**, EUR, GBP, AUD
  and USD, fetched live, and does not exist free for CAD, JPY, CHF and NZD. If a
  separate `policy_path` sub-indicator inside MONETARY is ever wanted, that is the
  coverage it would start from, and whether a 4-of-8 indicator is worth adding
  under `MIN_COMPONENT_WEIGHT` is a scoring decision, not a data one. Evidence:
  `docs/answers/data.md` question 3.
- **Section 7 should not be handed to a reasoning layer as context.** It exists so
  a human can check the arithmetic by hand. Evidence:
  `docs/answers/product.md`, reasoning-layer 1.
