# 0004. The yield change window ends at the latest session, and an observation carries its indicator's unit

Status: Accepted

## Context

Build desk A asked for a ruling on issue #56 on 2026-09-12 and got no answer for
two days. The same two questions then blocked #58 (merged as PR #104) and #59
(merged as PR #107). All three merged with `Refs:` rather than `Closes:`, and
`yield_2y_chg_1m` and `yield_2y_chg_3m` have no source that emits them anywhere:
`FredSource` refuses them, `CurvesSource` refuses them, and every curve provider
is now implemented, so there is no later pull request to defer to.

Both questions come out of one sentence in the `TRANSFORMS` docstring in
`src/fbe/datasources/registry.py`:

> `chg_1m` and `chg_3m` mean the source publishes a percent level and the caller
> must resample it to month-end (or quarter-end for the three-month case),
> difference over one or three periods, and rescale the percentage-point result
> into basis points by multiplying by 100.

The parenthetical says quarter-end with a one-period difference for the
three-month case. The phrase after it says one or three periods, which only
makes sense if both resample to month-end. The two halves of the sentence
disagree, and the reader has no way to tell which half is the specification.

**What is at stake.** `yield_2y_chg_3m` carries 0.25 of MONETARY's sub-weight
and MONETARY carries 0.30 of the composite, so it is 0.075 of the composite
score and the single heaviest sub-indicator in the model. `yield_2y_chg_1m` adds
0.06. The two together are 0.135 of the composite, more than the INFLATION
pillar in full.

**What this record does not claim.** Nothing in this project has been measured
against out-of-sample returns. The sub-weights are priors, section 3.1 of
`docs/scoring-spec.md` reasons for them and does not evidence them, and the
window chosen below is a prior too. What separates the options here is internal
consistency with contracts this repository already states, not performance. Any
claim that one window predicts better than another waits for Phase 6.

## Decision 1: the change window ends at the latest session

Neither reading offered on #56 is adopted. Both anchor the end of the window to
a calendar boundary, and that is the part that is wrong.

`chg_1m` and `chg_3m` are trailing changes in the published level series:

- The later endpoint is the newest session the source holds for that currency.
- The earlier endpoint is the last session on or before the same calendar day
  one month back for `chg_1m`, or three months back for `chg_3m`. When that
  calendar day does not exist in the target month, use the last day of the
  target month.
- The value is `(later - earlier) * 100`, in basis points. Positive means the
  yield has risen, which is currency-positive, in line with every other
  indicator in the model.
- `Observation.period` is the later endpoint's session date, since that is the
  date the value describes and it is what `BasePillar.staleness_days` ages.
- When the earlier endpoint is not within a stated tolerance of its target date,
  emit no observation. A window longer than the one the key names is a wrong
  number wearing the right label, and ADR 0002 already settles that absence is
  represented rather than approximated. The tolerance is a registry or config
  number, not a literal, and choosing it belongs to `data-engineer` with
  `quant-analyst`.
- The two transforms differ only in the lag. Nothing else about them differs,
  which is the seam a `chg_6m` would land on if one is ever wanted.

Six things separate this from an anchored window, and five of them are checkable
inside the repository today rather than matters of taste.

**1. The registry already declares these series daily.** `YIELD_2Y_CHG_1M` and
`YIELD_2Y_CHG_3M` both carry `Frequency.DAILY` and `max_staleness_days=10`, and
every ref under them carries `Frequency.DAILY`. A value that changes once a
month, or once a quarter, is not a daily series. One of the two statements in
the registry has to give way, and the transform sentence is the one that was
already known to be ambiguous.

**2. An anchored period zeroes the weight of the heaviest sub-indicator for most
of the calendar.** `scoring.freshness` with the registry's 10-day allowance and
the shipped `ScoringConfig` gives a factor of 1.0 up to 3 days, 0.75 at 5 days,
0.45 at 7 days and 0.0 from 10 days on. With the period stamped at the last
quarter-end, the factor is zero for about 80 of every 91 days. With the period
stamped at the last month-end, it is zero for about 20 of every 30 days.
`BasePillar.component_freshness` feeds that factor straight into the component
blend, so under either anchored reading the model's heaviest sub-indicator
contributes nothing for most of the year while `ScoringConfig` and section 3.1
both say 0.25. That is invariant 4 broken in the same way the two defects named
in the engineering standards broke it, quietly and through arithmetic nobody
intended.

**3. An anchored period makes the staleness machinery blind.** With the period
pinned to a month-end or a quarter-end, a currency whose feed dies on the third
of the month keeps producing a fully fresh-looking change until the next anchor.
The 10-day allowance exists, in the registry's own words, because a 2-year yield
stale by a week means the feed broke. An anchored window throws that away for
0.135 of the composite. A window ending at the latest session keeps it: the feed
stopping is exactly what ages the observation.

**4. An anchored window reintroduces the calendar jump the ramp exists to
prevent.** The docstring of `scoring.freshness` says a hard cutoff "would let a
currency's composite jump on a day when no data changed and nothing happened
except the calendar turning over". An anchored window does precisely that, for
all eight currencies on the same day, in the sub-indicator with the most weight.
The CLI `--compare` diff reads the previous report, so that jump is rendered as
a move. A reader would see MONETARY shift across the whole universe on the first
business day of a quarter and have no way to tell it from repricing.

**5. `docs/data-sources.md` already promises the behaviour this decision
delivers.** It says of these two indicators that "Coverage, freshness and the
CHF/NZD manual gap are therefore identical to `yield_2y`'s", and its coverage
tables give both the same 6/8 fresh as the level. Freshness identical to a daily
level series is only true when the window ends at the latest session. Under
either anchored reading, the document's own published coverage figures are wrong
for most of the calendar.

**6. The rest of the pillar updates daily.** `policy_rate` and `yield_2y` are
daily levels. A pillar where two of five components step on a calendar boundary,
carrying 0.45 of the sub-weight between them, is not a coherent object. The
engine runs daily and its output is read daily.

On the last thing #56 asked to be weighed, whether either reading is what someone
would implement unaided from `docs/data-sources.md`: neither. The document
repeats the registry's ambiguity in a shorter form, "resampled to month-end or
quarter-end before differencing", and adds nothing that resolves it. The
registry docstring, both `IndicatorSpec.description` strings, the two
`_yield_change_series` note suffixes and the `docs/data-sources.md` section must
all be rewritten in the commit that implements this, or the ambiguity survives
the record that settled it.

## Decision 2: `_observation` takes the unit from `IndicatorSpec`

Of the three candidates named on #56, the second. `BaseDataSource._observation`
reads `unit` from `INDICATORS[indicator].unit` rather than from `ref.unit`, and
raises when a ref whose `transform` is `level` carries a unit its indicator does
not, since there is then nothing between the two to explain the difference and
no unit that is honest to emit.

That is exactly the rule `ManualSource` adopted in commit `3bf8c54`, for exactly
this reason, and it is already documented there: `SeriesRef.unit` is what a
source publishes before a transform, `IndicatorSpec.unit` is the canonical unit
after one. An `Observation` is the output of the transform, so it takes the
output unit. The two paths now say one thing instead of two.

**This is wider than the two yield transforms.** Twenty nine refs in the
registry carry a unit that differs from their indicator's, and four of the five
affected indicators are on `main` and being fetched today:

| Indicator | Refs | Ref unit | Indicator unit |
| --- | --- | --- | --- |
| `cpi_yoy` | USD, EUR | `index` | `percent` |
| `core_cpi_yoy` | USD, EUR | `index` | `percent` |
| `gdp_yoy` | seven | `billions_chained_usd` and six others | `percent` |
| `employment_chg` | USD | `thousands_of_persons` | `persons` |
| `yield_2y_chg_1m`, `yield_2y_chg_3m` | eight each | `percent` | `basis_points` |
| `commodity_price` | CAD | `usd_per_barrel` | `index` (level ref) |

FRED computes `pc1` and `chg` server side, so every one of those transformed
values is already in the canonical unit and is already labelled with the
published input's unit instead. The defect #56 found in a series nothing emits
yet is live in four series that do.

**On whether any source depends on `ref.unit` differing from the indicator's.**
Checked ref by ref across all twenty nine. None does. Twenty eight are
transformed refs whose emitted value is the canonical unit and whose label is
wrong today. The twenty ninth is `commodity_price` for CAD, a `level` ref where
the registry genuinely contradicts itself, and it is the reason the guard is part
of this decision rather than a separate cleanup: relabelling a barrel price as an
index would replace a true label on a wrong indicator with a false label on a
real number. Outside `_observation`, only `manual.py` reads `unit` off a ref, and
it already reads the spec's. No pillar reads `Observation.unit` at all.

**A unit rule alone is not enough, and the implementing change must not stop
there.** `employment_chg` for USD is `PAYEMS` in thousands of persons under an
indicator declared in persons, while the other seven arrive in persons. The
values are a thousand to one apart inside one cross-sectional z-score, so a
200,000 payroll print enters the EMPLOYMENT pillar as 200. Relabelling it
`persons` and changing nothing else makes that defect invisible instead of
merely undetected, and removes the only warning currently in the data. The
implementing pull request must confirm, for each of the twenty nine, that the
value really is in the indicator's unit and convert where it is not. That is the
difference between a labelling fix and a correctness fix, and only the second is
worth landing. The USD payroll scale is its own defect and wants its own issue.

**`src/fbe/types.py` does not change.** `Observation.unit` is already a `str`,
`_observation`'s signature does not move, and `base.py` already imports from
`registry`, so `INDICATORS` adds no dependency edge. Nothing in this record
requires a types change, and if an implementer finds one, that is a reason to
come back here rather than to land it.

## Alternatives considered

**Reading A, quarter-end with a one-period difference.** The literal
parenthetical, and the only reading under which `chg_1m` and `chg_3m` are the
same operation at different frequencies. Rejected on every count above and worst
on the arithmetic: on 15 May it compares end-March to end-December, so the window
closes six weeks before the run date, the value is unchanged for two months out
of three and then jumps, and the freshness ramp gives it zero weight for about
80 days of every 91. A momentum term that is constant for two thirds of its life
is not measuring momentum.

**Reading B, month-end for both with a one-period or three-period difference.**
The lane's own inference, and it is a strict improvement on A: the window is
never more than a month behind, the jump is monthly rather than quarterly, and it
is the only reading under which the phrase "difference over one or three
periods" means anything. It loses to the decision above for reasons 1 through 6
in weaker form rather than in kind. It was the right call against the reading it
was compared with, and the lane was right to say plainly that it was inference.

**Widen `max_staleness_days` so that an anchored window keeps its weight.** The
obvious rescue for Reading B, and the reason it is not adopted is worth keeping.
To stop the ramp chewing a month-end series, the allowance has to cover a whole
month at full weight, which means about 90 days given
`staleness_full_days / max_staleness_days` is one third. A 90-day allowance on a
two-year government yield cannot detect a feed that died nine weeks ago. The
choice is between an anchored window that loses its weight to the calendar and an
anchored window that cannot tell a dead source from a live one. A window ending
at the latest session needs neither, and keeps the 10-day allowance meaning what
it means for `yield_2y`.

**Option 1 for the unit, the registry's refs carry the post-transform unit.**
Cheapest diff, touches no shared code path. Rejected because it deletes a fact
the registry exists to hold. `_yield_change_series` reuses `YIELD_2Y`'s refs
unchanged, on purpose, so that the derived indicators cannot drift from the level
they are computed from, and `docs/data-sources.md` publishes those refs as a
description of what each provider actually serves. Saying the ECB publishes a
basis-point series is false. It also puts the canonical unit in two places, the
ref and the spec, and a number that exists in two places will disagree silently.
Applied across all twenty nine refs it would additionally erase the only visible
sign that `PAYEMS` is in thousands.

**Option 3 for the unit, each source passes the unit explicitly.** Rejected on
the docstring of the method it would change. `_observation` exists so that
`source`, `series_id`, `unit` and `frequency` are copied from the registry rather
than re-typed per source. Handing the unit back to four call sites is four
chances to type a wrong string, and a wrong unit string raises nothing anywhere.

**Do nothing and let the two indicators stay refused.** Rejected. It is 0.135 of
the composite absent from the model with no record of why, and it leaves four
live indicators mislabelled. Refusing was right while the ruling was outstanding,
and the lane was right to refuse rather than approximate. It stops being right
now that the ruling exists.

## Consequences

**Good.** The two indicators can be implemented, and the implementation is the
same in every source rather than three guesses. The declared 0.25 and 0.20
sub-weights become the operative ones on a normal day instead of on a handful of
days each quarter. The staleness allowance goes on meaning what it means for the
level series it is derived from. `docs/data-sources.md`'s published coverage
figures become true. Four indicators stop shipping a wrong unit label, and one
shared path decides the unit instead of five.

**Bad, and this is the real cost.** A trailing window moves when the far endpoint
rolls off, not only when today's yield moves. Under a month-end anchor the far
endpoint steps once a month and the base effect is visible and occasional. Under
this decision it moves every day, so a currency's `chg_3m` can change on a day
its yield did not, because the session three months ago dropped out. That is a
smaller and more frequent artefact than the anchored jump rather than no artefact
at all, and anyone reading a day's change in MONETARY should know it is there.

**Bad, and worse for two currencies.** CHF and NZD have no fetchable 2-year
yield and are typed by hand. A window ending at the latest session needs a recent
typed value and a typed value near the target date a month or three months back.
An operator entering a yield once a month satisfies a month-end anchor
comfortably and satisfies this convention not at all, so those two currencies
will hold no `chg_1m` or `chg_3m` unless someone types a yield at least weekly.
They lose 0.45 of MONETARY's sub-weight, `blend_components` renormalises over
what is left, and `MIN_COMPONENT_WEIGHT` decides whether the pillar survives at
all. That is a coverage loss and it is chosen deliberately: the alternative is a
hand-typed number from three weeks ago carrying full weight in the heaviest
sub-indicator, and this repository's prime directive prefers the loud gap.

**Bad.** The unit guard raises on `commodity_price` for CAD the moment it lands,
so `FredSource` stops emitting that series until the registry stops contradicting
itself. The number it emits today is already labelled wrong for the indicator it
sits under, and a barrel price and an index inside one cross-sectional z-score is
its own problem, but the visible effect of this decision is one series
disappearing from EXTERNAL. The implementing pull request must either correct
that ref or accept and name the gap.

**Bad.** Five pieces of prose currently say month-end or quarter-end: the
`TRANSFORMS` docstring, the two `IndicatorSpec.description` strings, the two note
suffixes built by `_yield_change_series`, and the `docs/data-sources.md` section.
If any survive the implementing commit, the ambiguity this record exists to
remove is still in the file a reader reaches first.

**Unresolved, and not this record's to settle.** The tolerance on the earlier
endpoint, and whether `max_staleness_days` for these two should stay at 10 now
that the period is a session date. Ten is inherited from `yield_2y` and looks
right for the same reason it is right there, but it has not been checked against
a holiday run of more than three sessions in any G10 market. Both numbers belong
to `data-engineer` and `quant-analyst`.

Also unresolved, and noticed while ruling rather than ruled on: on the section 7
fixture `chg_1m` and `chg_3m` produce nearly identical z-scores across all eight
currencies, which puts 0.45 of MONETARY on two series that say close to the same
thing. ADR 0001 already corrects the pillar's volume for the correlation between
its own components, so this is not a new leak, but whether two change horizons
earn 0.45 between them is a scoring question and Phase 6 is the first point at
which it can be answered.

## Reopening

Reopen decision 1 with a measurement, not a preference. The comparison that
would separate a trailing window from a month-end anchored one is the same
comparison ADR 0003 defers: both constructions against subsequent pair returns,
which needs the forward record Phase 6 produces. Read `docs/answers/scoring-maths.md`
Spec 8 first. It shows that two constructions which rarely disagree cannot be
separated at this cadence within any reasonable horizon, and how far apart these
two windows actually land on a given day has not been computed. Compute that
before designing the test.

Decision 2 reopens if a source is ever added whose published value is not in its
indicator's unit after its own transform, and which cannot convert. That source
would need the unit to travel with the value rather than with the key, and this
record would be superseded rather than amended.

## References

- Issue #56, comment of 2026-09-12, which states both questions and refuses to
  guess at either.
- Issue #58 and PR #104, and issue #59 and PR #107, both merged `Refs:` for the
  same reason.
- `src/fbe/datasources/registry.py`: `TRANSFORMS`, `_yield_change_series`,
  `YIELD_2Y`, `YIELD_2Y_CHG_1M`, `YIELD_2Y_CHG_3M`.
- `src/fbe/datasources/base.py::BaseDataSource._observation`.
- `src/fbe/datasources/fred.py`: `UNITS` and the refusal that raises `SourceError`
  for these two transforms.
- `src/fbe/datasources/manual.py`: `LEVEL_TRANSFORM`, and the unit rule commit
  `3bf8c54` that this record follows for the fetching path.
- `src/fbe/scoring.py::freshness`, `src/fbe/pillars/base.py::component_freshness`
  and `staleness_allowance`, for the effective-weight arithmetic.
- `docs/scoring-spec.md` section 3.1 and section 7.1, `docs/data-sources.md`
  under `yield_2y_chg_1m` and `yield_2y_chg_3m`.
- ADR 0002, for absence rather than approximation. ADR 0001 and ADR 0003, for the
  standing treatment of declared against operative weights.
