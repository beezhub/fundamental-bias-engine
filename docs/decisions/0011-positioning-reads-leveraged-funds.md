# 0011. POSITIONING reads leveraged funds, as a percent of open interest

Status: Accepted

## Context

`cot_net_pct_oi` is POSITIONING's only sub-indicator, at sub-weight 1.00.
Building the source for it (#174) ran into two documents that disagreed about
what the key holds.

**Which column.** Section 3.6 of `docs/scoring-spec.md` defined the quantity as
`(non_commercial_long - non_commercial_short) / open_interest`. Non-commercial
is a category of the CFTC's **Legacy** report. The issue's third acceptance
criterion mandates the **Traders in Financial Futures** report, which has no
non-commercial column: it splits the same open interest into dealer, asset
manager, leveraged funds and other reportables. The `TFF_FIELDS` docstring in
`src/fbe/datasources/cot.py` named a third quantity, `lev_money_long -
lev_money_short`, and argued for it. So the specification, the criteria and the
module disagreed, and the registry description agreed with the specification.

Build desk B stopped before writing code and captured both datasets live for
report date 2026-09-08, futures-only, TFF `gpe5-46if` and Legacy `6dca-aqww`.
The four candidate readings, as a share of open interest:

| | TFF leveraged funds | TFF AM+LF+Other | TFF LF+AM | Legacy non-commercial |
| --- | ---: | ---: | ---: | ---: |
| AUD | +0.1093 | +0.0316 | +0.0210 | -0.0766 |
| CAD | -0.1656 | -0.2335 | -0.2552 | -0.2105 |
| CHF | -0.0875 | -0.3210 | -0.3196 | -0.1951 |
| EUR | -0.0353 | +0.2393 | +0.2307 | -0.0452 |
| GBP | +0.1087 | -0.2206 | -0.2236 | -0.1847 |
| JPY | -0.0983 | +0.0725 | -0.0994 | +0.0216 |
| NZD | -0.1377 | -0.0030 | -0.0264 | +0.0495 |
| **implied USD** | +0.3064 | +0.4346 | +0.6725 | +0.6410 |

**What scale.** The key is named for a percent and `PositioningPillar` documents
receiving one, in four sentences including "divided by open interest, in
percent, so this pillar never sees a raw count". Section 7.4's worked example
has USD at +26.2 and `tests/test_worked_example.py` asserts that figure. Section
3.6's formula was a bare division and called the result a share, a hundred times
smaller than the same document's worked example.

Section 3.6 was not alone, and the other one was a decision rather than a
wording. The `COT_NET_PCT_OI` registry description ruled explicitly for the
ratio, with reasoning: "**A share, not a percentage.** The value is in
``[-1, 1]``, not ``[-100, 100]`` ... The ``pct`` in the key is historical, and
the key is not renamed again because a rename costs every consumer while the
unit field already says what the number is." That argument anticipates and
rejects the one that wins below, so this record is reversing a prior decision
and not merely correcting a loose sentence.

## Decision

**`cot_net_pct_oi` is leveraged funds, over all open interest, in percent.**

    (lev_money_positions_long - lev_money_positions_short) / open_interest_all * 100

on `gpe5-46if`, the TFF futures-only dataset. The division and the scale happen
in the data source, because sources emit canonical keys. `fetch_contract` still
returns raw Socrata rows, which is what `derive_usd_position` consumes.

**The dollar is the sign-flipped net of the seven other legs**, each normalised
by its own open interest before they are added. There is no liquid dollar
contract in TFF, and a long EUR future is a short dollar position.

## Why

**The decisive argument is in the issue's own third criterion.** It uses TFF
because "the Legacy report's commercial and non-commercial split lumps every
financial participant into one bucket". Netting asset manager, leveraged funds
and other reportables together is, by construction, everything that is not the
dealer, which is exactly that bucket. It pays for the TFF integration and then
discards the only thing TFF buys. If the single bucket were the right quantity,
the Legacy report is the cheaper way to get it, and criterion 3 has already
ruled that out.

**It matches what the pillar says it measures.** Section 3.6 reasons from forced
liquidation: the position is fragile because "the buyers have already bought"
and "the next piece of bad news forces liquidation". That describes leveraged
money, which runs stops and answers to redemptions. An asset manager's currency
exposure is mostly benchmark and overlay flow that does not unwind on a bad
print, so netting it in dilutes the one property the pillar is built on. The
`TFF_FIELDS` docstring made this argument first and nothing in the specification
answered it. Section 3.6 said non-commercial because it was written before the
decision to use TFF, not because anyone weighed the two.

**On the table above, read what it does and does not show.** The four sign
differences are in the raw level. The pillar does not consume the level: it
z-scores against that currency's own five-year history, so what reaches `f(p)`
is the sign of the deviation from that currency's own mean. A currency at +0.109
against a five-year mean of +0.15 scores negative. The table shows the four
definitions are far enough apart in level that they are not interchangeable,
which is worth having and is why stopping to ask was right. It does not show
that four currencies would be scored with opposite signs, and this record does
not rule as though it did.

**The scale follows the consumer's declared contract.** Four places read a
percent, and the two that wrote a ratio were the specification's formula and the
registry description quoted above. The scale is free for the score, since
dividing every point of a series by the same constant leaves every time-series
z-score unchanged, which is also why nothing would have raised had the ratio
been emitted: `PositioningPillar` would have reported a headline number a
hundred times flatter than the one section 7.4 shows, and every score would have
been correct. A quantity that cannot be checked by its effect on a score has to
be pinned by its name.

**Against the prior decision's own argument, which was that the unit field says
what the number is.** It does, to a reader who looks at it. The rename it
rejected is not on the table here and was never the only alternative: the key
keeps its name, the unit field keeps saying what the number is, and the number
becomes the one both of them describe. What the prior decision left standing was
a pillar docstring, a worked example and a test all reading a percent from a key
declared a share, which is four consumers to correct instead of one producer.

## Consequences

Seven places named the non-commercial numerator and now name leveraged funds:
section 3.6's formula and prose, its sub-indicator table row, section 7.4's
column label, the `COT_NET_PCT_OI` registry description, three docstrings in
`src/fbe/pillars/positioning.py`, the `net_position` transform note in the
registry, and the forward-looking measurement recipe in
`docs/answers/scoring-maths.md`. `SPEC_ANCHORS` in
`tests/test_worked_example.py` tracks the 7.4 label, so it moved with it.

The same sweep for the scale reached further than the numerator's did, and two
of its misses were found in review rather than by searching for the word:
`cot.py`'s own module docstring and the CFTC section of `docs/data-sources.md`
both still said the source returned raw contract counts and left the division to
the scoring layer. Both are the first thing a reader of either file meets.

**No number in section 7 changed.** Line 1336 is a column label on a fixture of
fixed values, and `tests/test_worked_example.py` is explicit that a published
figure adjusted until an assertion passes is worse than no fixture at all.

The registry `unit` is `percent_of_open_interest`, not `contracts`, and
`SeriesRef.unit` is echoed onto every `Observation`, so the correction had to
reach the refs and not only the spec-level entry.

**A dollar week is derived only where every leg reported it.** A sum cannot
tell an absent leg from a leg at zero, so a week one contract missed has no
dollar reading rather than a reading built from the six that published. One leg
empty for a whole window is the opposite case and raises: the series would
otherwise stop with nothing said, and an empty sequence reads downstream as a
currency with no positioning. All seven empty is a window the dataset holds no
week for, which is data.

`PositioningPillar`'s `headline_component` is `net_percent`, renamed from
`net_share` in the same change. Nothing consumed it yet, `_transform` being
scaffolded, so the rename was free now and would not have been after #175.

`PositioningPillar`'s USD paragraph specified the negative of the
open-interest-weighted mean of the seven legs. That quantity is
``sum(net) / sum(open_interest)``, which is the raw contract-count sum over
total open interest and therefore the euro-dominated reading the source exists
to avoid: +2.98 against +30.64 on the capture above. It also gave the ICE Dollar
Index contract precedence "where the registry supplies it", and the registry
does supply it, so the stated rule resolved to a branch the source never takes.
Both were corrected to describe the sum the source emits.

Asset manager and dealer columns stay in `TFF_FIELDS` and are consumed by
nothing. They are named so a cross-check is one query away, and the docstring
says so, so the next reader does not take them for a description of what is
built.

## Two things this record leaves open

**The derived dollar leg's unit is loose and knowingly so.** Its value is the
sum of seven percents of seven different denominators, which is not a percent of
any one open interest, and it sits on roughly seven times the scale of the legs
it is built from. It carries `percent_of_open_interest` because `_observation`
copies the unit from the ref and because the scale of its terms is the closest
true thing available. Section 7.4 forces the sum rather than a mean: its USD row
is +26.2 against legs in the tens, where a mean of seven would be nearer +4.
Whether the derived leg deserves a unit of its own, and whether anything should
distinguish a derived reading from a fetched one, is a change to `types.py` or to
the registry's vocabulary and is not taken here.

**The release stamp is derived from the period, which `BaseDataSource`
forbids.** Its `_observation` docstring says `released_at` is "never derived
from ``period``", because a lag assumed from a period is the look-ahead bias
Phase 6 has to avoid. This source adds three days to the Tuesday. The exception
is deliberate: the CFTC publishes on a fixed schedule, so the Friday is the
release date rather than an assumption about it, and #174's second criterion
requires it. What it costs is the case where the schedule does not hold.
Publication has been suspended and backfilled before, and every Tuesday inside
such a span is stamped as released three days later although none of them could
be read until the catch-up. Closing that needs a published release calendar,
which this dataset does not carry. The report date is checked to be a Tuesday so
that the three-day addition cannot silently produce a release stamp on a day the
CFTC never publishes on.

## What would reopen this

The correlation between the two candidate `p` series over the full lookback, and
the share of weeks on which `f(p)` differs in sign between them. That needs this
source to exist, so it could not come first. If that share is large and the
leveraged-funds series is the noisier of the two, bring the numbers back.

A second sub-indicator for asset manager positioning is a change to section
3.6's sub-weight table and is a proposal rather than a defect. It is not ruled
on here.
