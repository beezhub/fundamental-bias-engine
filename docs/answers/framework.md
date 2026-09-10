# Answers: framework questions

Six open questions from `docs/scoring-spec.md` section 10 and the "Open
questions" list in `docs/roadmap.md`, taken up deliberately rather than left to
drift.

**This file is a proposal, not a change.** Section 10 of the scoring spec says
none of these may be silently resolved in code. Nothing in the spec, the
methodology, the roadmap or any source file has been edited. Where a
recommendation would change one of those documents, the change is stated
precisely and left for the owner to accept or reject.

**Verdicts.** Each question gets exactly one of four, and they mean different
things:

| Verdict | Meaning |
| --- | --- |
| ANSWERED | Settled with evidence, cited. |
| NARROWED | Not settled, but options eliminated or the answer bounded. |
| BLOCKED | Cannot be answered until the journal has real trades. The measurement and the sample size needed are stated. |
| JUDGEMENT | No empirical answer exists. It is a preference, and one is recommended. |

Where a question has two genuinely separate parts, each part carries its own
verdict and they are labelled. They are not averaged into one.

**On provenance.** As stated once in `docs/methodology.md`, the relative-value
framework this engine implements is a reconstruction assembled from publicly
available descriptions of the Anton Kreil and ITPM approach and from general
macro-FX practice. Nothing below reproduces paid course material. Every citation
here is to public research.

**On what the citations do and do not establish.** The published work cited
below is about the FX market in general. None of it was run on this repository's
outputs, none of it validates these weights, and no number in this file is a
measured result of this model. Phase 6 of the roadmap remains the first point at
which anyone can say whether the engine works.

---

## Q1. Inflation credibility: is the double-counting concern real, and is there a cleaner instrument than the gate?

*Source: `docs/scoring-spec.md` section 10, item 1.*

**Verdict: ANSWERED on the double-counting question. NARROWED on the cleaner instrument.**

### Part A: the double-counting concern. ANSWERED, and the spec has it aimed at the wrong term.

Section 10 rules the front-end response gate out partly on the grounds that "the
real policy rate term already inside MONETARY captures part of the same effect,
so the gate would partly double-count something the model already sees." Three
distinctions need separating, because the sentence is doing the work of three
different claims.

**1. Literal double-counting: no.** The gate is
`g = clip(yield_2y_chg_3m / 0.10pp, 1.0)` applied multiplicatively to a positive
inflation gap. Its input, `yield_2y_chg_3m`, enters MONETARY as a linear term
with sub-weight 0.25 and a monotone positive sign. `real_policy_rate` is
`policy_rate - cpi_yoy` and contains no yield-change term at all. The gate would
introduce a product, `gap x front-end change`, and no such interaction exists
anywhere in the model. A product of two variables does not double-count either
of them linearly. On the literal reading, the concern does not hold.

**2. Effective overlap on the episodes that matter: yes.** The one clear
within-G10 instance of a credibly tolerated overshoot is the Bank of Japan
through 2022. Japanese CPI ran above target while the policy rate stayed near
zero and the front end barely moved. In that configuration `g` is near zero, so
the gate would zero out JPY's positive inflation contribution. But
`real_policy_rate` was simultaneously deeply negative for the same reason, and
MONETARY was already scoring that. The worked example in spec section 7.1 shows
the mechanism at a smaller scale: JPY has the fastest-rising 2-year yield in the
universe, at z-scores of +1.46 and +1.53, and still finishes MONETARY at -0.31,
pulled there by a real policy rate of -2.30%. So the two corrections fire on the
same episodes and push the same way. Effective overlap is real, even though the
variables are different.

**3. A third overlap, larger than either, that section 10 does not mention.**
`real_policy_rate = policy_rate - cpi_yoy` carries an explicit coefficient of
minus one on headline CPI. INFLATION carries a positive loading on the same
series. The model therefore has an inflation term with two opposing signs, and
they partially cancel by construction.

The size of the cancellation, computed from the worked example's own
cross-sectional dispersions in spec sections 7.1 and 7.2, and using the fact
that with `n = 8` a move in one currency shifts the mean as well, so its own
z-score responds by `7/8` of the naive amount:

| Term | Composite loading per 1pp of headline CPI |
| --- | --- |
| INFLATION, headline sub-indicator | +0.1008 |
| INFLATION, core sub-indicator, if core moves one for one with headline | +0.1666 |
| MONETARY, real policy rate | -0.0501 |

Two cases:

- Headline moves alone, core unchanged. Net loading +0.0508 against INFLATION's
  own +0.1008. **The real policy rate term cancels 49.7% of it.**
- Headline and core move together. Net loading +0.2174 against +0.2674.
  **Cancellation of 18.7%.**

So the model's net response to inflation is somewhere between roughly a fifth
and a half smaller than the INFLATION pillar's declared 0.15 weight implies,
and which end of that range applies depends on whether the print is a headline
shock or a broad one. This is arithmetic, not a measurement, and the exact
figures move with each run's dispersions, since `sd(blend)` and each
sub-indicator's `sd` are recomputed daily. The direction and the rough magnitude
do not move.

That is a finding worth acting on independently of the gate. The re-standardisation
pass in spec section 2.3 exists precisely so that "the weights in section 3 are
meant to be the whole of the model's opinion about relative importance". The
`-cpi_yoy` inside `real_policy_rate` quietly breaks that guarantee for the one
pillar it touches.

**The base sign the pillar rests on is supported.** Clarida and Waldman studied
10-minute windows around inflation announcements for 10 countries from July 2001
to March 2005 and found that higher than expected inflation appreciates the
currency, with an r-squared above 0.25 for core measures, and with significant
differences between the inflation-targeting and the non-inflation-targeting
countries ([NBER w13010](https://www.nber.org/papers/w13010)). The mechanism is
exactly the policy channel spec section 3.2 asserts: the surprise does not change
beliefs about the long-run price level, it changes the expected near-term policy
rate.

**The credibility conditioning is also supported, but mostly out of sample for
this universe.** Dallas Fed research finds advanced-economy currencies appreciate
on an inflation surprise while emerging-market currencies show mixed or
depreciating responses, and that central bank transparency explains 48% of the
cross-country variation in that correlation
([Dallas Fed, 2024](https://www.dallasfed.org/research/economics/2024/0903)).
That is strong evidence the effect exists. It is weak evidence that it can be
exploited here, because the dispersion in credibility is measured between
advanced and emerging economies, and all eight currencies in this universe are
transparent inflation targeters at the top of that scale. The gate would be
harvesting a cross-sectional effect whose documented variation comes from a
sample this model does not trade.

### Part B: a cleaner instrument. NARROWED.

Four candidates were considered. Three are eliminated.

**Eliminated: market-based inflation expectations (breakevens or inflation
swaps).** This is the textbook credibility instrument and it fails on two counts.
Coverage: US TIPS breakevens are free on FRED, UK index-linked gilts are
available, and after that the G10 runs out. Euro area inflation swaps are not on
a free feed, and JPY, CHF, CAD, AUD and NZD have no liquid, freely published
breakeven series. Under `MIN_COMPONENT_WEIGHT` that would leave the term absent
for most of the universe. Sign: a rising breakeven is ambiguous, since it can
mean the market expects the bank to hike or that the market has stopped believing
the target. An instrument whose sign is ambiguous is worse than no instrument.

**Eliminated: substituting a real 2-year yield for the nominal.** This adds no
information the existing `real_policy_rate` term does not carry and makes the
cancellation documented in Part A larger, not smaller.

**Eliminated: a discrete credibility flag in `CurrencyMeta`.** A hand-set boolean
per currency is a free parameter with no update process, and it interacts badly
with Q9 below.

**Retained, and reframed: the gate and the real policy rate term are alternative
implementations of one correction, and should be chosen between rather than
stacked.** This is the cleanest form of the question and it dissolves the
double-counting objection, because you cannot double-count if you only run one.

- Option 1, status quo. Keep `real_policy_rate` at sub-weight 0.15, no gate.
  The credibility correction is always on, linear, and invisible. Its cost is
  the 19% to 50% cancellation above, which is unstated in the spec.
- Option 2. Drop `real_policy_rate` from MONETARY, redistribute its 0.15
  sub-weight, and apply the gate inside INFLATION. The correction becomes
  explicit, conditional, and located in the pillar it corrects. Its cost is the
  two free parameters section 10 already objects to, the `0.10pp` scale and the
  pivot rule.

Choosing between them requires measurement that does not exist. Neither is
obviously better on reasoning alone.

### Recommendation

1. **Do not add the gate now.** Section 10's conclusion stands, but its stated
   reason does not, and the reason should be corrected rather than left as a
   claim that will be repeated.
2. **Change the reason.** Replace the double-counting sentence in section 10
   item 1 with the Part A finding: the gate does not double-count linearly, it
   overlaps in effect on the episodes it targets, and there is a separate and
   larger opposing-sign overlap between INFLATION and `real_policy_rate` that is
   worth documenting on its own.
3. **Document the net loading.** Add the two-case table above to spec section
   3.2, marked as arithmetic derived from the worked example's dispersions, so
   that a reader can see what the model's actual response to an inflation print
   is rather than inferring it from the 0.15 weight.
4. **Bind the decision to Q2 of section 10, as section 10 already says.** The
   real-rate sub-weight and the gate are the same decision. This belongs with
   the quant's sub-weight work, not before it. Specifically: if the sub-weight
   test moves toward equal weighting inside MONETARY, `real_policy_rate` rises
   from 0.15 to 0.20 and the cancellation grows, which strengthens the case for
   Option 2.
5. **Add the failure mode.** MONETARY's known-failure paragraph in section 3.1
   should say that its real policy rate term makes MONETARY partly an inflation
   pillar with the opposite sign to INFLATION.

---

## Q7. Does carry deserve its own pillar?

*Source: `docs/scoring-spec.md` section 10, item 7.*

**Verdict: ANSWERED. No separate carry pillar. The overlap argument holds, and
it can be made precise.**

### The overlap is near-total, by construction

Koijen, Moskowitz, Pedersen and Vrugt define an asset's carry as its expected
return assuming its price does not change
([JFE 2018](https://pages.stern.nyu.edu/~lpederse/papers/Carry.pdf)). In FX that
quantity is the interest rate differential, equivalently the forward discount.
There is no other input.

MONETARY already scores rate levels cross-sectionally, at sub-weight 0.15 on
`policy_rate` and 0.25 on `yield_2y`, which is 0.40 of the pillar. A carry pillar
would take the same rate levels, z-score them across the same eight currencies,
and emit a score. The cross-sectional z of a rate level *is* the cross-sectional
carry rank, up to a monotone transform. In a differenced relative-value model
the two are the same object with different labels.

The roadmap already contains the decisive internal test. Phase 3's definition of
done says "two pillars correlated above 0.9 across the cross-section are
double-counting and one gets folded or reweighted." A carry pillar built from
rate levels would fail that test on the first run, before any data question
arises.

### At the owner's horizon, carry as income is negligible

Carry-as-a-pillar could only earn its keep by contributing something the level
terms do not, and the obvious candidate is the accrual itself. The arithmetic
rules it out.

At an annualised differential of `d` percent, a hold of `n` calendar days
accrues `d * n / 365` percent. The scoring spec's bias horizon is
`horizon_days = 10`. At `d = 2`, a wide G10 differential, that is **0.055%**.
The trading plan holds hours to a few days, so the realised figure is smaller
again. Compare that against the pair's expected move over the same window, which
spec section 6 already computes as `atr_20d_pips * sqrt(10)`. The engine will
produce that number itself in Phase 1, and it will not be close. Carry accrual
is a rounding error against a 1-2% risk position held for two days.

So only carry-as-a-signal matters here, and carry-as-a-signal is the rate level
that MONETARY already holds.

### The carry factor's risk is already the RISK pillar's subject

Lustig, Roussanov and Verdelhan identify a slope factor in exchange rates that
accounts for most of the cross-sectional variation in average excess returns
between high and low interest rate currencies
([RFS 2011, NBER w14082](https://www.nber.org/papers/w14082)). That factor's
return is a risk premium, and Brunnermeier, Nagel and Pedersen document what the
risk is: carry returns are negatively skewed, and the negative skewness comes
from sudden unwinds in periods when risk appetite and funding liquidity fall
([NBER w14473](https://www.nber.org/papers/w14473)).

That unwind is precisely what spec section 3.7 describes: "in a genuine risk-off
episode, correlations across G10 FX collapse into a single factor. Funding and
haven currencies are bought regardless of their macro picture, and high-beta
commodity currencies are sold regardless of theirs."

So a carry pillar would double-count MONETARY's level terms on the upside and
the RISK pillar on the downside. It adds a data dependency and a maintenance
burden and no independent information. Under spec section 8 constraint 3, a
pillar that cannot justify 0.05 should be deleted, not diminished. This one
should not be created.

### One caveat, and it belongs in the spec as a failure mode

There is a live tension in the sign of the level terms that Q7 has surfaced and
that the spec does not currently state.

The classic Fama result, which underwrites the "high rate currency appreciates"
reading, does not hold across the whole sample. Bussière, Chinn, Ferrara and
Heipertz find the rejection of UIP holds for eight dollar exchange rates in the
earlier sample but "does not survive into the period during and in the decade
after the financial crisis", where the coefficient on the interest differential
"becomes large and positive"
([NBER w24342](https://www.nber.org/papers/w24342), IMF Economic Review 2022).
A large positive Fama coefficient means high-rate currencies depreciate, which is
the opposite of the level terms' assumed sign.

This does not argue for removing the level terms. It argues that the level terms'
sign is a regime-dependent claim and the momentum terms' sign is not, which is
consistent with the pillar already putting 0.45 of its sub-weight on the two
momentum windows and only 0.40 on levels. It is exactly the kind of thing a
"known failure mode" paragraph is for.

Also worth recording: even taken on its own terms, the carry factor is weakest
in this universe. Published work finds including emerging market currencies
substantially raises the carry Sharpe ratio relative to a developed-market-only
portfolio ([NBER w32900](https://www.nber.org/system/files/working_papers/w32900/revisions/w32900.rev0.pdf)).
A G10-only engine is where the carry factor has least to offer.

### Recommendation

1. **Close item 7.** Move it from section 10 to a decided item. No carry pillar.
   The overlap argument holds and the reasoning above is the record of why.
2. **Say where carry lives.** Add one sentence to spec section 3.1 stating that
   FX carry is present in this model as the two rate-level terms of MONETARY and
   is not scored separately, so a future reader does not re-open the question.
3. **Add the sign caveat.** Extend MONETARY's known failure mode with the New
   Fama Puzzle point: the positive sign on the rate *level* is regime-dependent
   in a way the sign on the rate *change* is not.
4. **Feed this to the quant.** The level-versus-momentum split, currently 0.40
   against 0.45, is the sub-weight division that carries real economic content
   inside MONETARY. If item 2's sub-weight test only has budget for one
   comparison, this is the one worth running.

---

## Q9. Which currency metadata actually moves on a timescale that matters?

*Source: `docs/scoring-spec.md` section 10, item 9.*

**Verdict: ANSWERED. Two of the three are fine as constants. `risk_beta` is not,
and its instability is documented and recent.**

| Field | Moves on a timescale that matters? | Verdict |
| --- | --- | --- |
| `inflation_target` | No. Changes are rare, announced in advance, and dated. | Fine as a constant. Needs a review process, not a feed. |
| `commodity_link` | No, for the identity of the link. Yes, for its strength, which is not what this field holds. | Fine as a constant. The error is elsewhere. |
| `risk_beta` | Yes, and fast enough to have had the wrong sign for a live currency within the last eighteen months. | Not fine as a constant. |

### `inflation_target`: static is correct

Inflation targets in this universe change on a decade scale, and when they change
it is by announcement, so a change can be applied deliberately rather than
detected. Recent evidence that the anchor holds:

- The Federal Reserve's 2025 framework review removed the average-inflation-targeting
  language but left the 2% longer-run goal untouched, and the goal was explicitly
  not a focus of the review
  ([Federal Reserve, 2025 Review overview](https://www.federalreserve.gov/monetarypolicy/guide-to-changes-in-statement-on-longer-run-goals-monetary-policy-strategy.htm)).
- The RBNZ remit was amended twice in three years, adding a house price
  consideration in 2021 and removing it in 2023, and the 1% to 3% band with a 2%
  midpoint survived both
  ([RBNZ, history of the remit](https://www.rbnz.govt.nz/monetary-policy/about-monetary-policy/history-of-the-remit-and-policy-targets-agreement)).

That is the pattern to expect: the framework around the target churns, the target
does not.

**But the field has a different problem that is larger than drift.** It holds a
point, and three of the eight currencies have a band. The spec's own example in
section 3.2 shows AUD at 2.5 as the midpoint of a 2-3% band, and NZD at 2.0 as
the midpoint of a 1-3% band. A 3.1% print is at the top of the Australian band
and outside the New Zealand band, and those are different policy problems that
the current field cannot distinguish. Switzerland is a third case: the SNB does
not run a point target at all, it defines price stability as inflation below 2%,
so the 1.0 in `CurrencyMeta` is a modelling choice standing in for an asymmetric
objective, not a published number.

### `commodity_link`: static is correct, and the field is not where the error is

The identity of each link is stable on any horizon this model operates over.
Dairy accounted for 29.9% of New Zealand's total goods exports in the twelve
months to June 2025 ([interest.co.nz on Stats NZ data](https://www.interest.co.nz/economy/134309/continued-strong-annual-growth-export-dairy-products-fruit-and-forest-products-has)),
and Australia remained the source of 53.9% of world iron ore exports in 2025
([worldstopexports](https://www.worldstopexports.com/iron-ore-exports-country/)).
Neither of those is going to flip inside a review cycle.

What does move is the *strength* of the transmission from that commodity to the
currency, and `EXTERNAL` currently assumes it is identical for all three by
giving each a flat 0.30 sub-weight on the terms-of-trade term. That assumption is
the weak one, not the link identity, and it is a sub-weight question rather than
a metadata question. It belongs with section 10 item 2.

### `risk_beta`: not fine as a constant

This is the field that fails, and the evidence is recent and specific.

**The dollar, April 2025.** The ECB's Financial Stability Review reports that the
US dollar depreciated 12% against the euro from January 2025, with roughly 7
percentage points of that after 2 April, that the correlation between US Treasury
yields and the dollar "turned negative for a period after 2 April" and remained
weak thereafter, and that cross-asset correlations had only partially normalised
by the time of writing
([ECB FSR, November 2025](https://www.ecb.europa.eu/press/financial-stability-publications/fsr/special/html/ecb.fsrart202511_01~fdf147a04a.en.html)).
CEPR's high-frequency study of the same announcement finds the dollar
"depreciated on impact, rather than appreciating as expected based on standard
theory and prior evidence"
([CEPR/VoxEU](https://cepr.org/voxeu/columns/tariffs-dollar-and-equities-high-frequency-evidence-liberation-day-announcement)),
and separate CEPR work describes the dollar as appearing "to switch from being a
safe-haven currency to a 'risk-on' currency"
([CEPR/VoxEU](https://cepr.org/voxeu/columns/dollar-dominance-and-trump-administration)).

Run that through spec section 3.7. In April 2025 a broad equity index was deep
below its 52-week high and a volatility index was far above its mean, so `R`
would have been strongly negative. With `risk_beta(USD) = -0.5`, the RISK pillar
would have scored USD strongly positive at the exact moment the dollar was
falling with equities. Not a magnitude error. A sign error, on the most-traded
currency in the universe, on the days the pillar exists to handle.

**The yen.** The same instability is documented on the other side. The IMF's
study of the yen's haven behaviour attributes it to mechanisms including
Japanese investors' repatriation and the yen's funding role, not to anything
permanent about Japan
([IMF WP/13/228](https://www.imf.org/external/pubs/ft/wp/2013/wp13228.pdf)), and
those mechanisms weaken when Japanese rates rise. Spec section 3.7's own failure
mode already says this. Time variation in safe-haven status is an active research
question in its own right, published under exactly that heading
([Time-variant safe haven currencies, 2024](https://www.sciencedirect.com/science/article/pii/S1059056024002685)).

### What to do about `risk_beta`

The obvious repair, a rolling regression of each currency's return against a
global risk factor, has a defect that matters more here than usual. A beta
estimated over a trailing window is a description of the regime that just ended.
It would have carried `risk_beta(USD) = -0.5` into April 2025 unchanged, and
would only have turned the sign some weeks after the episode it was needed for.
Rolling estimation converts a wrong constant into a lagging variable, which is
not obviously an improvement and costs a fitted parameter.

The recommendation is the middle path, and it is the one consistent with how the
rest of this repository behaves: **report the disagreement, do not silently
adapt to it.**

### Recommendation

1. **Keep all three fields static.** The alternative for `risk_beta` is not
   better, for the reason above.
2. **Add a rolling realised beta as a reported diagnostic, not as a score
   input.** Compute each currency's trailing beta against a global risk proxy
   from the price data the engine already needs, and surface it next to the
   static value on the report and dashboard. When the realised beta and the
   static one disagree in sign, say so on the page. The RISK pillar continues to
   use the static value. This is cheap, it uses no new data source, it changes no
   score, and it converts an unmeasured assumption into something visible that
   Phase 6 can act on.
3. **Add `last_reviewed` to `CurrencyMeta`.** Section 10 item 9's real complaint
   is "there is no process for reviewing them and no record of when they were
   last correct." A date field with a default fixes the record half of that. Per
   `CLAUDE.md`, an additive field with a default is the cheap kind of change to
   `universe.py`, and it needs a docstring saying who reads it.
4. **Set the review cadence in the methodology.** Annual, plus on any announced
   framework change by a central bank in the universe. That matches the
   re-weighting cadence in spec section 8, which already names "a G10 central
   bank formally abandoning an inflation target" as a trigger.
5. **Record the band problem.** Add a note to spec section 3.2 that
   `inflation_target` holds a point where AUD, NZD and CHF have a band or an
   asymmetric objective, and that the point is a midpoint chosen by this model.
   Whether to add an optional `inflation_band` field is a separate decision and
   is not recommended yet, because nothing currently consumes it.

---

## Q2 (roadmap). Is MONETARY at 0.30 too low?

*Source: `docs/roadmap.md`, Open questions, item 2.*

**Verdict: NARROWED. The literature bounds the answer away from 0.40 and away
from "the other six are tiebreakers". It cannot supply a number, and the residual
is BLOCKED.**

### What the published work supports

**Fundamentals do move G10 FX, and the rate channel is the transmission.**
Andersen, Bollerslev, Diebold and Vega show that macroeconomic announcement
surprises produce jumps in the conditional mean of dollar spot rates, so
"high-frequency exchange-rate dynamics are linked to fundamentals"
([AER 2003](https://public.econ.duke.edu/~boller/Published_Papers/aer_03.pdf)).
Faust, Rogers, Wang and Wright, studying short windows around announcements
across exchange rates and both countries' term structures, find that a stronger
than expected release appreciates the dollar on the day
([JME 2007](https://www.federalreserve.gov/econres/ifdp/the-high-frequency-response-of-exchange-rates-and-interest-rates-to-macroeconomic-announcements.htm)).
That supports the model's basic architecture. It does not say the rate
differential should carry 0.40.

**What predicts, when anything does, is a mixture and not the rate differential
alone.** Rossi's survey of the whole literature concludes that "predictability is
most apparent when one or more of the following hold: the predictors are Taylor
rule or net foreign assets [fundamentals], the model is linear, and a small
number of parameters are estimated"
([JEL 2013](https://crei.cat/wp-content/uploads/users/working-papers/Rossi_ExchangeRatePredictability_Feb_13.pdf)).
Taylor rule fundamentals are the inflation gap, the output gap and the policy
rate together. In this model's vocabulary that is MONETARY, INFLATION and GROWTH
jointly, which currently carry 0.30, 0.15 and 0.15, or 0.60 between them.

That is the central point for this question, and it cuts against the premise.
The proposal in the roadmap is to raise MONETARY toward 0.40 and treat the other
six as tiebreakers. The literature that finds any short-horizon out-of-sample
predictability at all finds it with a *combination* that includes inflation and
activity, not with the interest differential on its own. Raising MONETARY at
INFLATION's and GROWTH's expense moves the model away from the specification the
evidence favours.

Rossi's second and third conditions are worth reading too. "The model is linear"
and "a small number of parameters are estimated" are both arguments the spec
already makes for itself, in section 10 item 1's objection to conditionals and in
section 8's warnings about curve fitting.

**At the horizon this model outputs on, most FX variance is not explained by any
macro variable.** Evans and Lyons produce a model of daily deutsche mark/dollar
log changes with an R-squared above 60% using order flow, against macro models
that explain close to nothing at daily frequency
([JPE 2002, NBER w7317](https://www.nber.org/papers/w7317)). Whatever the weight
scheme, it is allocating within a small explained share of daily variation.
Concentrating that allocation into one pillar buys no extra explanatory power. It
buys concentration risk on a single data pipeline, which is the exact hazard spec
section 8 constraint 2 caps at 0.40 for.

**The market's own weight on any single fundamental is not stable.** The
scapegoat literature finds that "the impact of macroeconomic variables on the
exchange rate changes over time", with a fundamental becoming a scapegoat when
it is out of line and the unexplained FX move is large. Empirical tests using
survey data measuring scapegoats directly for 12 exchange rates support the
theory ([ECB Working Paper 1418](https://www.ecb.europa.eu/pub/pdf/scpwps/ecbwp1418.pdf),
published in JME).

This reframes the question rather than answering it. If the market's loading on
rates is genuinely time-varying, then a single fixed weight is a compromise
across regimes and there is no true value of 0.30 or 0.40 to find. Searching for
the optimal fixed weight would be searching for a parameter that does not exist,
and any number found would be the average of the sample it was found on. That is
the definition of a curve fit under spec section 8's third warning sign.

### The bound

- **Not above 0.35.** The evidence supports rates as the largest single driver
  and does not support them as the only one. 0.40 is the spec's hard cap and
  sitting at a cap removes the headroom that cap exists to preserve.
- **Not below 0.25.** Below that MONETARY stops being clearly the largest weight
  and spec section 8 constraint 4 starts to bind.
- **0.30 sits in the middle of the supported range and is where it should stay.**
  It is a prior. It remains a prior.

### What is BLOCKED, and what would settle it

The residual question, whether 0.30 outperforms 0.35 in this specific model, is
BLOCKED. The measurement that would settle it is not a weight sweep, which would
fit noise. It is a single comparison that can be stated in advance:

> Does the seven-pillar composite ranking of the 28 pairs produce better
> forward outcomes than a MONETARY-only ranking of the same 28 pairs, over the
> same forward-recorded period?

That is decidable and it has only two outcomes. If MONETARY-only wins, the other
six pillars are not earning their weight and the answer to this question is yes,
0.30 is too low. If the composite wins, the mixture is doing work and the weight
stays.

**Sample size.** Be honest about what a forward record actually contains. 26
weeks of daily biases over 28 pairs is about 3,640 pair-days, and almost none of
them are independent. The pairs share eight underlying currency scores, and the
scoring spec's own bias horizon is 10 days, so consecutive daily observations
overlap almost entirely. The honest unit is a non-overlapping 10-day window, of
which 26 weeks supplies roughly 13, spread across perhaps four or five genuinely
independent currency stories. That is not enough to separate two weight sets.
Two years of forward record gives roughly 50 non-overlapping windows, which is
enough to answer the composite-versus-MONETARY-only comparison above and still
not enough to choose between 0.30 and 0.35.

### Recommendation

1. **Leave the weight at 0.30.** No change to `ScoringConfig`.
2. **Rewrite roadmap item 2.** "Phase 6 decides" is too optimistic and points at
   the wrong measurement. Replace it with the composite-versus-MONETARY-only
   comparison above, the sample-size statement, and the note that a weight sweep
   is explicitly not the test.
3. **Record the scapegoat point in the methodology.** The section on running a
   book and the re-weighting section of the spec both assume a stable underlying
   parameter. Published work says the market's loading on individual fundamentals
   moves. That is an argument for changing weights rarely, which is what spec
   section 8 already concludes, and it deserves to be stated as the reason rather
   than left implicit.

---

## Q11 (roadmap). Should the engine ever contradict the technical setup loudly?

*Source: `docs/roadmap.md`, Open questions, item 11.*

**Verdict: JUDGEMENT. The line is drawn in the right place. Keep it, and note
that the mechanism the question asks for already exists in a quieter form.**

There is no empirical answer to this. It is a question about what the tool should
be, and it should be decided as one.

### The engine structurally cannot do what the question describes

To warn that "a chart-based long runs against a high-conviction fundamental
short", the engine must be told there is a chart-based long. It has no price
bars, no pattern recognition, and no knowledge of what the trader intends.
Building the warning means adding an input where the trader declares the trade
before taking it, and an engine that is told the intended trade and responds is
one step from an engine that approves trades. `docs/methodology.md` states that
"the engine never produces an entry price, a target, or a time", and that if a
future version starts emitting levels "that is a bug in the design and not a
feature". A per-trade verdict on a declared trade is the same category of thing.

### The contradiction is already delivered, twice, in the right places

This is the part the roadmap question overlooks. The engine already contradicts
the setup, as a standing rule stated in advance rather than as an alert:

- `docs/methodology.md`, in the horizon-mismatch section: "Setups that contradict
  a MEDIUM or HIGH bias are skipped, not reversed."
- `docs/risk-and-execution.md` section 8, the pre-trade checklist, already
  carries the line: "Your technical direction matches the engine's bias. If it
  does not, you can still take it, but mark `agreed_with_bias` false so it is
  counted separately."
- `src/fbe/journal.py` already has the field. `agreed_with_bias: bool = True`,
  and `docs/risk-and-execution.md` already specifies counting the overrides.

So the contradiction reaches the trader at the moment of maximum temptation,
which is the checklist, and it is recorded so it can be scored later. What a loud
warning would add is volume, not information.

### Why volume is the wrong thing to add now

Two arguments, one cited, one internal.

**Algorithm aversion.** Dietvorst, Simmons and Massey find that people lose
confidence in an algorithmic forecaster faster than in a human forecaster after
seeing the same error, and become less likely to use it even when they have seen
it outperform
([Management Science / Wharton](https://marketing.wharton.upenn.edu/wp-content/uploads/2016/10/Dietvorst-Simmons-Massey-2014.pdf)).
A loud warning is memorable when it is wrong, and a quiet filter is not. This
model has measured nothing. A warning is a claim of authority, and issuing one
before Phase 6 spends credibility the engine has not earned. The follow-up
finding points the same way: people use imperfect algorithms considerably more
when they can modify the output
([Management Science 2018](https://faculty.wharton.upenn.edu/wp-content/uploads/2016/08/Dietvorst-Simmons-Massey-2018.pdf)).
That is an argument for handing over the pillar table and letting the trader
override with a reason, which is precisely the current design, and against
handing over a verdict.

**The horizon mismatch makes the warning wrong on purpose some of the time.**
`docs/methodology.md` is explicit: "A currency can be fundamentally strong for
three months and fall for nine consecutive days inside that period." A trade
against a MEDIUM bias, held for six hours, is not a mistake. It is the two clocks
disagreeing, which the methodology says they are supposed to do. An engine that
shouts on every such occasion would be shouting about its own known limitation.

### Recommendation

1. **Keep the line where it is.** No warning layer, no declared-trade input, no
   per-trade verdict. Roadmap item 11 can be closed as decided, with this
   reasoning as the record.
2. **Make the existing override path measurable rather than louder.** Add one
   line to the Phase 6 deliverables in `docs/roadmap.md`: `fbe evaluate` should
   report hit rate and average R separately for `agreed_with_bias` true and
   false. The field already exists in `src/fbe/journal.py`. Nothing needs
   building, only reporting.
3. **Revisit only on evidence, and state the trigger now.** If the evaluation
   shows override trades performing materially worse than with-bias trades, over
   at least 30 closed overrides, which is the same threshold
   `docs/risk-and-execution.md` already applies to the conviction ladder, then
   the case for a louder warning becomes an evidence-based one and can be
   re-opened. Until then it is an assertion. Adding the warning later is cheap.
   Un-asserting authority is not.

---

## Q12 (roadmap). Does the universe ever widen past G10?

*Source: `docs/roadmap.md`, Open questions, item 12.*

**Verdict: NARROWED. Not on a R2,000 account, and dealing spread is not the
reason. Four conditions are stated below, all four of which must hold, and the
first one is not close.**

### The binding constraint is lot granularity, not spread

`docs/risk-and-execution.md` section 7 already settles this and the roadmap item
does not reference it. At R2,000 with R20 at risk and a 25-pip stop, "several G10
crosses size below any retail broker's minimum lot", and the constraint
"dissolves on its own above roughly R10,000". The document's worked Example A is
one of the pairs that fails.

Going from 8 currencies to 10 takes the pair grid from 28 to 45, which is 17 new
crosses. Adding 17 rows to a table where some of the existing 28 cannot be sized
is not an expansion of the opportunity set. It is an expansion of the shortlist
the trader has to read past. The plan's own answer, "focus on major currency
pairs", points the other way.

### Spread is already handled and does not need a policy decision

Spec section 6 already contains the instrument:

    cost_ratio = (typical_spread_pips + commission_pips) / (atr_20d_pips * sqrt(10))
    blocked if cost_ratio > 0.05

That filter is currency-agnostic. If EURNOK's measured cost ratio clears 0.05 it
passes, and if it does not it is blocked, and nobody has to hold an opinion. The
roadmap frames SEK and NOK as excluded by a standing judgement about retail
spread cost. They are more usefully described as excluded by a measurement that
has not been taken.

That measurement cannot be taken from public sources with any confidence.
Advertised spreads for Nordic crosses vary by an order of magnitude across
brokers, and `docs/risk-and-execution.md` already instructs that broker values be
read off the owner's own contract specification and that spreads be sampled from
the owner's own terminal during the hours he actually trades. Any number quoted
here would be worse than the number the owner can produce himself.

### The cost that the roadmap item does not mention

This is the strongest argument and it is internal to the model. Adding a currency
is not an addition at the margin. It changes every existing score.

Spec sections 1.3 and 2.2 normalise every sub-indicator cross-sectionally over
the currencies present in the run. Going from `n = 8` to `n = 10` changes every
mean, every standard deviation, every z-score, every `sd(blend)` and therefore
every re-standardisation factor, every composite, all 28 existing spreads and
some of their conviction tiers. SEK and NOK are both small open European
economies and NOK is oil-linked, so they would not land symmetrically around the
existing mean, and NOK would become the second currency with a crude oil
`commodity_link`, changing the terms-of-trade z-score for CAD.

Spec section 8's sensitivity procedure is the right test for this, applied to the
universe rather than to the weights: re-run the last 26 weeks under `n = 8` and
`n = 10` and count how many pair-weeks change conviction tier. If more than
roughly 20% change, the model is more sensitive to universe composition than to
data, which is a finding in itself. That run is currently impossible because
`data/reports/` has no stored history.

Two secondary points. POSITIONING draws on CFTC Commitments of Traders data, and
whether CME lists a krone or krona contract with enough open interest for a
meaningful five-year z-score needs checking rather than assuming. If it does not,
both currencies score 0.90 coverage permanently, which clears the 0.80 demotion
and the 0.60 blocker but sits closer to both. And the full indicator registry
would need Riksbank and Norges Bank series for every key in
`docs/data-sources.md`, which is Phase 1 work for two currencies that cannot yet
be sized.

### The four conditions

All four must hold. Stating them now means the decision is made against a
standard rather than against an appetite.

1. **Account balance materially above R10,000**, so that the sizing constraint in
   `docs/risk-and-execution.md` section 7 has stopped binding on the existing 28
   pairs. This is the gate. Nothing else matters while it is shut.
2. **Measured `cost_ratio` below 0.05** for the specific SEK and NOK crosses the
   owner would actually trade, computed from spreads sampled on the owner's own
   terminal during his own trading hours over at least 20 sessions, and from the
   engine's own 20-day ATR. Not an advertised typical spread.
3. **Full registry coverage for both currencies**, such that neither sits below
   the 0.80 coverage demotion threshold in a normal run.
4. **A universe sensitivity run** under spec section 8's procedure showing fewer
   than roughly 20% of existing pair-weeks changing conviction tier when the
   universe goes from 8 to 10. This requires at least 26 weeks of stored reports
   and is therefore not available before Phase 5 has been running for half a
   year.

### Recommendation

1. **No change to `src/fbe/universe.py`.** `G10` stays at eight. The docstring
   there already points at `docs/roadmap.md` if it changes, which is correct.
2. **Rewrite roadmap item 12** with the four conditions above, and correct the
   stated reason for exclusion. SEK and NOK are not excluded because their
   spreads are known to be too wide. They are excluded because the account cannot
   size the pairs it already has, and because widening the cross-section
   re-prices every existing score, and the cost question is a measurement nobody
   has taken.
3. **Add a cross-reference** from the `universe.py` module docstring to section 7
   of `docs/risk-and-execution.md`, so the sizing constraint and the universe
   scope are visibly the same argument.
4. **Note the general principle in the methodology.** Any change to the set of
   currencies in the run changes the meaning of every score in it. That is a
   direct consequence of cross-sectional normalisation and it is not currently
   written down anywhere.

---

## Summary

| # | Question | Verdict | Recommendation in one line |
| --- | --- | --- | --- |
| Spec 1 | Inflation credibility, double-counting | ANSWERED | The gate does not double-count linearly; a larger opposing-sign overlap exists between INFLATION and `real_policy_rate` and should be documented. |
| Spec 1 | Inflation credibility, cleaner instrument | NARROWED | Breakevens eliminated on coverage and sign. The gate and the real-rate term are alternatives, not additions, and the choice waits on the sub-weight work. |
| Spec 7 | FX-specific carry pillar | ANSWERED | No. Carry's input is already 0.40 of MONETARY's sub-weight and its risk is already RISK. Close the item. |
| Spec 9 | Static currency metadata | ANSWERED | `inflation_target` and `commodity_link` are fine as constants. `risk_beta` is not. Report a rolling realised beta as a diagnostic without feeding it to the score. |
| Roadmap 2 | Is MONETARY at 0.30 too low? | NARROWED | Bounded to 0.25-0.35. Leave it at 0.30. Replace the roadmap's weight sweep with a composite-versus-MONETARY-only comparison. |
| Roadmap 11 | Contradict the technical setup loudly? | JUDGEMENT | No. The mechanism already exists quietly via the checklist and `agreed_with_bias`. Report the overrides instead. |
| Roadmap 12 | Widen past G10? | NARROWED | Not at R2,000. Four conditions stated; the account balance gate is the one that is not close. |

Nothing in this file is a measured result of this model. Six of the seven
verdicts above rest on published research about the FX market in general plus
arithmetic internal to the spec. The seventh is a preference, labelled as one.
Every weight in `ScoringConfig` remains a prior.
