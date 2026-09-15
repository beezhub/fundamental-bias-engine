# Roadmap

From the current skeleton to a v1 that produces a daily G10 bias the owner can
actually trade off. Phases are ordered so that each one is useful on its own
and each one proves something the next depends on.

Nothing here is a schedule. There are no dates, because the only honest
sequencing constraint is what has to work before the next thing can.

## Phase 0: contracts and documentation

**Status: done.**

**Goal.** Fix the vocabulary and the direction of data flow before any
algorithm exists, so that six workstreams can proceed without renegotiating the
interfaces.

**Deliverables.**

- `src/fbe/types.py`: `Observation`, `PillarScore`, `CurrencyScore`, `PairBias`, `CalendarEvent`, `PositionSize`, `TradeIdea`, `BiasReport`, the `DataSource` and `Pillar` protocols, and the `Direction` / `Conviction` / `PillarName` / `Frequency` enums.
- `src/fbe/universe.py`: G10 set, `CurrencyMeta` per currency, `ALL_PAIRS` in market quoting convention, `pair_name`, `split_pair`, `meta`.
- `src/fbe/config.py`: `RiskConfig`, `ScoringConfig`, `DataConfig`, `Config.digest()`, `Config.validate()`, `default_config()`.
- `pyproject.toml` with ruff, mypy, and pytest configured.
- `docs/methodology.md`, `docs/scoring-spec.md`, `docs/data-sources.md`, `docs/risk-and-execution.md`, `docs/interfaces.md`, `docs/trading-plan.md`.
- CI running lint, format, types, and tests on 3.11 and 3.12.

**Definition of done.**

- [x] `python3 -m pytest` passes with real tests over `universe` and `config`.
- [x] Weights sum to 1.0 and `Config.validate()` returns empty for defaults.
- [x] `ALL_PAIRS` has 28 entries, correct quoting convention, no self-pairs.
- [x] Every stub raises `NotImplementedError`, not a fabricated value.

## Phase 1: the data layer

**Goal.** Get real numbers into `Observation` objects, cached, reproducible,
and provably correct against the source.

**Deliverables.**

- `src/fbe/datasources/base.py`: the shared source behaviour, retries, and error types.
- `src/fbe/datasources/cache.py`: on-disk cache under `data/cache/`, keyed by source and request, honouring `DataConfig.cache_ttl_hours` and `offline`.
- `src/fbe/datasources/fred.py`: FRED client covering policy rates, 2y and 10y yields, CPI, core CPI, GDP, unemployment, and the trade balance for all eight G10 economies.
- `src/fbe/datasources/registry.py`: the canonical indicator registry mapping keys such as `yield_2y` and `cpi_yoy` to a source series per currency.
- `src/fbe/datasources/prices.py`: spot and cross rates from Stooq or Yahoo.
- `src/fbe/datasources/manual.py`: YAML loader for series with no free API.
- `fbe doctor` reporting source reachability and cache state.

**Definition of done.**

- [ ] Every indicator key in the registry resolves to a live series for every currency that should have one, and the gaps are listed explicitly rather than silently empty.
- [ ] `fbe refresh` populates `data/cache/` and a second run inside the TTL makes zero network calls, verified by a test with the network mocked.
- [ ] `DataConfig(offline=True)` never touches the network, and fails loudly when the cache is cold rather than returning an empty set.
- [ ] For at least three series, a spot-check test asserts a specific known value against the published number, so a silent unit or scale change breaks the build.
- [ ] `released_at` is populated wherever the source publishes it, because Phase 6 cannot avoid look-ahead bias without it.

## Phase 2: the MONETARY pillar, end to end

**Goal.** One complete vertical slice: source to cache to pillar to score to
pair bias to a rendered table. Monetary is the right slice because rate
differentials carry most of the signal in G10 FX, so a monetary-only engine is
already worth reading, and because proving one pillar through the whole
pipeline de-risks the other six.

**Deliverables.**

- `src/fbe/pillars/base.py`: the shared pillar machinery, cross-sectional normalisation, clipping, staleness accounting, coverage.
- `src/fbe/pillars/monetary.py`: scores from the short-end yield differential, with the policy rate as fallback.
- `src/fbe/scoring.py`: weighted aggregation into `CurrencyScore`, including `dispersion` and `coverage`.
- `src/fbe/bias.py`: differencing into `PairBias`, with `Direction` and `Conviction` from spread, agreement, and freshness.
- `fbe bias` printing a ranked currency table and a pair table to the terminal.

**Definition of done.**

- [ ] `fbe bias` runs from cold cache to a printed table for all 28 pairs without a manual step.
- [ ] Every `PairBias` satisfies `spread == base_score - quote_score` to floating-point tolerance, tested.
- [ ] Inverting a pair inverts the direction and negates the spread, tested, because a silent inversion is the single most dangerous bug this codebase can have.
- [ ] A currency missing its yield data produces `coverage < 1.0` and a warning, never a score of zero passed off as neutral.
- [ ] Conviction bands respond to `ScoringConfig.min_spread_*` thresholds, tested at each boundary.
- [ ] The output is sane by inspection: the currency with the highest short rate ranks near the top on this pillar.

## Phase 3: the remaining six pillars

**Goal.** Fill out `INFLATION`, `GROWTH`, `EMPLOYMENT`, `EXTERNAL`,
`POSITIONING`, and `RISK` against the interface Phase 2 proved.

**Deliverables.**

- One module per pillar under `src/fbe/pillars/`.
- `src/fbe/datasources/cot.py` for CFTC Commitment of Traders, which only `POSITIONING` needs.
- Per-pillar documentation in `docs/scoring-spec.md`: inputs, transform, sign convention, known weaknesses.

**Definition of done.**

- [ ] All seven pillars produce a score for all eight currencies, or a documented reason they cannot.
- [ ] Sign conventions are tested individually. Inflation above target is positive for the currency because it implies tightening; a widening trade deficit is negative. Each sign has a one-line justification in the docstring and a test.
- [ ] `dispersion` is non-zero and meaningful, meaning the pillars genuinely disagree sometimes. If they never disagree, they are measuring the same thing and the weights are fiction.
- [ ] Pillar cross-correlation is computed and recorded. Two pillars correlated above 0.9 across the cross-section are double-counting and one gets folded or reweighted.
- [ ] No pillar silently returns 0.0 on missing data.

## Phase 4: risk sizing and the calendar guard

**Goal.** Connect the bias to the two parts of the trading plan a machine can
enforce: the 1-2% risk cap and the high-impact news blackout.

**Deliverables.**

- `src/fbe/risk.py`: `PositionSize` from entry, stop, balance, and risk fraction, in ZAR, handling the cross-rate conversion for non-USD-quoted pairs.
- `src/fbe/calendar_guard.py`: blackout windows either side of high-impact releases, setting `tradeable=False` and populating `blockers`.
- `src/fbe/datasources/calendar.py`: the Forex Factory calendar feed.
- `src/fbe/journal.py`: append-only trade log with bias, conviction, size, config digest, and outcome.

**Definition of done.**

- [ ] Position sizing is verified by hand against three worked examples across a USD-quoted pair, a JPY pair, and a cross, including the ZAR conversion.
- [ ] Sizing refuses rather than rounds when the minimum tradeable size would exceed the risk cap on a R2,000 account. Refusing is correct behaviour here, not a limitation.
- [ ] `Config.validate()` rejects any risk cap above 2%, tested.
- [ ] A pair with a high-impact release inside the window on either leg is marked not tradeable and names the event in `blockers`.
- [ ] Journal entries round-trip through disk and can be filtered by pair, direction, and conviction.

## Phase 5: report and dashboard

**Goal.** Make the output something the owner reads every morning as part of
the routine in `docs/trading-plan.md`, rather than something they have to run a
command to remember.

**Deliverables.**

- `src/fbe/report.py`: `BiasReport` serialised to JSON under `data/reports/`, carrying the config digest.
- `src/fbe/dashboard/`: static HTML, currency ranking, the pair grid, per-pillar breakdown on click, calendar blackouts marked.
- `fbe report` and `fbe dashboard` commands.

**Definition of done.**

- [ ] Every rendered number traces to a field on a dataclass. Templates compute nothing.
- [ ] The dashboard shows its working: each pair's score can be expanded to the seven pillar contributions and the observations behind them.
- [ ] Stale and missing data are visible on the page, not hidden. A pair at 60% coverage says so.
- [ ] The config digest appears on the report, so an old report can be tied to the weights that made it.
- [ ] The dashboard is readable on a phone, because the morning routine does not happen at a desk.
- [ ] No text on the page claims a measured edge.

## Phase 6: journal-based evaluation

**Goal.** Find out whether any of this works. This is the first phase that
produces evidence rather than opinion, and until it has run for long enough,
the honest description of the model is "untested".

**Deliverables.**

- Forward-record every daily bias to `data/reports/`, whether or not a trade follows.
- Join the journal's realised trade outcomes to the bias that was live at entry.
- `fbe evaluate`: hit rate and average return by direction, by conviction band, and by pillar agreement level; the same for the trades that were skipped.
- A weight-sensitivity run: how much does the pair ranking move when each weight is perturbed.

**Definition of done.**

- [ ] At least six months of forward-recorded daily biases exist, generated before the outcome was known. Backfilled biases do not count and are excluded by construction, which is what `released_at` on `Observation` is for.
- [ ] Hit rate is reported per conviction band with a confidence interval, and the interval is stated, not just the point estimate.
- [ ] The evaluation can conclude that the model has no edge. If the analysis has no way of returning that answer, it is not an analysis.
- [ ] Weight changes made after this phase cite the evaluation output in the commit body.

## How the model gets validated

Every weight in `ScoringConfig` today is a prior. It is reasoned from the
relative-value framework in `docs/methodology.md` and from the observation that
rate expectations dominate G10 FX over a multi-day horizon. It is not a
finding. No part of this model has been fitted, backtested, or measured against
out-of-sample returns, and no file in this repository may say otherwise.
That restriction is recorded in `CLAUDE.md` and it holds until Phase 6 produces
data.

Validation happens in three stages, in this order.

**1. Correctness, from Phase 1.** Does the code compute what it claims to
compute? Spot-check tests against published source values, pair inversion
tests, sign-convention tests per pillar, and arithmetic identities such as
`spread == base_score - quote_score`. This catches bugs, not bad ideas.

**2. Coherence, from Phase 3.** Does the output behave the way the framework
says it should? The highest-yielding currency should score well on `MONETARY`.
A risk-off week should push JPY and CHF up on `RISK`. Pillars should disagree
sometimes, and pillars that never disagree are measuring the same variable
twice. This catches bad ideas that are internally inconsistent. It does not
establish that the model predicts anything.

**3. Predictive value, from Phase 6.** Does a high-conviction long bias
actually precede the base currency outperforming the quote currency? This is
the only stage that can answer the question, and it requires forward-recorded
biases, because a backtest built on today's revised macro data is measuring a
world the trader never saw. `Observation.released_at` exists specifically so
that a later backtest can be built without look-ahead bias, but forward
recording is the primary evidence and the backtest is a supporting check.

Two rules for the whole exercise:

- **Sample size before conclusions.** A 60% hit rate over 20 trades is noise. Report the confidence interval every time, and do not reweight on a handful of observations.
- **No fitting to the owner's own trade history.** The journal has few enough entries that fitting weights to it would produce a model that describes the past perfectly and predicts nothing. Evaluate against the bias record for all 28 pairs, not just the pairs that were traded.

## Open questions

Recorded so they are decided deliberately rather than by accident.

Each question carries its status and points at the evidence behind it. A
verdict recorded here has not been applied to the code: section 10 of
`docs/scoring-spec.md` governs what is applied and when, and nothing below
may be resolved in code without updating that section first. The files under
`docs/answers/` are dated snapshots of the investigations, not descriptions of
the repository.

**Scoring and weights**

1. Should normalisation be purely cross-sectional (a currency versus the other seven today) or blended with a time-series z-score over `lookback_years`? Cross-sectional is the framework's logic and needs no history. Time-series catches the case where the whole G10 tightens together. The current design is cross-sectional and this is unresolved.
   **Status: answered, negative.** The hybrid was designed and rejected. On
   a universe-wide move the only thing a time-series leg adds to a pair
   spread is a term in the ratio of the two currencies' historical
   volatilities, which ranks them by the quietness of their own history, and
   it would cost `lookback_years` of history for every indicator. Section
   2.2 of the scoring spec stands. Evidence: `docs/scoring-spec.md` section
   10 item 4, `docs/answers/scoring-maths.md` Spec 4 and Roadmap 1.
2. Is `MONETARY` at 0.30 too low? If rate differentials carry most of the signal, an argument exists for 0.40 or higher and for treating the other six as tiebreakers. Phase 6 decides.
   **Status: narrowed, residual blocked on trade data.** Published work
   bounds the weight to 0.25-0.35 and cannot supply a number, so 0.30 stays.
   The test that would settle the residual is a comparison of the
   seven-pillar ranking of the 28 pairs against a MONETARY-only ranking on
   the forward record, not a weight sweep, and it needs roughly two years of
   record. Evidence: `docs/answers/framework.md` Q2 (roadmap).
3. Should the expected policy path be scored separately from the current setting? The 2y yield mixes both. Splitting them needs a forward-curve source that is free, which may not exist.
   **Status: answered on the data, scoring call not taken.** A free forward
   curve exists and was fetched live for EUR, GBP, AUD and USD, and does not
   exist free for CAD, JPY, CHF and NZD. Whether a 4-of-8 `policy_path`
   sub-indicator is worth adding under `MIN_COMPONENT_WEIGHT` is a scoring
   decision. Evidence: `docs/answers/data.md` question 3,
   `docs/scoring-spec.md` section 10 findings.
4. `RISK` uses static `risk_beta` values in `CurrencyMeta`. Those betas move with the regime. Estimate them rolling, or accept a static approximation and document the error?
   **Status: answered, with a remainder tracked separately.** Keep the static
   betas: a rolling estimate describes the regime that just ended, and in
   simulation it beats the constant only once the true beta's drift exceeds
   roughly 0.25 of the `-1..+1` band. Any rolling beta must be measured
   against a per-currency index, never a dollar pair. The two answer
   documents reach different headline positions on the sign-flip case,
   which is [#22](../../issues/22): costed on the section 7 fixture and
   answered as far as the evidence allows, with the rolling-beta diagnostic
   pending as a proposal. Evidence: `docs/scoring-spec.md` section 10 item
   9, `docs/answers/framework.md` Q9, `docs/answers/scoring-maths.md`
   Roadmap 4.

**Data**

5. No free source covers G10 PMIs completely. Options: `data/manual/` YAML upkeep, drop PMI from `GROWTH`, or accept uneven coverage across currencies and let `coverage` reflect it.
   **Status: answered.** The OECD Business Tendency Surveys dataflow carries
   a composite business confidence balance for all eight currencies, fetched
   live: monthly for USD, EUR, GBP and CHF, quarterly for JPY, CAD, AUD and
   NZD. It is a percentage balance centred on zero, not a diffusion index
   centred on 50, so it needs its own indicator key. The registry entry is
   proposed in [#6](../../issues/6); whether GROWTH takes it, given the
   quarterly half, is [#23](../../issues/23). Evidence:
   `docs/answers/data.md` question 5, `docs/scoring-spec.md` section 10
   findings.
6. CFTC COT is published Friday for the prior Tuesday. That is three days stale on arrival and up to ten by the next release. Does `POSITIONING` earn 0.10 with that lag?
   **Status: narrowed on the lag, blocked on the weight.** How much of the
   signal survives the lag is a function of weekly persistence, measurable
   from CFTC history alone once `datasources/cot.py` lands. Whether the
   pillar earns 0.10 needs COT history joined to forward returns, roughly
   seven years of it. The shape function accounts for more of the pillar's
   shortfall against 0.10 than the lag does. Evidence:
   `docs/answers/scoring-maths.md` Roadmap 6.
7. The Forex Factory calendar has no official free API. If scraping breaks, is there a fallback, and does the blackout fail open or closed? Failing closed means missing trades; failing open means trading into an NFP print.
   **Status: narrowed, fail direction open.** Working fallbacks exist per
   country: central bank rate decision calendars for all eight and the ONS
   release calendar for GBP, while the BLS returns 403 to automated requests
   and no single free feed covers all eight, so a fallback is eight to ten
   small scrapers rather than one. Whether the blackout fails open or closed
   is [#24](../../issues/24), owned by the execution desk. Evidence:
   `docs/answers/data.md` question 7.
8. GDP revisions can be large. Score the first print, the latest vintage, or both?
   **Status: answered.** Revisions are large enough to move the
   cross-sectional ranking. Score the latest vintage for the live run, which
   is what the registry does; first-print data is for a backtest, which is
   what `Observation.released_at` and `revision` exist for. Evidence:
   `docs/answers/data.md` question 8, `docs/scoring-spec.md` section 10
   findings.

**Product**

9. What is the right refresh cadence? Daily fits the routine in the trading plan, but most of these series are monthly, so most days the bias will not move. Does a daily run that says "unchanged" build discipline or invite banner blindness?
   **Status: answered, one sub-question blocked on usage data.** Daily. The
   plan's morning routine fixes the cadence, and the calendar changes every
   day even when no composite does. Whether a daily "unchanged" run builds
   discipline or invites blindness is about the owner's behaviour and waits
   on the journal. Evidence: `docs/answers/product.md` Roadmap 9.
10. Should the engine suppress a pair entirely when conviction is `NONE`, or show it greyed out? Hiding is cleaner; showing proves the engine considered it and reduces the temptation to re-derive it by hand.
   **Status: answered.** Show it. A pair silently missing is
   indistinguishable from a pair the engine never scored. NONE pairs stay in
   the list and the matrix and sort to the bottom; the trader shortens the
   list with `--min-conviction` or `--tradeable-only`. Evidence:
   `docs/answers/product.md` Roadmap 10.
11. Should the engine ever contradict the technical setup loudly, for instance warning when a chart-based long runs against a high-conviction fundamental short? That edges from bias into advice, which is out of scope as currently drawn.
   **Status: judgement.** No. The line stays where it is. The contradiction
   already reaches the trader through the checklist and `agreed_with_bias`
   in the journal, and `fbe evaluate` should report overrides separately
   rather than warn louder. Revisit only if at least 30 closed overrides
   perform materially worse than with-bias trades. Evidence:
   `docs/answers/framework.md` Q11 (roadmap).
12. Does the universe ever widen past G10? SEK and NOK are the obvious candidates and were excluded on retail spread cost. Revisit only if the account grows enough for the spread to stop mattering.
   **Status: narrowed.** Not on a R2,000 account, and dealing spread is not
   the binding reason: the account cannot size the pairs it already has, and
   widening the cross-section re-prices every existing score. Four
   conditions are stated in the evidence, all of which must hold, and the
   account balance gate is the one that is not close. Evidence:
   `docs/answers/framework.md` Q12 (roadmap).
