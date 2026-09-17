# Answers: scoring, normalisation and aggregation

Proposals against the open questions in `docs/scoring-spec.md` section 10 and
`docs/roadmap.md` that fall to the scoring, normalisation and aggregation layer.

**This file resolves nothing.** Section 10 says none of these may be silently
settled in code, so nothing here has been applied to the spec, to
`ScoringConfig`, or to any module. Each section states what would change if the
recommendation is accepted, and stops there.

**Nothing here measures whether the model predicts anything.** Every number
below is a sensitivity measurement: how much the model's own output moves when
one of its own choices moves. None of it is a result about returns. That
question is Phase 6's and it is untouched.

## Verdicts

| # | Question | Verdict | Recommendation |
| --- | --- | --- | --- |
| Spec 2 | Sub-weights against equal weights | ANSWERED for the measurable part, BLOCKED on which is better | Keep the current sub-weights, record the bound |
| Spec 3 | POSITIONING joins, slope, cap | NARROWED | Keep the joins, declare the slope unidentified, document the pillar's true loudness |
| Spec 4 / Roadmap 1 | Cross-sectional against time-series | ANSWERED, negative | No hybrid. Expose raw cross-sectional dispersion as run metadata instead |
| Spec 5 | The eight-currency sample | ANSWERED | Keep mean and standard deviation. Reject median and MAD. Reject winsorising. Add a residual-dispersion diagnostic |
| Spec 6 | `horizon_days = 10` | ANSWERED for the structure, BLOCKED on the value | State the single invariant `k`. Measure the hold from the journal |
| Spec 8 | Conviction, quality against magnitude | NARROWED, empirical route effectively closed | Keep the ladder. Record the quality-adjusted spread alongside it |
| Roadmap 4 | Rolling or static `risk_beta` | ANSWERED, conditional | Keep static. Document the error band. Annual review |
| Roadmap 6 | Does POSITIONING earn 0.10 with the COT lag | NARROWED on the lag, BLOCKED on the earning | Measure weekly persistence when `cot.py` lands. The shape function costs more than the lag does |

## Method

Two engines produced every number below.

**The fixture.** Section 7 of `docs/scoring-spec.md` was reimplemented from its
raw inputs, in plain Python, following sections 1.3, 2.2, 2.3, 4 and 5. It
reproduces the published table exactly: all 35 cross-sectionally normalised
pillar scores, the POSITIONING and RISK columns, all eight composites
(USD `+0.9420`, JPY `+0.1875`, NZD `-0.9774`), NZD's coverage of `0.950` and
dispersion of `0.7056`, all eight ranks, and the four pair conclusions
(NZDUSD short MEDIUM at spread `-1.9194` and agreement `0.8974`, USDJPY long LOW
at `+0.7545` and `0.7000`, NZDJPY short LOW at `-1.1649`, EURCAD NONE at
`+0.1635`). Because it reproduces the fixture it can be perturbed and trusted.
The fixture is one cross-section, so it is used for existence claims and worked
examples, never for frequencies.

**Synthetic cross-sections.** For frequency claims, universes were drawn with
each pillar's components generated from an equicorrelation model whose
correlation is set to that pillar's mean pairwise component correlation measured
on the fixture: MONETARY `+0.354`, INFLATION `+0.881`, GROWTH `+0.695`,
EMPLOYMENT `+0.428`, EXTERNAL `+0.382`. Components are standard normal, then
put through the same z-score, blend, re-standardise, clip, aggregate and
difference path. Seeds are fixed. These draws are not market data and carry no
claim about how G10 fundamentals are actually distributed. They establish how
sensitive the machinery is to its own parameters, which is the only thing they
are used for.

Scratch scripts were kept outside the repository. Anything below can be rebuilt
from section 7's raw input tables and the generating model in the paragraph
above.

Sign convention throughout, unchanged: positive means currency-strengthening.

---

## Spec 2. Sub-weights are undefended

> Does equal weighting inside each pillar change the ranking on the section 7
> fixture, and is the current scheme distinguishable from equal weights on eight
> data points?

**Verdict: ANSWERED for what was asked. BLOCKED on which scheme is better.**

### On the fixture

Replacing the eleven sub-weights with equal weights inside each pillar:

| Currency | Current | Equal | Difference | Rank, current | Rank, equal |
| --- | --- | --- | --- | --- | --- |
| USD | +0.9420 | +0.9465 | +0.0045 | 1 | 1 |
| AUD | +0.6925 | +0.7170 | +0.0245 | 2 | 2 |
| GBP | +0.4810 | +0.4735 | -0.0075 | 3 | 3 |
| JPY | +0.1875 | +0.1130 | -0.0745 | 4 | 4 |
| EUR | -0.3245 | -0.3260 | -0.0015 | 5 | 5 |
| CAD | -0.4880 | -0.4670 | +0.0210 | 6 | 6 |
| CHF | -0.5450 | -0.5520 | -0.0070 | 7 | 7 |
| NZD | -0.9774 | -0.9332 | +0.0442 | 8 | 8 |

No rank changes. Across the 28 pairs: no conviction tier changes, no direction
changes, largest spread move `0.1187`, mean spread move `0.0386`. The 28 pair
spreads in this run have a standard deviation of `0.9538`, so the whole effect of
eleven parameters is 4.0% of the signal's own scale, and every pair stays in the
band it was in.

Per pillar, the blend under the two schemes correlates at:

| Pillar | Sub-indicators | Mean pairwise component correlation | Blend correlation, current against equal | Largest score change |
| --- | --- | --- | --- | --- |
| MONETARY | 5 | +0.354 | 0.99265 | 0.300 |
| INFLATION | 2 | +0.881 | 0.99874 | 0.090 |
| GROWTH | 4 | +0.695 | 0.99879 | 0.090 |
| EMPLOYMENT | 2 | +0.428 | 1.00000 | 0.000 |
| EXTERNAL | 3 | +0.382 | 0.99611 | 0.130 |

The mechanism is in the third column. Sub-weights can only matter to the extent
that a pillar's components disagree, and inside these pillars they mostly do
not. EMPLOYMENT is the limiting case: two components at 0.50 each are already
equal weights, so the change is exactly zero.

### Across many runs

5,000 synthetic runs, 140,000 pair observations:

| Change | Conviction tier changes | LONG to SHORT | To or from NEUTRAL |
| --- | --- | --- | --- |
| Equal sub-weights inside each pillar | 3.42% | 0.00% | 3.17% |
| MONETARY 0.30 to 0.35, EXTERNAL 0.10 to 0.05 | 6.07% | 0.00% | 5.41% |

Three readings. The eleven sub-weights together are worth about half of one
0.05 move in a single pillar weight. Not one pair observation in 140,000 had its
direction reversed, so a sub-weight change cannot flip the model's mind about a
pair, only about whether it has one. And section 8's own sensitivity bar is that
more than roughly 20% of pair-weeks changing tier means the model is more
sensitive to weights than to data. At 3.42% the sub-weights are nowhere near it.

### Is the current scheme distinguishable from equal weights?

No, and it cannot be made so by the current data. Two constructions whose blends
correlate between 0.993 and 1.000 across an eight-name cross-section are the
same construction as far as any test on that cross-section is concerned. Out of
sample they would need to be told apart on a mean pair-spread difference of
`0.0386` against an unmeasured error, and the two schemes never disagree on
direction, so a hit-rate comparison has nothing to count.

### Recommendation

**Keep the current sub-weights.** Not because they are right, but because
changing them buys nothing measurable and costs the stated economic reasons in
section 3, several of which are real mechanisms rather than fitted numbers: core
inflation carries more than headline because central banks act on core; the
2-year yield carries more than the policy rate because it prices the path. Equal
weighting would delete those statements to remove parameters that the evidence
above shows are not doing enough to be dangerous.

The argument for equal weighting is that eleven undefended parameters are eleven
places a future curve fit could hide. That argument is sound in general and weak
here, because nothing in this repository has ever been fitted to anything, so
the parameters carry no fitted content to remove.

If accepted, the change to `docs/scoring-spec.md` is additive and confined to
section 10 item 2:

- Record that the scheme was tested against equal weights and that the two
  agree to within `0.1187` on any pair spread and `0.0386` on average, on the
  section 7 fixture, with no rank, tier or direction change.
- Record that the choice is not empirically separable at eight names, so the
  sub-weights are not a productive place to spend effort, and a future proposal
  to change them needs an economic mechanism, not a result.

One test obligation worth adding to section 9: assert that swapping every
pillar's sub-weights for equal weights moves no pair spread on the section 7
fixture by more than `0.15`. That turns the bound into something CI protects, so
a future sub-weight edit that quietly matters gets caught.

---

## Spec 3. The POSITIONING boundaries

> Can anything constrain the joins at `|p| = 1.0` and `|p| = 2.0` and the
> contrarian slope of 1.5, or are they conventions?

**Verdict: NARROWED.** Three of the four constants are constrained, one by
arithmetic and two weakly by what the words are supposed to mean. The slope is
unidentified and should be labelled as such. A fifth quantity nobody has written
down is more consequential than any of them.

### What is not free

Given the design commitments already in section 3.6, the shape function has
fewer degrees of freedom than it looks. Oddness removes any long or short
asymmetry. Continuity at both joins plus a peak magnitude of 1.0 at the first
join plus a zero crossing at the second fixes the fading branch entirely once
the two joins are chosen. And the saturation point is not free either: it is
`|p| = flip + cap / slope`, so choosing the cap at 2.0 and the slope at 1.5 is
the same act as choosing saturation at 3.33.

### What the joins have to mean

`p` is a time-series z-score, so it has unit variance by construction. Only the
tail shape is an assumption. Under a normal `p`:

| Peak | Flip | Confirming | Fading | Contrarian | Contrarian currencies per 8-name run |
| --- | --- | --- | --- | --- | --- |
| 1.00 | 2.00 | 68.3% | 27.2% | 4.55% | 0.36 |
| 1.00 | 1.50 | 68.3% | 18.4% | 13.36% | 1.07 |
| 0.75 | 1.50 | 54.7% | 32.0% | 13.36% | 1.07 |
| 1.00 | 2.50 | 68.3% | 30.5% | 1.24% | 0.10 |
| 1.25 | 2.50 | 78.9% | 19.9% | 1.24% | 0.10 |

This does constrain the flip point, and the constraint is a definition rather
than a fit. The contrarian branch is meant to fire when positioning is extreme.
At a flip of 1.5 it fires on one currency in the universe in every single run,
and a condition that holds for one of eight names every week is not extreme. At
2.5 it fires on one currency every tenth run, which is too rare to justify
carrying the branch at all. The current 2.0, at roughly one currency every third
run, is the only setting in that table that matches the word.

The saturation point is constrained the same way:

| Slope | Saturation at | Probability | Frequency across 8 names |
| --- | --- | --- | --- |
| 1.0 | 4.000 | 0.0063% | once per 1,973 runs |
| 1.5 | 3.333 | 0.0858% | once per 146 runs |
| 2.0 | 3.000 | 0.2700% | once per 46 runs |
| 3.0 | 2.667 | 0.7661% | once per 16 runs |

At 1.5 the cap binds about twice a year on daily runs, which is what a backstop
should do. At 1.0 it never binds and is decoration. At 3.0 it binds monthly and
has become an operating point, which section 3.6 explicitly argues against.

### What is genuinely unidentified

Downstream the slope barely exists. 4,000 synthetic runs, 112,000 pair
observations, measuring conviction tier changes against the current constants:

| Variant | Tier changes | Direction changes |
| --- | --- | --- |
| Flip 2.0 to 1.5 | 1.54% | 1.40% |
| Flip 2.0 to 2.5 | 0.71% | 0.66% |
| Slope 1.5 to 1.0 | 0.10% | 0.10% |
| Slope 1.5 to 2.5 | 0.17% | 0.16% |
| Peak 1.0 to 0.75 with flip 1.5 | 2.40% | 2.20% |
| Pillar switched off entirely | 4.64% | 4.24% |

The model cannot tell a slope of 1.0 from a slope of 2.5. One tier decision in a
thousand turns on it. Deleting the pillar outright changes 4.64%, so every
constant inside it is arguing over a fraction of that.

### The quantity nobody wrote down

Section 2.3 forces the five cross-sectionally normalised pillars to a
cross-sectional standard deviation of exactly 1.0 so that the declared weights
are the operative ones. POSITIONING is excluded, for the good reason in section
2.2. The consequence has not been stated: the shape function's own output has a
standard deviation of `0.5885` under a normal `p`, confirmed at `0.5887` by two
million draws, with `E|f(p)| = 0.5067`. On the section 7 fixture the POSITIONING
column has a standard deviation of `0.6437` against 1.0 for every re-standardised
pillar.

So POSITIONING's declared weight of 0.10 buys roughly `0.10 x 0.5885 = 0.0588` of
effective influence. The pillar is 41% quieter than its weight says, and the
shape constants are what makes it so. Widening the confirming region to a peak
of 1.5 would raise the standard deviation to `0.7634` and the effective weight to
`0.0763`; moving the peak to 0.75 with a flip at 1.5 would lower it to `0.5102`
and `0.0510`, close to the 0.05 floor section 8 sets for a pillar's existence.
That is the strongest constraint available on these constants and it is
internal, not empirical: whatever they are set to, they must leave the pillar
above the floor it is nominally well clear of.

### Recommendation

1. **Keep the joins at 1.0 and 2.0.** Defend the flip point on the occupancy
   table, not on intuition. One currency in eight crossing into the contrarian
   branch every third run is the reading the word "extreme" supports.
2. **Keep the slope at 1.5 and record that it is unidentified.** State in
   section 3.6 that varying it between 1.0 and 2.5 changes at most 0.17% of
   conviction tiers, so the constant is a convention and no future work should
   spend time on it. Naming a parameter as unidentified is more useful than
   defending it.
3. **State the effective weight.** Add to section 3.6 that the shape function
   emits at a cross-sectional standard deviation near 0.59, so POSITIONING
   contributes roughly 0.059 rather than 0.10, and that this is a consequence of
   the exclusion from the re-standardisation pass rather than an oversight. This
   is a documentation change only. It should not be fixed by re-standardising
   POSITIONING, which section 2.3 already rules out for a good reason.

**What would settle the constants empirically.** Bin CFTC `p` against the
subsequent pair return and find where the relationship changes sign. The
contrarian bin holds 4.55% of currency-weeks, so roughly 100 observations in
that bin needs about 2,200 currency-weeks, which across the six G10 currencies
with a liquid contract is around 370 weeks, close to seven years. COT history
runs back to 1986, so this is available once `datasources/cot.py` lands. It is
not blocked on the journal, only on the data layer.

---

## Spec 4 and Roadmap 1. Cross-sectional against time-series normalisation

> Design the hybrid or explain why it should not exist.

**Verdict: ANSWERED, negative.** It should not exist in the score. The
motivation is real, the proposed cure does not address it, and what a hybrid
would actually inject is a directional signal built from the difference in the
two legs' historical volatility.

### The blind spot is exactly as described

Adding 1.75 to every currency's input in the simulated universe changes every
cross-sectional z-score by at most `6.66e-16`. The shift cancels in the mean, so
it cancels in every z and in every pair spread. Universe-wide moves are
invisible by construction, and that is not a bug in the implementation, it is
the definition of the transform.

### What the time-series leg puts back is not the common move

Write the hybrid as `lambda * z_xs + (1 - lambda) * z_ts`. In a pair spread the
cross-sectional part behaves as above. The time-series part does not cancel, and
the algebra says exactly why: a common shift of size `d` contributes

    (1 - lambda) * d * ( 1 / sd_hist(base) - 1 / sd_hist(quote) )

to the pair spread, where `sd_hist` is each currency's own historical standard
deviation. Simulated with per-currency historical volatilities spanning JPY at
0.60 and CHF at 0.65 up to AUD at 1.25 and NZD at 1.35, a universe-wide move of
1.75 units moves the pure time-series pair spread by:

| Pair | Change in the time-series spread | Predicted from the formula |
| --- | --- | --- |
| AUDJPY | 0.7849 | 0.7849 |
| NZDJPY | 0.7250 | 0.7250 |
| USDJPY | 0.7024 | 0.7024 |
| GBPJPY | 0.6930 | 0.6930 |
| EURJPY | 0.6347 | 0.6347 |

The formula reproduces the simulation to four decimal places, so this is a
derivation rather than an observation. At `lambda = 0.5` the AUDJPY figure is
`0.3925`, which is 52% of the `min_spread_low` neutral band of 0.75, arriving
from an event in which nothing at all happened to the relative position of the
Australian dollar and the yen.

Read the pattern in that table. Every large entry is a yen cross, because the
yen has the quietest history in the simulated set. A hybrid would go
systematically long every low-volatility currency against every high-volatility
one whenever the G10 repriced together, at a size set by nothing more than the
ratio of their historical standard deviations. That is a normalisation artefact
presented as a trade.

### How much else a hybrid changes

Simulated over 130 runs and 28 pairs, hybrid pair spreads against pure
cross-sectional ones:

| Lambda | Correlation with cross-sectional | Sign agreement | Mean absolute difference |
| --- | --- | --- | --- |
| 1.00 | 1.0000 | 100.0% | 0.0000 |
| 0.75 | 0.9920 | 96.3% | 0.2721 |
| 0.50 | 0.9497 | 90.7% | 0.5441 |
| 0.25 | 0.8198 | 80.7% | 0.8162 |
| 0.00 | 0.5334 | 65.1% | 1.0883 |

A hybrid agrees with the model it replaces most of the time and disagrees
precisely in the episodes the previous section shows it handles badly. It buys
nothing anywhere else, and it would cost a hard data dependency: `lookback_years`
of history for every indicator in every pillar for every currency, which the
data layer does not have and which `docs/data-sources.md` gives no free route to
for several series.

### There is a real question underneath, and it is not a scoring question

The motivation in section 10 item 4 is right that a universe-wide repricing
changes something. What it changes is not the ordering, it is how much ordering
there is. That quantity is already computed inside every pillar and then thrown
away: the cross-sectional dispersion of the raw inputs before z-scoring. In the
simulated history it moves from 2.170 to 1.501 across four sampled runs while
the level mean falls from 7.569 to 2.623. A run in which the eight policy rates
span 25 basis points and one in which they span 400 produce identical z-scores,
and only the second is a market where a relative-value view is worth much.

### Recommendation

1. **No hybrid.** Record in section 10 item 4 and roadmap open question 1 that
   the hybrid was designed and rejected, with the reason stated as the formula
   above rather than as a preference: the only thing a time-series leg
   contributes to a pair spread on a universe-wide move is
   `d * (1/sd_hist(base) - 1/sd_hist(quote))`, which ranks currencies by the
   quietness of their own history.
2. **Expose the dispersion instead.** For each pillar and each sub-indicator,
   carry the raw cross-section's standard deviation and range forward as run
   metadata, so that a report can say the G10 rate spread has narrowed and a
   reader can discount the whole run accordingly. This needs a field on
   `PillarScore` or an entry in `PillarScore.notes`, which is a `types.py`
   decision and belongs to whoever owns that file. It changes no score.
3. If anyone revisits this, the burden is to explain why the difference in two
   currencies' historical volatilities is a fundamental fact about them.

---

## Spec 5. The eight-currency sample

> One outlier moves the mean and standard deviation together and can flip the
> sign of a mid-ranked currency. Test it, then test median and MAD, and
> winsorising, against the same perturbation.

**Verdict: ANSWERED.** The effect is real and the section 10 description of it is
accurate. Both proposed cures are worse than the disease. A third option, which
costs nothing and changes no score, is better than either.

### The effect is real, and here it is

Take the section 7 fixture and shift the Swiss franc's entire MONETARY block by
2 standard deviations of each component's own cross-section. That is a plausible
Swiss National Bank regime change: the policy rate goes from 0.25% to 3.30%, the
2-year yield from 0.30% to 3.02%, the 3-month yield change from -8bp to +45bp.

| Currency | Clean composite | After the shift | Change |
| --- | --- | --- | --- |
| USD | +0.9432 | +0.8643 | -0.0789 |
| AUD | +0.6923 | +0.6193 | -0.0730 |
| GBP | +0.4825 | +0.4090 | -0.0735 |
| **JPY** | **+0.1869** | **-0.0117** | **-0.1986** |
| EUR | -0.3239 | -0.4422 | -0.1183 |
| CAD | -0.4868 | -0.5779 | -0.0911 |
| NZD | -0.9807 | -1.0785 | -0.0978 |

The yen, ranked fourth, changes sign. Nothing about Japan changed. That is
exactly the failure section 10 item 5 describes, now with a number on it.

Two mitigating facts matter as much as the effect. The ordering of the seven
untouched currencies is unchanged, and it is preserved by construction on any
single component, because a z-score is an affine map and an affine map does not
reorder. And the pair layer, which is what is traded, is much less affected than
the composite layer: at this perturbation, zero of the 21 pairs that contain
neither leg of the franc change conviction tier, with the largest spread move at
`0.1257`.

Pushed harder, the pair layer does move:

| Perturbed currency | Shift | Largest spread move, 21 untouched pairs | Tier changes, 21 |
| --- | --- | --- | --- |
| CHF | 2 sd | 0.1257 | 0 |
| CHF | 3 sd | 0.1548 | 2 |
| CHF | 5 sd | 0.3865 | 4 |
| JPY | 3 sd | 0.2862 | 2 |
| AUD | 3 sd | 0.4521 | 4 |
| AUD | 5 sd | 0.5902 | 5 |

### What an outlier destroys

The clearest statement of the damage is at the component level. Push the dollar's
2-year yield away from the pack and watch the spread of z-scores across the
seven currencies that were not touched:

| Perturbation | mean and sd | median and MAD | Winsorise one each end |
| --- | --- | --- | --- |
| 0 (clean) | 2.7608 | 1.9089 | 2.5751 |
| 1 sd | 2.3455 | 1.9089 | 2.5751 |
| 2 sd | 1.9572 | 1.9089 | 2.5751 |
| 5 sd | 1.2207 | 1.9089 | 2.5751 |
| 10 sd | 0.7201 | 1.9089 | 2.5751 |
| 100 sd | 0.0824 | 1.9089 | 2.5751 |

In the limit every untouched currency converges to `-1 / sqrt(7) = -0.3780` and
the outlier to `+sqrt(7) = +2.6458`. The seven do not get reordered, they get
compressed until the differences between them are numerically meaningless. That
is the mechanism, and describing it as a sign flip understates it.

### Median and MAD: reject

Tested over 20,000 clean draws from a standard normal and 20,000 draws with one
currency shifted 4 standard deviations:

| Rule | Estimation error on clean data, RMSE | Contamination of the untouched seven, RMSE | Worst single change |
| --- | --- | --- | --- |
| mean and sd | 0.4419 | 0.5618 | 2.1617 |
| median and MAD | 0.8101 | 0.5680 | 47.6170 |
| Winsorise one each end, then mean and sd | 0.5424 | 0.3738 | 1.9715 |

Median and MAD is 1.83 times noisier than mean and standard deviation on clean
data and is no more robust in expectation, which is not what its reputation
suggests. The reason is `n = 8`. The median of eight points is the average of the
fourth and fifth order statistics and the MAD is the median of eight absolute
deviations, so both are step functions of the data and both jump when the eighth
point moves across an order statistic. The 47.6 in the last column is a run
where the MAD collapsed toward zero and the divisor with it.

It also fails on the fixture. It is the only rule of the three that changes a
conviction tier on a clean run with no outlier at all: AUDNZD moves, with a
largest spread change of `0.3951`. Adopting it would also invalidate every
threshold in `ScoringConfig`, since the scale of a MAD-based z is not the scale
of a standard-deviation-based one, so 0.75 and 1.50 and 2.50 would all need
resetting with no evidence to reset them against.

**Reject median and MAD.** It is the intuitive answer and it is wrong at this
sample size.

### Winsorising: reject, for a specific reason

Winsorising one point at each end works exactly as advertised. It removes the
contamination completely at the component level, the untouched seven keep a
spread of `2.5751` no matter how extreme the outlier gets, and it cuts
contamination RMSE by 33%. Its cost on clean data is modest, 23% more estimation
noise, and it changes no conviction tier on the clean fixture.

It is still the wrong choice, for a reason the tables above do not show.
Winsorising one point at each end of an eight-point cross-section replaces the
largest value with the second largest. On the clean section 7 fixture that ties
the top two currencies on **16 of 16 components**. Every run, on every
sub-indicator, ranks 1 and 2 become indistinguishable.

That is fatal here specifically. `bias.shortlist` explains at length that the
model's ordering is most reliable at its ends and that the shortlist works by
pairing the top of the ranking against the bottom. Winsorising takes the
sharpest information in the model and deletes it in every run in order to defend
against an event that happens rarely. It also mutes the dislocation itself: the
yen's composite at a 5 standard deviation MONETARY shock reads `+0.995` under the
current rule and `+0.631` under winsorising, so the one currency that genuinely
did something gets understated.

**Reject winsorising.**

### The option that is better than either

The problem with the current rule is not that it produces a wrong number, it is
that it produces a compressed number silently. Compression is exactly
computable, so it can be surfaced instead of suppressed.

For population z-scores over `n` names the squared z-scores sum to `n`. So once
the largest absolute z is `m`, the remaining `n - 1` currencies satisfy

    residual root mean square = sqrt( ( n - m^2 ) / ( n - 1 ) )

with no estimation and no parameter. For `n = 8`:

| Largest absolute z | Residual RMS of the other seven |
| --- | --- |
| 1.0000 | 1.0000 |
| 1.5000 | 0.9063 |
| 2.0000 | 0.7559 |
| 2.3000 | 0.6222 |
| 2.5000 | 0.5000 |
| 2.6458 | 0.3779 |

The fixture is already in this territory on two components, without anyone
having noticed:

| Pillar | Component | Largest absolute z | Residual RMS |
| --- | --- | --- | --- |
| MONETARY | `real_policy_rate` | 2.3481 | 0.5960 |
| EXTERNAL | `terms_of_trade` | 2.2024 | 0.6707 |
| INFLATION | `core_dev` | 2.0033 | 0.7547 |
| GROWTH | `pmi_composite` | 1.7856 | 0.8291 |

Japan's real policy rate of -2.30% has already compressed the other seven
currencies' real-rate scores to 60% of their natural spread, in the run that the
specification uses as its worked example.

### One incidental finding, and it points the same way

`docs/scoring-spec.md` section 2.3 specifies the re-standardisation divisor as
`sd(blend)`, this run's own cross-sectional standard deviation, and section 7.1
divides by `0.7001` accordingly. `src/fbe/pillars/base.py::blend_divisor` does
something different: it uses the median blend standard deviation over the last
`ScoringConfig.restandardisation_window_runs` runs, falling back to the run-local
value only below `min_restandardisation_runs`. The docstring's argument for that
is sound and unrelated to this question.

It also happens to reduce outlier contamination, because a run-local divisor
re-inflates a cross-section that an outlier has just compressed:

| Perturbation | Run-local divisor: largest spread move / tier changes | History-fixed divisor: largest spread move / tier changes |
| --- | --- | --- |
| CHF 3 sd | 0.1548 / 2 | 0.1248 / 0 |
| CHF 5 sd | 0.3865 / 4 | 0.2809 / 3 |
| JPY 3 sd | 0.2862 / 2 | 0.1614 / 1 |
| JPY 5 sd | 0.4764 / 5 | 0.3435 / 3 |
| AUD 3 sd | 0.4521 / 4 | 0.3304 / 3 |

The code is doing the better thing and the specification does not describe it.
That divergence needs resolving on its own merits regardless of this question.

### Recommendation

1. **Keep mean and population standard deviation.** Section 1.3 stands as
   written. Both robust alternatives were tested against the same perturbation
   and both are worse: median and MAD is noisier without being more robust,
   winsorising ties ranks 1 and 2 on every component in every run.
2. **Add the residual dispersion diagnostic.** Compute
   `sqrt((n - max_abs_z^2) / (n - 1))` per component, carry it in
   `PillarScore.notes` or as a run warning, and surface it in the report when it
   falls below a stated level. It costs one line, has no parameters, changes no
   score, and converts an invisible failure into a visible one. Choosing the
   level at which the report should complain is a judgement; `0.70`, which
   corresponds to a largest absolute z above roughly 2.15, is a reasonable
   starting point and should be stated as a convention rather than defended as a
   finding.
3. **Record the sign-flip case in section 10 item 5 as measured**, with the CHF
   shift and the yen figure of `+0.1869` to `-0.0117`, and record the two
   mitigations alongside it: ordering is preserved on any single component by
   construction, and the pair layer moves less than the composite layer because
   part of the contamination is common to both legs.
4. **Reconcile section 2.3 with `blend_divisor`.** They currently disagree, and
   the code's version is both better argued and, per the table above, more
   robust to the failure this question is about. Section 7.1's worked example
   would need a note explaining that it shows the fallback path.

---

## Spec 6. `horizon_days = 10` in the cost filter

> If the real average hold is three days the filter is roughly twice as
> permissive as it should be. What should it be, and what does it depend on?

**Verdict: ANSWERED on the structure, BLOCKED on the value.**

### The filter has one free parameter, not two

    cost_ratio = cost / ( atr_20d * sqrt(horizon_days) ) <= max_cost_ratio

rearranges to

    cost / atr_20d <= max_cost_ratio * sqrt(horizon_days) = k

Only `k` is identified. `max_cost_ratio` and `horizon_days` cannot be argued
about separately, because every pair of values with the same product describes
the same filter. At the defaults, `k = 0.05 * sqrt(10) = 0.1581`.

| `horizon_days` | `max_cost_ratio` | `k`, the ceiling on cost divided by ATR |
| --- | --- | --- |
| 3 | 0.05 | 0.0866 |
| 5 | 0.05 | 0.1118 |
| 10 | 0.05 | 0.1581 |
| 20 | 0.05 | 0.2236 |
| 3 | 0.0913 | 0.1581 |
| 5 | 0.0707 | 0.1581 |

So the section 10 concern is right in substance and slightly off in size. Moving
the horizon from 10 to 3 without touching `max_cost_ratio` tightens the filter by
a factor of `sqrt(10/3) = 1.83`, not 2. In plain terms, a pair with a 62-pip
20-day ATR may cost up to 9.80 pips round trip at `horizon_days = 10` and 5.37
pips at 3.

### What it depends on

Three things, in order of size.

1. **The actual holding period**, which the engine does not know and, per
   `docs/trading-plan.md`, is not entitled to an opinion about: "The engine has
   no view on holding period. It refreshes a bias; it does not tell a trade to
   close."
2. **The square-root scaling**, which assumes a random walk. Over a horizon on
   which the engine claims a directional bias, that assumption understates the
   move if the bias has any content and overstates it if the pair mean-reverts.
   The filter is a coarse screen so this is tolerable, but it means `k` is not a
   physical constant even once the hold is known.
3. **Which ATR**, since a 20-day daily ATR and the intraday range on the 1h and
   4h charts the entry is timed from are different numbers.

### What cannot be settled here

Nothing in this repository measures a holding period. Two of the two worked cost
examples in section 7 pass at every horizon in the table (NZDUSD at
`cost / atr = 0.0290`, NZDJPY at `0.0364`, against a ceiling of `0.0866` even at
`horizon_days = 3`), so the fixture cannot demonstrate a pair changing status.
There is no per-pair spread and ATR table in the repository, so which pairs sit
between `0.0866` and `0.1581` and would flip cannot be enumerated. That is the
missing input, and it belongs to the execution layer.

### Recommendation

1. **Do not change `horizon_days` yet.** Changing it now would be picking a
   number to replace a number.
2. **State `k` in section 6 of the spec.** Write the filter as "round-trip cost
   must not exceed `k = max_cost_ratio * sqrt(horizon_days) = 0.1581` of the
   20-day ATR" and note that the two constants are not separately identified.
   This is the single most useful edit in this document, because it stops the
   next person arguing about `horizon_days` in isolation.
3. **Measure the hold.** `journal.TradeRecord` already carries `opened_at` and
   `closed_at`, so the mean and dispersion of the hold are available with no new
   fields. To pin the mean to plus or minus one day, at a plausible dispersion of
   three days, needs about 35 closed trades: `(1.96 * 3 / 1)^2 = 35`. That is
   reachable inside a year of ordinary trading, which makes this the cheapest
   open question in the set to close. Once measured, set `horizon_days` to the
   observed median hold and set `max_cost_ratio` so that `k` lands where the
   trader wants it, rather than adjusting either alone.
4. **A better filter exists one layer down.** The quantity the trader actually
   cares about is cost as a share of the money at risk, which is
   `cost / stop_distance` and needs no horizon at all. `risk.py` receives the
   stop; `bias.py` does not, and cannot, because the bias is computed before a
   chart is looked at. So keep the ATR screen at the bias layer as a coarse
   filter and propose the exact check at the sizing layer. That is a proposal to
   whoever owns `docs/risk-and-execution.md`, not a change here.

---

## Spec 8. Conviction does not distinguish quality from magnitude

> Would a continuous conviction score, then bucketed, beat a bucket that is then
> demoted? Test whether they disagree, and on what.

**Verdict: NARROWED, and the empirical route to settling it is effectively
closed.** They disagree on 3.05% of pair observations, almost entirely at the
NONE and LOW boundary, which is the one place the specification already says the
decision is meaningless.

### The ladder's real defect, quantified

The demotions in section 5.4 act on an ordinal ladder, so the same trigger costs
wildly different amounts depending on where the pair started.

| Absolute spread | Base tier | After one demotion | Range of spreads that land in the same place |
| --- | --- | --- | --- |
| 2.55 | HIGH | MEDIUM | 1.50 to 2.50 |
| 2.00 | MEDIUM | LOW | 0.75 to 1.50 |
| 1.55 | MEDIUM | LOW | 0.75 to 1.50 |
| 1.20 | LOW | NONE | below 0.75 |
| 0.76 | LOW | NONE | below 0.75 |

A pair at 2.55 loses 0.05 of effective spread to a one-step coverage demotion. A
pair at 0.76 loses its direction entirely. The trigger is identical. That is a
genuine inconsistency and it is worth writing down whether or not the rule
changes.

### The comparison

The continuous alternative was built to introduce no new constants at all. Every
anchor is an existing `ScoringConfig` field, so the comparison is between two
readings of the same numbers rather than between a rule and a tuned rival:

    m_agreement  = min(1, agreement / min_agreement)          # 1 at and above 0.60
    m_coverage   = min(1, coverage / coverage_demotion)       # 1 at and above 0.80
    m_dispersion = min(1, max_dispersion / dispersion)        # 1 at and below 1.20

    Q = |spread| * m_agreement * m_coverage * m_dispersion

then bucketed at the same 0.75, 1.50 and 2.50, with the 24-hour event test still
capping at LOW.

**On the section 7 fixture the two schemes agree on all 28 pairs.**

Across 4,000 synthetic runs and 112,000 pair observations they disagree on 3.05%:

| Ladder says | Continuous says | Share of pair observations |
| --- | --- | --- |
| NONE | LOW | 2.60% |
| LOW | NONE | 0.39% |
| LOW | MEDIUM | 0.06% |
| NONE | MEDIUM | 0.00% |

85% of the disagreement is the case in the first row: a pair whose spread was
just over 0.75, demoted to NONE by the ladder for thin coverage or high
dispersion, which the continuous score haircuts but leaves above the line. That
is the boundary section 7.7 already describes as "a rounding difference between
two nearly flat currencies". The two schemes differ where neither of them
matters.

### Stability

Jiggling every input by a normal with standard deviation 0.05:

| Scheme | Pair observations changing tier |
| --- | --- |
| Bucket then demote | 3.64% |
| Continuous then bucket | 2.91% |

The continuous form is about 20% more stable, which is what you would expect: it
crosses three thresholds smoothly and one sharply, while the ladder crosses four
sharply. Stability is not correctness. A rule that never changes its mind is
stable and useless.

### Why the empirical route is closed

Distinguishing a 50% hit rate from a 60% one at 80% power and 5% significance
takes about 389 observations per arm. The two schemes disagree on 3.05% of pair
observations, which at 28 pairs a day is roughly 0.85 disagreements per run and
about 111 over six months of daily runs. Those overlap heavily: a 10-day horizon
means consecutive days re-observe the same episode, so six months yields on the
order of ten independent disagreeing episodes, not 111. The gap between ten and
389 is not closable by waiting a little longer. It would take years, and by then
the weights and the data set will both have changed.

So this question will not be answered by measurement at this cadence, and it
should stop being described as though Phase 6 will settle it.

### Recommendation

1. **Keep the ladder.** It is simpler, it is already specified and tested, and
   the alternative differs only where the specification itself says the answer
   does not matter. Since no measurement will separate them, the tie goes to the
   rule that is easier to explain to the person acting on it at seven in the
   morning.
2. **Record the quality-adjusted spread anyway.** Compute `Q` and carry it on
   `PairBias`, or in the report, without letting it decide anything. It costs
   three lines, it makes the ladder's inconsistency visible on the pairs where it
   bites, and it builds the forward record that would be needed if anyone ever
   does have the sample. Adding a field to `PairBias` is a `types.py` change and
   is not mine to make.
3. **Amend section 10 item 8** to say that the alternative was built and tested,
   that the two disagree on about 3% of pair observations concentrated at the
   NONE and LOW boundary, and that the sample needed to choose between them is
   roughly forty times what six months of forward recording produces. An open
   question that cannot be closed should say so rather than waiting.

---

## Roadmap 4. Static or rolling `risk_beta`

> Betas move with the regime. Rolling estimate or documented static
> approximation? What would a rolling estimate need, and can the data layer
> supply it?

**Verdict: ANSWERED, conditional on one stated quantity.** A rolling estimate
does not beat the static constant unless the true beta drifts by more than about
a quarter of the `-1..+1` band, and even then the improvement is small next to
the noise it introduces.

### The simulation

True beta follows an Ornstein-Uhlenbeck process around each currency's
`CurrencyMeta` value with a 125-day half-life. Currency returns are
`0.25 * beta * equity_return + noise`, with a 1.0% daily equity standard
deviation and a 0.5% daily idiosyncratic currency standard deviation. At those
settings a normalised beta of 0.9, which is AUD, gives a daily correlation with
equities of 0.41, at the top of the plausible G10 range, so this is a generous
signal-to-noise assumption rather than a pessimistic one. Beta is then estimated
by rolling OLS and both estimators are scored against the true path.

Root mean squared error on the `-1..+1` scale, lower is better:

| Drift standard deviation of the true beta | Window | Rolling RMSE | Static RMSE | Rolling wins |
| --- | --- | --- | --- | --- |
| 0.15 | 60 | 0.2731 | 0.1583 | no |
| 0.15 | 125 | 0.2018 | 0.1511 | no |
| 0.15 | 250 | 0.1659 | 0.1402 | no |
| 0.15 | 1000 | 0.1608 | 0.1518 | no |
| 0.25 | 60 | 0.2802 | 0.2300 | no |
| 0.25 | 125 | 0.2319 | 0.2672 | yes |
| 0.25 | 250 | 0.2545 | 0.2566 | marginal |
| 0.25 | 1000 | 0.2584 | 0.2576 | no |
| 0.35 | 60 | 0.3036 | 0.4042 | yes |
| 0.35 | 125 | 0.2716 | 0.3129 | yes |
| 0.35 | 250 | 0.3021 | 0.3260 | yes |
| 0.35 | 1000 | 0.3897 | 0.3734 | no |

The break-even sits at a drift standard deviation near 0.25. Below it the static
constant wins at every window from 60 days to 1000. At 0.35 the best rolling
window improves RMSE by 13%.

The reason is in a column not shown above: the rolling estimate's own standard
deviation across runs is between 0.62 and 0.77 on a scale where the eight true
betas span -0.9 to +0.9. The estimator wanders across most of the band the
constants occupy. A 60-day window, which is the one people reach for because it
responds quickly, is the worst performer at every drift level tested.

### What a beta error costs the model

`RISK score = 2 * R * risk_beta`, so the error is proportional to the regime
reading and vanishes when the regime is calm.

| `R` | Beta error | Pillar error | Composite error | Worst pair spread error | Share of the 0.75 neutral band |
| --- | --- | --- | --- | --- | --- |
| -0.25 | 0.30 | 0.1500 | 0.0150 | 0.0300 | 4.0% |
| -0.625 | 0.15 | 0.1875 | 0.0188 | 0.0375 | 5.0% |
| -0.625 | 0.30 | 0.3750 | 0.0375 | 0.0750 | 10.0% |
| -1.000 | 0.30 | 0.6000 | 0.0600 | 0.1200 | 16.0% |
| -1.000 | 0.50 | 1.0000 | 0.1000 | 0.2000 | 26.7% |

On the section 7 fixture at `R = -0.625`, perturbing all eight betas with a
normal of the stated standard deviation, over 2,000 draws:

| Beta error standard deviation | Pair observations changing tier | Changing direction | Largest spread move |
| --- | --- | --- | --- |
| 0.15 | 3.20% | 1.96% | 0.1152 |
| 0.30 | 5.25% | 2.96% | 0.2127 |
| 0.50 | 6.94% | 3.73% | 0.3492 |

The whole question is worth a few percent of tier decisions, and only in a
risk-off regime. In a calm run where `R` is near zero it is worth nothing, by
construction.

### What a rolling estimate would need, and whether the data layer can supply it

It would need three things.

1. **A per-currency return that is not quoted against another G10 currency.**
   This is the part that is easy to get wrong. Regressing EURUSD on the S&P
   measures the difference between the euro's beta and the dollar's, not either
   one, and the dollar has a large negative beta of its own. The fix is a
   currency index per currency, for example the geometric mean of that
   currency's seven crosses from `universe.ALL_PAIRS`. That is constructible
   from `datasources/prices.py` and needs no new source.
2. **A daily risk factor.** The RISK pillar already requires
   `world_equity_index`, so this exists.
3. **At least 125 trading days of overlapping daily history per run**, and the
   table above says shorter windows are worse than doing nothing.

So the data layer can supply it. The simulation says it should not be asked to.

### Recommendation

1. **Keep the static betas** in `CurrencyMeta` and stop calling it an
   approximation without a number. Replace the note in section 3.7's known
   failure mode with the stated error band: the static constant is worth using
   as long as the true beta's drift standard deviation stays under roughly 0.25
   of the `-1..+1` band, at which point a 125-day rolling estimate becomes
   marginally better, and even at a drift of 0.35 the improvement is 13% of an
   error worth about 5% of the neutral band.
2. **Record the currency-index requirement** in roadmap open question 4, because
   it is the part a future implementer will get wrong, and a beta measured off
   USD pairs would be confidently and invisibly wrong for every currency at
   once.
3. **Review the constants on the section 8 cadence**, once a year or after a
   structural regime change, and record the date they were last reviewed. That
   also answers half of section 10 item 9, which notes there is no process for
   reviewing static metadata.
4. **Do not add a rolling estimator** unless someone first measures the drift of
   the true betas and finds it above 0.25. That measurement is the same
   regression, run once historically rather than every day, and it is cheap.

---

## Roadmap 6. Does POSITIONING earn 0.10 with the COT lag?

> CFTC COT is three days stale on arrival and up to ten by the next release.
> What fraction of the pillar's information survives?

**Verdict: NARROWED on the lag, BLOCKED on whether the pillar earns its weight.**
The two halves of this question have different answers and should be separated.

### How much survives, as a function of one unmeasured number

Positioning is persistent, so the lag attenuates rather than destroys. Under an
AR(1) in weekly steps with weekly autocorrelation `rho`, the correlation between
the reading acted on and the true current reading is `rho^(L/7)` for a lag of `L`
days. The lag runs from 3 days on the Friday of publication to 10 days on the
following Thursday, averaging about 6.5.

| Weekly `rho` | 3 days | 6.5 days | 10 days |
| --- | --- | --- | --- |
| 0.95 | 0.978 | 0.953 | 0.929 |
| 0.90 | 0.956 | 0.907 | 0.860 |
| 0.85 | 0.933 | 0.860 | 0.793 |
| 0.80 | 0.909 | 0.813 | 0.727 |
| 0.70 | 0.858 | 0.718 | 0.601 |
| 0.60 | 0.803 | 0.622 | 0.482 |
| 0.50 | 0.743 | 0.525 | 0.371 |

Section 8 sets a floor of 0.05 below which a pillar should be deleted rather than
diminished. The lag alone pushes an effective weight of 0.10 below that floor
only when weekly persistence falls under about 0.52, which would mean speculative
positioning half-reverting every week.

### The lag is not the pillar's main problem

Section 3 of this document measured something the specification does not state:
the POSITIONING shape function emits at a cross-sectional standard deviation of
`0.5885`, against exactly 1.0 for the five pillars that go through the
re-standardisation pass. Combining the two effects:

| Weekly `rho` | Attenuation at 6.5 days | Effective weight from the lag alone | Effective weight including the shape function |
| --- | --- | --- | --- |
| 0.95 | 0.953 | 0.0953 | 0.0561 |
| 0.90 | 0.907 | 0.0907 | 0.0534 |
| 0.85 | 0.860 | 0.0860 | 0.0506 |
| 0.80 | 0.813 | 0.0813 | 0.0478 |
| 0.70 | 0.718 | 0.0718 | 0.0423 |

At any plausible persistence the shape function costs the pillar more than the
publication lag does. If POSITIONING is under-weighted relative to its declared
0.10, the reason is inside `f(p)`, not inside the CFTC's publication schedule.
Whether that is a problem depends on whether 0.10 was ever the intended
influence, which the specification does not currently say either way.

### What is blocked, and what is not

**Not blocked: the lag.** Weekly persistence is measurable from CFTC history
alone, with no returns and no journal. Once `datasources/cot.py` lands, compute
the net non-commercial share of open interest per currency, take the AR(1)
coefficient of the time-series z per currency over `lookback_years`, and read the
attenuation off the table above. Roughly 260 weekly observations per currency at
five years, which is ample for an AR(1) coefficient. This should be a spot-check
test, so that a persistence collapse in some future regime becomes visible rather
than being absorbed silently.

**Blocked: whether it earns 0.10.** Persistence is an upper bound on surviving
information, not a measure of surviving value. A perfectly persistent series that
never predicted anything survives the lag intact and is still worth nothing. The
only thing that answers the weight question is the relationship between lagged
`p` and subsequent pair returns, which requires COT history joined to forward
returns and, per section 3 of this document, roughly 2,200 currency-weeks to put
100 observations in the contrarian bin. That is about seven years across the six
G10 currencies with a liquid contract. The data exists back to 1986, so the
measurement is possible, but it is a research task with a look-ahead-bias
problem, not something six months of forward recording delivers.

### Recommendation

1. **Keep POSITIONING at 0.10** for now. Nothing measured says otherwise, and
   section 8 is right that the default is to change nothing.
2. **Add the persistence measurement** to the Phase 3 definition of done, next to
   the existing pillar cross-correlation requirement. It is cheap, it needs no
   returns, and it converts the lag half of this question from an opinion into a
   number.
3. **Record the effective weight of roughly 0.056** in roadmap open question 6
   and in section 3.6, and note that the shape function accounts for more of the
   shortfall than the lag does. That reframes the question usefully: the honest
   version is not "does the lag ruin this pillar" but "was 0.10 ever what this
   pillar was going to contribute", and the answer to the second is no.
4. **Do not raise the weight to compensate** for the shape function's quietness.
   The two are separate decisions and combining them would hide a real property of
   the pillar inside a weight.

---

## Incidental findings

Raised here because they were found while answering the above, and left for
their owners.

1. **Section 2.3 and `blend_divisor` disagree.** The specification divides the
   blend by this run's own cross-sectional standard deviation, and section 7.1
   divides by `0.7001` accordingly. `src/fbe/pillars/base.py` divides by the
   median over recent runs and treats the run-local value as a fallback that must
   be recorded in `PillarScore.notes`. The code's version is the better one on
   its own argument and, per section 5 above, is also measurably more robust to a
   dislocated currency. Section 2.3 and the section 7 worked example both need
   updating, and section 7 will need a note that it demonstrates the fallback
   path. Until then the fixture in section 7 and the code cannot both be right.
2. **The section 7 fixture is reproducible and should become a test.** Section 9
   already lists it as a test obligation. Reconstructing it from the raw input
   tables reproduced every published figure exactly, so the obligation is
   achievable as written and nothing in the worked example is approximate.
3. **`PillarScore` has nowhere to carry a cross-section diagnostic.** Two
   recommendations above want one: the raw cross-sectional dispersion from
   section 4, and the residual dispersion from section 5. Both can live in
   `notes` as strings, which is ugly but needs no `types.py` change, or both can
   be fields, which does. That decision belongs to whoever owns `types.py`.
