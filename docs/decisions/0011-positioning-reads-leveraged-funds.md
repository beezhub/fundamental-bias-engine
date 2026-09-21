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
percent and one wrote a ratio, so the formula's wording was the outlier. The
scale is free for the score, since dividing every point of a series by the same
constant leaves every time-series z-score unchanged, which is also why nothing
would have raised had the ratio been emitted: `PositioningPillar` would have
reported a headline number a hundred times flatter than the one section 7.4
shows, and every score would have been correct. A quantity that cannot be
checked by its effect on a score has to be pinned by its name.

## Consequences

Six places named the non-commercial numerator and now name leveraged funds:
section 3.6's formula and prose, its sub-indicator table row, section 7.4's
column label, the `COT_NET_PCT_OI` registry description, two docstrings in
`src/fbe/pillars/positioning.py`, and the forward-looking measurement recipe in
`docs/answers/scoring-maths.md`. `SPEC_ANCHORS` in
`tests/test_worked_example.py` tracks the 7.4 label, so it moved with it.

**No number in section 7 changed.** Line 1336 is a column label on a fixture of
fixed values, and `tests/test_worked_example.py` is explicit that a published
figure adjusted until an assertion passes is worse than no fixture at all.

The registry `unit` is `percent_of_open_interest`, not `contracts`, and
`SeriesRef.unit` is echoed onto every `Observation`, so the correction had to
reach the refs and not only the spec-level entry.

Asset manager and dealer columns stay in `TFF_FIELDS` and are consumed by
nothing. They are named so a cross-check is one query away, and the docstring
says so, so the next reader does not take them for a description of what is
built.

## What would reopen this

The correlation between the two candidate `p` series over the full lookback, and
the share of weeks on which `f(p)` differs in sign between them. That needs this
source to exist, so it could not come first. If that share is large and the
leveraged-funds series is the noisier of the two, bring the numbers back.

A second sub-indicator for asset manager positioning is a change to section
3.6's sub-weight table and is a proposal rather than a defect. It is not ruled
on here.
