# Backlog audit

An architect pass over the whole repository, opening the issue backlog from
nothing. Every candidate below was checked against the current files before it
was filed or dropped. The repository had zero issues when this started and 20
when it finished, numbered 7 to 26; issues 2 to 6 are the product analyst's
proposals, filed in parallel.

The limit was 20 issues. Everything found is listed here, ranked, including the
items that were deliberately not filed and why, so nothing is lost.

## How the ranking works

Priority follows `.github/labels.yml`. `p0` means a wrong number is reaching the
trader now, and nothing qualifies: three modules are implemented and the pipeline
does not run end to end. `p1` is the ceiling in force, meaning wrong behaviour
that is not yet reaching anyone. Within a priority, the ranking is by how much of
the model the defect moves and by how invisible it is when it fires.

## Filed

### p1

| # | Type | Area | Finding |
| --- | --- | --- | --- |
| [#7](../../issues/7) | defect | data | `registry.INDICATORS` disagrees with `Pillar.requires` and the spec section 3 tables on seven keys, and two more keys the monetary pillar requires have no registry entry. `series_for` raises on the first one touched; if a runner skips instead, RISK, POSITIONING and EXTERNAL are absent for all eight currencies and coverage lands at 0.70 universe-wide, which demotes all 28 pairs one tier and says nothing. |
| [#8](../../issues/8) | defect | scoring | A quarterly print stamped at its quarter start is about 120 days old on publication against `max_staleness_days` of 45, so it never clears the ramp. INFLATION has zero effective weight for AUD and NZD on every run, both of whose CPI series are quarterly. `IndicatorSpec.max_staleness_days` claims to override the global value and has no path into `scoring.freshness`. The reverse also holds: `staleness_days` is one scalar per pillar, so GROWTH carries a 162-day-old GDP print at its full 0.30 sub-weight because retail sales updated. |
| [#9](../../issues/9) | defect | scoring | `real_policy_rate` places a coefficient of minus one on headline CPI inside MONETARY against INFLATION's positive loading, cancelling 49.7% of the model's inflation response when headline moves alone and 18.7% when headline and core move together. Ruled in [ADR 0003](decisions/0003-publish-effective-loadings-rather-than-remove-real-policy-rate.md). |
| [#10](../../issues/10) | defect | infra | `Config.digest()` hashes the whole config, so it changes when the account balance moves, when the FRED key rotates, and between machines because `REPO_ROOT` is embedded. `docs/interfaces.md` says a digest change makes two runs "not comparable", so `--compare` will suppress the diff after every closed trade. Reproduced: `476f0a31c62f` against `682810c3090a` for identical weights and a balance of R2,000 against R2,137.50. |

### p2

| # | Type | Area | Finding |
| --- | --- | --- | --- |
| [#11](../../issues/11) | defect | scoring | `types.py` still describes dispersion as an unweighted standard deviation across pillar scores and agreement as a headcount, both superseded by `aa6705d`. Worked case in the issue: 0.9640 by the contract against 1.0705 by the code, on a threshold of 1.20. |
| [#12](../../issues/12) | defect | scoring | POSITIONING emits at a standard deviation near 0.5885 so it buys about 0.059 against a declared 0.10, and section 3.6 does not say so. RISK is worse and nobody has written it down at all: `sd(risk_beta) = 0.6364`, so its effective influence runs from 0.000 in a calm market to 0.127 at maximum risk-off, below section 8's 0.05 deletion floor on any run where `abs(R) < 0.393`. |
| [#13](../../issues/13) | defect | scoring | Spec sections 5.4 and 6 write `0.80`, `1.20`, `0.05` and `0.60` where `ScoringConfig` fields hold them, and section 4.1 derives `s0 = S / 3` where `staleness_full_days` is an independent field. At `max_staleness_days = 30` the two readings give a 0.30 pillar an effective weight of 0.150 or 0.200 for the same input. |
| [#14](../../issues/14) | defect | scoring | `bias.shortlist(limit: int = 3)` hardcodes `RiskConfig.max_concurrent_positions` while its own docstring names the coupling. Lowering the cap to 2 hands the trader three ideas and refuses the third at the ticket. |
| [#15](../../issues/15) | defect | scoring | `MIN_COMPONENT_WEIGHT` is 0.5 and the test is `<`, so EMPLOYMENT's two 0.50 components leave exactly 0.50 present and the floor can never fire for the one pillar whose docstring says "neither guards against the other's failure mode on its own". |
| [#16](../../issues/16) | defect | infra | `Config.validate()` accepts `staleness_full_days` above `max_staleness_days`, which makes the section 4.1 ramp denominator negative and two branches both apply; `coverage_demotion` below `min_coverage`, which makes the demotion unreachable; spread thresholds out of order, which deletes the LOW tier and turns an R20 trade into an R30 one; and a negative pillar weight that still sums to 1.0. |
| [#25](../../issues/25) | defect | risk | `RiskConfig` says its limits are "taken directly from the trading plan". Four of them are not in the plan: the three-position cap, the 4% correlated exposure cap, the 4% daily loss limit and the 10% drawdown pause. All four are well argued in `docs/risk-and-execution.md` section 4, but `CLAUDE.md` makes the plan win any conflict, so a derived limit claiming plan authority cannot be argued with. |
| [#26](../../issues/26) | defect | data | `docs/data-sources.md` says CHF and NZD monetary scores "are built from policy rate and 10-year yield alone", in the same paragraph as saying they "lose the pillar outright". Both cannot be true, and MONETARY has no 10-year term at any sub-weight. `yield_10y` is registered to MONETARY at 100% coverage and consumed by nothing. |
| [#18](../../issues/18) | debt | docs | `docs/roadmap.md` still poses twelve open questions that `docs/answers/` and spec section 10 settled, with no pointer to either. `docs/reasoning-layer.md` open question 1 likewise. |
| [#19](../../issues/19) | debt | scoring | Section 7 of the scoring spec is declared a fixture by section 9 and has no test. 418 lines of published arithmetic across every stage, already confirmed reproducible, with nothing pinning it to the code. This is the single highest-value test available in the repository. |
| [#22](../../issues/22) | question | scoring | The two answer documents on `risk_beta` were merged in spec item 9 without evaluating the case that motivated the item. `scoring-maths.md`'s simulation is an OU process centred on the static value and cannot produce a sign flip; `framework.md`'s evidence is a sign flip. At a beta error of 1.0 and `R = -1.0` the worst pair spread error is 0.4000, which is 53% of the neutral band on all seven dollar pairs at once, against a published table topping out at 0.2000. |
| [#23](../../issues/23) | question | scoring | Whether GROWTH takes the OECD business confidence proxy, given four of the eight series are quarterly and therefore structurally absent under the current staleness rule. Blocked behind #8 rather than behind data. |
| [#24](../../issues/24) | question | execution | Whether the calendar blackout fails open or fails closed. `docs/answers/data.md` question 7 settles the fallback half and returns NARROWED on this. Execution desk's call. |

### p3

| # | Type | Area | Finding |
| --- | --- | --- | --- |
| [#17](../../issues/17) | defect | scoring | `apply_filters` emits `no_edge`, `event:unchecked` and `cost:unchecked`, none listed in spec section 6, and both renderers print `blockers` only when `tradeable` is false, so an unchecked calendar prints `yes` on all 28 rows. |
| [#20](../../issues/20) | debt | infra | `CLAUDE.md` tells the reader a stub raises `NotImplementedError` "with a message pointing at `docs/roadmap.md`" and names `load_config()` as the reference. 116 stubs raise bare, 14 carry a message, none of the 14 mentions the roadmap, and `load_config()` is the only instance in the package. |
| [#21](../../issues/21) | debt | interface | `report.py` says `data/reports/` is gitignored and points at `docs/interfaces.md`. It has been tracked since `9ae45ff` and `docs/interfaces.md` says the opposite. |

## Architecture decision records written during this pass

- [ADR 0002](decisions/0002-representing-not-known.md). Absence is represented
  explicitly, never as a passing value. Generalises three separate proposals,
  issues #2, #3 and #4, which are one defect in three coats: an empty calendar
  reads as no blackout, empty position state reads as limits cleared, and an
  unconfirmed broker constant reads as a confirmed one. Settles the convention
  without settling any of the three, and without touching the approval gate.
- [ADR 0003](decisions/0003-publish-effective-loadings-rather-than-remove-real-policy-rate.md).
  The ruling on issue #9. Change no pillar and publish the effective loadings.
  Recorded as an arbitrary choice between two defensible constructions, because
  that is what it is.

## Found and deliberately not filed

Ranked by how close each came to being filed.

**`MIN_REWARD_TO_RISK` lives in `risk.py` rather than `RiskConfig`.** The
engineering standards say every threshold lives in `ScoringConfig` or
`RiskConfig`. Not filed: the rule exists because a number in two places will
disagree silently, and this number is in one place. Its docstring already names
the assumption, says it is untested, and names the measurement that would confirm
or refute it, which is more than moving it to config would achieve. Revisit if
anything else starts holding a reward-to-risk minimum.

**`DEFAULT_BROKER` carries invented spreads and lot sizes.** Not filed as a
defect. The docstring shouts that the owner must confirm every value and explains
what goes wrong in both directions. The real gap is that nothing records whether
they were confirmed, which is proposal #4 and is covered by ADR 0002 rule 1.

**`dashboard/build.py` computes `bar_pct`, `heat` and `at_pct` for the
template.** Invariant 5 says renderers compute nothing. Not filed: these are
presentational scalings rather than model numbers, the module docstring argues
the case explicitly, `check_constraints` enforces the output, and the alternative
puts arithmetic in a template where it cannot be tested. A judgement call that
went the right way.

**Spec section 3.6 says POSITIONING is "the only pillar using time-series rather
than cross-sectional normalisation".** RISK also bypasses the cross-sectional
step, though it uses neither time-series nor cross-sectional normalisation on its
own output, so the sentence is defensible as written. Not worth an issue on its
own; folded into #12, which touches both sections anyway.

**`pytest-cov` is in the dev extras and CI runs no coverage step.** Trivial, and
adding a coverage gate to a package that is mostly stubs would produce a number
nobody should act on.

**`yield_10y` is registered and unconsumed.** Filed, as the second half of #26,
rather than separately.

**Spec section 10 item 10, intervention and capital controls.** Recorded as
"open, unworked" with no evidence in either direction, and none of the four
specialist passes took it up. Not filed: an issue restating that a question is
open adds nothing the spec does not already say, and the scope call belongs to
the macro strategist.

**`Config.digest()` imports `hashlib` and `json` inside the function body.**
Style only. Not worth a line of anyone's time.

**Several recommendations in `docs/answers/*.md` have not been applied.**
Deliberately not filed. Spec section 10 states that nothing in those files may be
silently resolved in code and that recording a status is not resolving a
question. Filing "recommendation not applied" as a defect would invert that rule.
The two exceptions filed above, #12 and #22, are filed for a different reason:
#12 because the figure is missing from the section a reader actually consults,
and #22 because the merge itself skipped the case.

## Verified as already fixed, not filed

A code review earlier in this project raised 19 findings and most were fixed.
These were checked and are no longer present.

- **Spec section 2.3 and `blend_divisor` disagree about the divisor.** Reported
  by the product analyst pass and confirmed stale. Section 2.3 now describes the
  rolling median the code implements, and `5415b9d` records the correction and
  the recomputation of the worked example. This is the reason
  `docs/answers/README.md` now says those files are dated snapshots.
- **`data/reports/` is gitignored.** Fixed in `9ae45ff`. Only the stale docstring
  survives, which is #21.
- **`PillarScore` has nowhere to carry a cross-section diagnostic.** Fixed in
  `d2d95ec`. The field exists and documents `emit_sd` and `contamination`.
  Nothing populates it, which is part of #12.
- **`PillarScore.weight` does not say which stage its value came from.** Fixed in
  `5bd3649`.
- **`PositionSize.notional` sits in the quote currency, and the journal records
  intended rather than realised risk.** Fixed in `5bd3649` and `4fc767b`.
- **The risk ladder hardcodes 1% and 2%.** Fixed in `e830e2d`.
  `CONVICTION_BAND_POSITION` now holds a position in the band and interpolates.
- **Extraction filters on period rather than release date.** Fixed in `57d8608`.
  The visibility and vintage rules are specified in `BasePillar._extract`.
- **The mirrored half of the pair matrix has no owner for the sign flip.** Fixed
  in `57d8608`.
- **`lookback_years` separated from its docstring.** Fixed in `da78f90`,
  reachable as `da80e78`.

## Could not verify either way

Stated plainly rather than filed as findings.

**Whether the section 7 worked example still reproduces.** `5415b9d` says it was
recomputed from its raw inputs and that no figure moved, and
`docs/answers/scoring-maths.md` says the reconstruction reproduced every
published figure exactly. Reproducing 418 lines of arithmetic by hand was outside
this pass. That is precisely why #19 exists: the claim is currently unfalsifiable
by anyone who was not in the conversation.

**Whether the registry's `last_observed` dates are still accurate.** They are
aged against a fixed `VERIFIED_ON` and this audit made no network calls, so every
coverage figure in `docs/data-sources.md` is a claim about the day the registry
was checked. `coverage_report`'s own docstring says the same and advises
re-verifying when the two drift apart. The arithmetic in #8 does not depend on
the dates being current, only on the frequencies and the period stamping.

**Whether the OECD Business Tendency Surveys codes in `docs/answers/data.md`
resolve as described.** No network calls, so the four monthly and four quarterly
split in #23 is taken from that document rather than confirmed. If it is wrong in
the direction of more monthly coverage, #23 gets easier and does not go away.

**Whether `min_lot` of 0.01 is the owner's real broker constraint.** Unknowable
from inside the repository, which is the point of proposal #4.

**The claim in `docs/answers/framework.md` that a rolling `risk_beta` "would only
have turned the sign some weeks after the episode".** Asserted rather than
measured, and it is the load-bearing claim behind rejecting two of the four
options in #22. Named there as measurement three.

## Notes for whoever reads this next

Three of the four `p1` items, #7, #8 and #10, share a shape: two modules hold the
same fact and disagree, and neither raises. The registry and the pillars disagree
about a key. The registry and the scorer disagree about how old is too old. The
digest and its consumers disagree about what it covers. That is worth watching
for as the remaining layers land, because each of the three was invisible until
two files were read side by side, and none of them would fail a naive test.

The single cheapest thing on this list is #19. Section 7 is the only end-to-end
statement of the arithmetic in the repository, it is already known to reproduce,
and until a test pins it the spec and the code can drift apart again the way they
did once already.
