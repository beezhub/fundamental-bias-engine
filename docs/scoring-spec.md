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
    z_pillar(c) = ( blend(c) - mean(blend) ) / sd(blend)
    score(c)    = clip(z_pillar(c), score_clip)

where `u_j'` is `u_j` renormalised over the sub-indicators actually available for
currency `c`, and `mean` and `sd` are taken across the currencies that have this
pillar at all.

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
the same scale. The re-standardisation pass puts each pillar's cross-sectional
standard deviation at exactly 1.0 before weighting, so the declared weights are
the operative ones.

Section 7.1 shows the size of the effect on real numbers: the blended MONETARY
column has a standard deviation of 0.7001 and is scaled up by a factor of 1.43,
while the two-component INFLATION column has a standard deviation of 0.9711 and
is barely touched at 1.03. Without the pass, MONETARY would have spoken roughly
39% more quietly than INFLATION relative to their declared weights.

Two implementation notes. When every currency has full sub-indicator coverage the
blend's mean is exactly 0 by construction, since each `z_j` has mean 0 and the
sub-weights sum to 1, so the pass reduces to a division by `sd(blend)`. The mean
is only non-zero when per-currency sub-weight renormalisation differs across
currencies, so subtract it anyway rather than relying on the special case. And
`sd(blend) < 1e-9` is handled exactly as in section 1.3: every score becomes 0.

The pass applies only to the five cross-sectionally normalised pillars.
POSITIONING and RISK are excluded for the reasons in section 2.2: both are
already constructed on the score band in units that mean something, and
re-standardising them would destroy that meaning. In particular it would force
POSITIONING to have a non-zero spread across currencies even in a run where
nothing is crowded, which is the opposite of what that pillar is for.

**Minimum available sub-weight.** A currency must hold at least half of a
pillar's total sub-weight for that pillar to be scored:

    MIN_COMPONENT_WEIGHT = 0.5
    if sum of u_j over available j  <  MIN_COMPONENT_WEIGHT:
        the pillar is absent for that currency

Renormalising is a reasonable repair for one missing series out of four. It is
not a reasonable repair for three missing out of four, where it stops being a
repair and becomes an assertion that the one surviving series speaks for the
whole pillar. Below the floor the honest output is absence, which flows into
coverage (section 4.2) and is visible in the report, rather than a confident
number resting on a fragment. If a currency has no sub-indicator at all for a
pillar, the pillar is likewise absent and contributes nothing to coverage.

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

**Sources.** FRED for US series and for those non-US yields it carries; a manual
CSV under `data/manual` for the remainder, refreshed weekly. The registry of
indicator keys to source series is owned by `docs/data-sources.md`.

**Known failure mode.** The pillar is blind to the difference between a yield
rising because policy is expected to tighten and a yield rising because the
market is demanding a risk premium on that country's debt. In a fiscal or
credibility event the two look identical in the data and mean opposite things for
the currency. It is also blind to the level effect at the extremes: a move from
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
the sub-weight is renormalised across the remaining three, per section 2.3, and
coverage is unaffected because the pillar still has data. The engine must not
substitute a proxy silently.

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

Let `s` be `PillarScore.staleness_days` (age of the newest input the pillar used),
`S = ScoringConfig.max_staleness_days` (default 45), and `s0 = S / 3` (15 days).

    phi = 1.0                    for s <= s0
    phi = (S - s) / (S - s0)     for s0 < s <= S
    phi = 0.0                    for s > S

Fresh data counts fully. Between 15 and 45 days a pillar's weight decays linearly
to zero. Past 45 days it stops counting, which is what the `max_staleness_days`
docstring means by "stop counting toward pillar coverage". The decay is gradual
rather than a cliff so that a quarterly series does not cause the composite to
jump the morning it crosses a threshold.

### 4.2 Effective weights and coverage

    w_eff(p) = weight(p) * phi(p)          , and 0.0 if the pillar has no data
    coverage = sum over p of w_eff(p)

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

    d(p)        = score(base, p) - score(quote, p)
    w_pair(p)   = ( w_eff(base, p) + w_eff(quote, p) ) / 2
    considered  = { p : |d(p)| > 1e-9 }
    agreeing    = { p in considered : sign(d(p)) == sign(spread) }

    agreement = sum of w_pair over agreeing / sum of w_pair over considered

Pillars where the two legs score identically are excluded from both numerator and
denominator: they express no opinion on this pair and should neither support nor
oppose it. If no pillar is considered, agreement is 0.0. Stored on
`PairBias.agreement`.

Weighting by `w_pair` rather than counting pillars is deliberate. MONETARY at 0.30
disagreeing is a materially worse sign than POSITIONING at 0.10 disagreeing, and
a headcount would treat them as equal.

### 5.4 Conviction

A base tier from the size of the spread, then demotions. Never a promotion.

**Base tier:**

| `abs(spread)` | Base `Conviction` |
| --- | --- |
| `< 0.75` (`min_spread_low`) | `NONE` |
| `0.75 <= x < 1.50` (`min_spread_medium`) | `LOW` |
| `1.50 <= x < 2.50` (`min_spread_high`) | `MEDIUM` |
| `>= 2.50` | `HIGH` |

**Demotions**, applied in order down the ladder `HIGH -> MEDIUM -> LOW -> NONE`:

| Test | Effect | Reason |
| --- | --- | --- |
| `agreement < min_agreement` (0.60) | Cap at `LOW` | One pillar is carrying the whole spread |
| `min(coverage_base, coverage_quote) < 0.80` | Demote one step | The view rests on partial data |
| `max(dispersion_base, dispersion_quote) > 1.20` | Demote one step | A leg's own pillars contradict each other |
| A high-impact `CalendarEvent` for either leg within the next 24 hours | Cap at `LOW` | The rate path could be repriced before the trade matures |

Demotions compound. A pair with weak agreement, thin coverage and a central bank
meeting due can fall from `HIGH` to `NONE`.

If the final conviction is `NONE`, `Direction` is forced to `NEUTRAL` regardless
of the spread. The two fields must never disagree.

The dispersion threshold of 1.20 is a judgement, not a fitted value. On the score
band, a weighted dispersion above 1.2 means the pillars are typically more than a
full band unit apart from the composite, which in practice means at least one
pillar is arguing hard in the opposite direction.

## 6. Hard filters

These set `PairBias.tradeable = False` and append a string to
`PairBias.blockers`. They are hard: a `HIGH` conviction pair that trips one does
not reach the shortlist. Conviction is a statement about the model's belief;
tradeability is a statement about whether the trade can be executed sensibly, and
the two are kept separate so a blocked pair still shows its reasoning.

| Blocker | Test | Rationale |
| --- | --- | --- |
| `cost` | `cost_ratio > 0.05` | Dealing cost eats too much of the plausible move |
| `event` | The intended execution time falls inside a blackout window for either leg | The plan says do not trade around high-impact news |
| `coverage` | `min(coverage_base, coverage_quote) < 0.60` | Under 60% of pillar weight, the composite is a guess |
| `no_coverage` | Either leg has `coverage == 0.0` | No composite exists |

**Cost ratio.** Compare the round-trip dealing cost against the move the pair can
plausibly produce over the bias horizon:

    expected_move_pips = atr_20d_pips * sqrt(horizon_days)      , horizon_days = 10
    cost_ratio = (typical_spread_pips + commission_pips) / expected_move_pips

The square-root scaling is the standard random-walk approximation, which is
adequate here because the filter only needs to separate viable pairs from
obviously uneconomic ones. A ratio above 0.05 means more than 5% of the expected
move is paid to the broker before the position starts, which on a R2000 account
with 1-2% risk is decisive. This filter is why exotic-ish G10 crosses will
usually fail even when the score spread is wide, and it is the mechanism by which
the plan's "low spreads and trading costs" rule enters the model.

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

Now the re-standardisation pass of section 2.3. Every currency has all five
sub-indicators, so the blended column has a mean of exactly 0 and the pass
reduces to a division by its standard deviation:

    sd(blend) = 0.7001
    USD: +1.13595 / 0.7001 = +1.6227  ->  published as +1.62

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
data is 30 days old, so for that pillar `phi = (45 - 30) / (45 - 15) = 0.500` and
`w_eff = 0.10 * 0.500 = 0.050`:

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
- Every pillar's `z` has cross-sectional mean 0 and population standard deviation
  1 after the re-standardisation pass of section 2.3, for the five pillars that
  use it, and the pass is **not** applied to POSITIONING or RISK.
- A pillar is absent for a currency holding less than `MIN_COMPONENT_WEIGHT`
  (0.5) of that pillar's sub-weight, and that absence lowers coverage rather than
  producing a score.
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

## 10. Open questions

Listed honestly. None of these is settled, and none should be silently resolved
in code without updating this section.

1. **Inflation credibility.** Section 3.2 assumes above-target inflation is
   currency-positive through the policy channel. When a central bank is credibly
   ignoring an overshoot the sign inverts, and the model has no switch for this.
   The idea behind any fix is to let the rate market decide whether the bank is
   expected to respond, rather than assuming it will.

   **Candidate: a front-end response gate.** Define a gate from the same
   3-month change in the 2-year yield that MONETARY already consumes:

       g = clip( yield_2y_chg_3m / 0.10pp , 1.0 )       , so g is in -1 .. +1

   A **positive** inflation gap is then scored `gap * g`, and a **negative** gap
   is left ungated and stays currency-negative. In words: inflation above target
   only helps the currency to the extent the front end is actually repricing
   toward a response, and if the front end is repricing the other way the same
   overshoot counts against the currency. Inflation below target is
   currency-negative either way, since a bank undershooting its target has no
   reason to tighten regardless of what the curve is doing. The rule pivots
   continuously at `gap = 0`, where both branches give 0, so there is no jump at
   the target.

   The gate is attractive because it answers the question with a series already
   in the model rather than a new data dependency, and because it degrades
   gracefully: at `g = 0`, a flat front end, inflation simply stops contributing
   instead of contributing the wrong sign.

   **It is not in the pillar.** Section 3.2 stands as written, with headline at
   0.40 and core at 0.60 and no gate. Two reasons. It adds a conditional, and
   conditionals are exactly what the methodology's section on separating
   fundamentals from technicals warns about: `0.10pp` is a free parameter, and
   the pivot rule is a second one. And the real policy rate term already inside
   MONETARY captures part of the same effect, since a tolerated overshoot shows
   up as a falling real rate, so the gate would partly double-count something the
   model already sees. Recorded here as the leading candidate, to be taken up
   with the sub-weight question in item 2 rather than on its own.

2. **Sub-weights are undefended.** The seven pillar weights are argued for. The
   sub-weights inside each pillar (0.15 / 0.25 / 0.20 / 0.25 / 0.15 in MONETARY,
   0.40 / 0.60 in INFLATION, and so on) are reasonable but essentially asserted.
   Equal weighting within each pillar is a defensible alternative that would
   remove eleven free parameters, and it has not been tested against the current
   scheme.

3. **The POSITIONING boundaries.** The joins at `|p| = 1.0` and `|p| = 2.0` and
   the contrarian slope of 1.5 are conventions. The shape is right in principle;
   the constants are guesses.

4. **Cross-sectional versus time-series normalisation.** The model normalises
   cross-sectionally almost everywhere, which means it has no view when all eight
   currencies move together. That is intentional, but it also means a
   universe-wide hawkish repricing shows up as nothing at all, when in reality it
   changes which currencies are worth funding with. A hybrid, blending a
   cross-sectional and a time-series z, has not been designed.

5. **The eight-currency sample.** Eight points is a small sample for a z-score.
   One outlier moves the mean and the standard deviation together, which can flip
   the sign of a mid-ranked currency. Winsorising the inputs before normalising,
   or using a median and median absolute deviation instead of mean and standard
   deviation, would be more robust and less familiar. Not decided.

6. **The 10-day horizon in the cost filter.** `horizon_days = 10` is a stand-in
   for "the bias horizon". The actual holding period is the trader's, is shorter,
   and is variable. If the true average hold is three days, the filter is roughly
   twice as permissive as it should be.

7. **No FX-specific carry term.** Carry is currently implicit, sitting inside
   MONETARY as a rate level. Whether it deserves its own pillar, particularly for
   the funding currencies, is unaddressed. It would overlap heavily with what is
   already there.

8. **Conviction does not distinguish quality from magnitude.** A spread of 2.6
   built from a single pillar and one built from seven agreeing pillars both
   reach HIGH before the agreement test demotes the first. The demotion ladder
   handles this, but coarsely: a continuous conviction score that was then
   bucketed might be better than a bucket that is then demoted.

9. **Currency metadata is static.** `risk_beta`, `inflation_target` and
   `commodity_link` are hard-coded constants standing in for relationships that
   drift. There is no process for reviewing them and no record of when they were
   last correct.

10. **No treatment of intervention or capital controls.** The model assumes eight
    freely floating currencies with central banks that target inflation. That is
    a good approximation for G10 and it is still an approximation.
