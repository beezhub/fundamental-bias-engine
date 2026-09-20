# 0010. The registry supplies a scaled quantity's denominator, and a key never mixes global and per-currency refs

Status: Accepted

## Context

Three Phase 3 pillar requirements arrived on the same day with the same shape.
`docs/scoring-spec.md` specifies a component as a scaled quantity, and the
registry supplies only the numerator.

- **#157, EMPLOYMENT.** `employment_trend` is "the three-month average change in
  `employment_chg`, expressed as an annualised percent of the employment level".
  `employment_chg` is registered for eight currencies in `persons`. No
  employment level is registered anywhere.
- **#158, EXTERNAL.** `trade_trend` is the three-month change in the trade
  balance "in percent of GDP". `trade_balance` is registered for eight
  currencies in `usd`. `gdp_yoy` is a growth rate, not a level, and no nominal
  GDP level is registered.
- **#159, RISK.** `_extract` is specified to pull "the global equity and
  volatility series". `vol_index` has exactly one ref, keyed `GLOBAL`.
  `equity_index` has eight per-currency refs and no `GLOBAL` ref, so the
  drawdown half of the regime has no named series.

Each issue offered the same three kinds of escape: register the missing series,
reconstruct it inside the pillar, or score the unscaled quantity.

## Decision

**The registry supplies the denominator as its own canonical key.** A pillar
never reconstructs a missing scaling series from the underlying data, and never
scores an unscaled quantity cross-sectionally when the specification asks for a
scaled one.

- #157 registers `employment_level`.
- #158 registers `gdp_nominal_usd`, in US dollars so that it agrees with
  `trade_balance`'s unit without a conversion step.
- #159 registers the global equity reading under its **own key** with a single
  `GLOBAL` ref, rather than adding a `GLOBAL` ref to `equity_index`.

**A canonical key never holds both a `GLOBAL` ref and per-currency refs.** A key
whose refs are `GLOBAL` is a global reading; a key whose refs are per-currency is
a per-currency reading.

**Where a denominator cannot be verified for a currency, the component is absent
for that currency.** No fallback, no substitute, no reconstruction. The existing
absence path handles it: `blend_components` renormalises over the sub-weight
present, and `MIN_COMPONENT_WEIGHT` refuses the pillar when too little remains.

## Why

**Scoring the unscaled quantity ranks country size.** A US payrolls print in the
hundreds of thousands against a New Zealand quarterly change in the thousands, or
a dollar trade balance whose magnitude tracks the economy, both produce a
cross-sectional z-score that is mostly a ranking of how big the countries are,
with a little of the intended signal on top. Nothing raises, every value is in
range, and the ordering is stable and plausible, which is the combination this
project treats as the worst outcome.

**Reconstructing inside the pillar puts data knowledge in the wrong layer.**
`CLAUDE.md` draws the boundary at sources emitting canonical keys and pillars
knowing nothing about source vocabulary. A pillar that rebuilds a level has to
know which series the flow was differenced from, per currency, which is registry
knowledge. The cost is not theoretical: when a source changes which series backs
an indicator for one currency, the registry entry moves and the pillar's
reconstruction silently keeps the old shape.

**Self-scaling against the quantity's own history fails where it matters.**
#158's third option, expressing a change as a percent of the trailing balance,
divides by something several G10 trade balances cross zero on. Near zero the
ratio explodes and at zero it is undefined, so a balance moving from `-0.1` to
`+0.2` reads as a 300% change and arrives at the clip as a maximum reading.

**Mixing `GLOBAL` and per-currency refs on one key silently changes what the
coverage reports describe.** `registry.coverage_report` and
`identifier_coverage` both short-circuit on a `GLOBAL` ref:

    global_ref = spec.series.get(GLOBAL)
    if global_ref is not None:
        report[key] = 1.0 if usable else 0.0
        continue

So a key that gains a `GLOBAL` ref is reported on that ref alone and its
per-currency refs stop being counted, with `stale_refs` skipping the key
outright. A data addition would change what two reporting functions describe,
with nothing raising. Giving the global reading its own key avoids it, and keeps
the name honest: a value called the global regime should not be reached by asking
for one currency's series.

## Consequences

Three registry hunts, eight refs each for #157 and #158 and one for #159, all
`data-engineer`'s. They may not come back complete, and an incomplete hunt is a
coverage gap rather than a blocker, which is what the absence rule above is for.

EXTERNAL is the one to watch. Its components are 0.40, 0.30 and 0.30, so a
currency missing both `trade_trend` and `terms_of_trade` holds 0.40 and is scored
absent. The five currencies with no commodity link therefore depend on
`gdp_nominal_usd` landing to hold EXTERNAL at all.

For #159, `equity_index`'s eight per-currency refs become consumed by nothing and
are declared in `UNCONSUMED_INDICATORS` with the reason, as `yield_10y` already
is. `tests/test_registry_pillar_agreement.py` enforces that an indicator
attributed to a pillar is either in that pillar's `requires` or listed
unconsumed, so leaving them attributed and unread fails a test rather than
passing quietly.

If no free daily world equity index can be verified, `SP500` may be used under
the new global key, with its `description` stating that a US index is standing in
for the world. The substitution is then recorded at the point of use rather than
implied by a currency code.

As landed: `employment_level` (#157) and `gdp_nominal_usd` (#158) are registered
under those keys, and the global equity reading is `world_equity_index`, backed
by `SP500` with the stand-in stated in its description, which `RiskPillar`
consumes alongside `vol_index` (#159). `equity_index` is listed in
`UNCONSUMED_INDICATORS`.

This record is reopened if a denominator turns out to be unavailable for most of
the universe, which would be an argument about whether the component belongs in
the pillar rather than about where its denominator comes from.
