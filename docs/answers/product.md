# Product open questions: answers

Answers to the product questions in `docs/roadmap.md` and the open questions in
`docs/reasoning-layer.md`. Each section gives a verdict, the reasoning, and a
recommendation. No code or other doc is changed here; where a recommendation
implies a change, the change is described, not made.

---

## Roadmap 9: what is the right refresh cadence?

**Verdict: ANSWERED**, with one sub-question left **BLOCKED** on usage data.

**Reasoning.** The routine in `docs/trading-plan.md` fixes the cadence before
this engine ever gets a vote. Step 1, Morning Routine, says to review the news
and economic calendar every day, not on days something changed. `fbe calendar`
is inherently a daily instrument: the calendar itself is different every day
even on a week where every currency's composite is flat, because new releases
roll onto and off the 24 to 72 hour window regardless of whether last month's
CPI print is still the freshest number in the model. So the calendar half of
the morning step settles the cadence on its own: daily, no argument.

The scoring half is the real question, and here the trading plan's psychology
rules point the same way the data does. Most of the seven pillars are monthly
series, so most days `fbe score` and `fbe bias` will show composites that
moved by noise, not by signal. That is not a reason to run less often, it is
the correct output. "Avoid overtrading," "the best trades are often the ones
you don't take," and "stay emotionally detached" (trading plan, Keep in mind
1 to 3, Enhance your focus 10) all describe a trader who is supposed to look
every day and conclude "no new information" most of those days. A tool that
only speaks up when something moved would train the opposite habit: it would
imply that silence means "don't bother checking," which contradicts the plan's
own reason for having a fixed daily routine in the first place, namely to build
habit independent of whether the market is offering anything that day.

The risk in the question, "does daily reporting invite banner blindness,"
is real but it is a rendering problem, not a cadence problem. It is only a
danger if a quiet day and a loud day look the same on the page. This project
already has the fix designed and not yet wired up: `ReportDiff` in
`src/fbe/report.py` and the "what changed since the last run" section in
`docs/interfaces.md` exist precisely to separate the two. A day with no
composite move past a threshold and no direction flips should read as one
short line, not as the full seven-pillar table re-read from scratch. Cadence
stays daily; attention scales with what actually moved.

**Recommendation.**
1. Keep `fbe refresh` and `fbe calendar` daily, as `docs/interfaces.md`
   already specifies. Do not add a weekly or conditional mode.
2. When `report.py` and the dashboard are built (Phase 5), add an explicit
   "quiet day" rendering: if no currency's composite delta exceeds a small
   threshold (for example `ScoringConfig.min_spread_low` scaled down, or a
   fixed 0.05) and no `PairChange.flipped` is true, the "what changed" section
   should render as a single sentence such as "No material change since
   yesterday: largest composite move was 0.03 on CAD" rather than the full
   diff table. This is a template concern in `report.md.j2` and
   `dashboard.html.j2`, not a scoring concern.
3. The residual question, whether daily review actually builds discipline or
   produces blindness in practice, is about the owner's behaviour, not the
   software, and cannot be settled from the plan text. It is **BLOCKED** on
   usage data. Instrument it once the journal exists: `fbe journal review`
   already reports "followed the plan" versus "broke the plan" splits
   (`docs/interfaces.md`, `fbe journal review`). Add one more cut to that
   split: for each trade, whether it was entered on a "quiet" day or a "loud"
   day per the diff, using the same threshold as above, stored on the journal
   entry. If plan violations or losing trades cluster on quiet days, that is
   the banner-blindness signature and the cadence question can be revisited
   with evidence instead of a guess.

---

## Roadmap 10: suppress a NONE-conviction pair, or show it greyed out?

**Verdict: ANSWERED.**

**Reasoning.** The trading plan's psychology rules are again the deciding
argument, not a coin flip between two equally good options. "Avoid
overtrading" and "the best trades are the ones you don't take" describe a
trader who needs to be told "no" as clearly as "yes." A pair silently missing
from the output is indistinguishable from a pair the engine never got to, and
that ambiguity is exactly the gap that invites the owner to re-derive a lean
by hand for a pair with no fundamental conviction, i.e. to fall back on gut
feel for the one part of the routine this engine exists to replace. Showing
the pair, clearly marked as no-conviction, is the only rendering that proves
the model looked and found nothing, which is itself useful information: it
tells the owner the pair is a pure technical trade this week, not a fundamental
one, without inviting them to invent a fundamental story for it.

The CLI has effectively already decided this at the interface level.
`docs/interfaces.md` documents `fbe bias --min-conviction` defaulting to
`none`, meaning nothing is hidden unless the trader opts in with
`--tradeable-only` or a higher `--min-conviction` filter. Suppression is
something the trader chooses per-session, not something the engine imposes.
The pair matrix in `fbe bias --matrix` cannot hide a cell in any case, since
it is a full 8x8 grid of 56 populated cells built by `_grid`; a NONE-conviction
pair there is a near-zero-spread cell on the diverging colour scale, which
`docs/interfaces.md` already specifies reads as "no view," not as a missing
value. The instruction sheet given to this role also states the standing
principle directly: "a shortlist entry without its pillar breakdown invites
blind trust," and the same logic runs the other way, a pair without an entry
anywhere invites the trader to trust their own hand-derived view instead,
which is worse.

**Recommendation.**
1. Default behaviour across `fbe bias`, `fbe report`, and the dashboard: never
   hide a pair by default. The trader filters explicitly with
   `--min-conviction` or `--tradeable-only` when they want a shorter list; the
   engine does not decide that for them.
2. In the dashboard's pair matrix (Phase 5, not yet built), a NONE-conviction
   cell stays in the grid at the neutral grey midpoint colour, consistent with
   the diverging-scale convention already specified in `docs/interfaces.md`.
   In the ranked list view, NONE-conviction pairs sort to the bottom and print
   with their spread and conviction column showing `none`, rather than being
   dropped from the row count.
3. The tradeable shortlist (`TradeIdea` cards) never lists a NONE-conviction
   pair, because there is no idea to attach a size or reasoning to. To keep
   the "engine considered everything" guarantee visible there too, the
   shortlist section can close with a one-line count, for example "20 pairs
   carried no conviction this run," so the reader can reconcile 28 pairs total
   without the shortlist itself growing to include empty cards.

---

## Reasoning-layer 1: how much of the scoring spec belongs in the cached prefix?

**Verdict: ANSWERED**, on the structure of the document, with token costs
given as estimates.

**Reasoning.** `docs/scoring-spec.md` is 1,267 lines and 65,793 characters.
Using the standard rough heuristic of about 4 characters per token for
English technical prose (not a measured tokenizer count, an estimate):

```
$ python3 -c "
chars = 65793
for cpt in (3.5, 4, 4.5):
    print(cpt, 'chars/token ->', round(chars/cpt), 'tokens')
"
3.5 chars/token -> 18798 tokens
4 chars/token -> 16448 tokens
4.5 chars/token -> 14621 tokens
```

So the whole document is roughly 15,000 to 19,000 tokens. `docs/reasoning-layer.md`
already prices the cached prefix at roughly 12,000 tokens total for "the
system prompt, a condensed form of the methodology and scoring spec, the
pillar definitions, the sign conventions, and the conviction table." The
whole document alone is bigger than that entire budget, before the system
prompt or anything else is added. Caching the full spec verbatim is not an
option to weigh, it does not fit the number the document itself already
commits to.

The document's own table of contents shows which parts a brief actually needs
and which parts it does not:

| Section | Lines | Needed by a brief? |
| --- | --- | --- |
| 1. Conventions (sign, score band, notation) | 13 to 51 (39) | Yes, condensed to a few sentences |
| 2. The pipeline (7 stages) | 52 to 176 (125) | Yes, condensed to a list, not the derivations |
| 3. Pillar specifications, all 7 | 177 to 497 (321) | Yes, this is the core of what "explains a pillar score" means |
| 4. Aggregation (staleness, coverage, dispersion, rank) | 498 to 554 (57) | Yes, condensed: these are the fields the brief narrates |
| 5. Pair layer (spread, direction, agreement, conviction) | 555 to 630 (76) | Yes, especially the conviction bands, since the brief has to explain why something is medium and not high |
| 6. Hard filters | 631 to 675 (45) | Partially: the blackout logic in one line, not the full filter table |
| 7. Worked example | 676 to 1087 (412) | No. This exists so a human can check the arithmetic by hand. The brief already has the real report's numbers; a fictitious worked example adds no explanatory power and is the single largest section in the file. |
| 8. Re-weighting | 1088 to 1148 (61) | No. This is a maintenance procedure for whoever edits `ScoringConfig`, irrelevant to explaining a given day's score. |
| 9. Test obligations | 1149 to 1178 (30) | No. Implementation concern. |
| 10. Open questions | 1179 to end | No. Explicitly unresolved design debate, not something to state to a reader as settled. |

Sections 7 to 10 are 45% of the file's lines and are exactly the parts a brief
does not need: a worked example for a human auditing the code by hand, and
maintenance and process notes. Excluding them leaves sections 1 to 6, about
660 lines, roughly half the document, which is still too large to hand over
verbatim at 4 pillars' worth of prose, docstring-style weakness notes, and
full formulas per pillar. Section 3 alone, the seven pillar specs, is 321
lines, and most of that per pillar is inputs, a transform, a sign convention,
a weight, and known weaknesses in full prose, when a brief needs only the
first four and a compressed version of the fifth.

**Recommendation.** Do not cache `docs/scoring-spec.md`. Write a separate,
short condensed brief primer (a new file, for example
`docs/reasoning-layer-primer.md`, or a generated fragment built from
`ScoringConfig` and the pillar docstrings rather than hand-maintained prose)
that keeps only:
- The one-line sign convention and score band (section 1, compressed to about
  5 lines).
- The 7-stage pipeline as a list, not the notation derivations (section 2,
  compressed to about 10 lines).
- Per pillar: name, weight, inputs, transform shape, and the sign convention's
  one-line justification. No worked numbers, no "known weaknesses" prose.
  Roughly 10 to 15 lines per pillar, about 90 lines for all seven, against
  321 in the source.
- Coverage, dispersion, and the conviction bands table verbatim, since these
  are the exact numbers a brief will be asked to explain (section 4 and the
  conviction part of section 5, about 40 lines combined).
- The blackout rule in one line (part of section 6).

That totals roughly 150 to 200 lines, versus 1,267. At the same characters
per line as the full document (about 52 chars per line) that is 7,800 to
10,400 characters, or, at 4 characters per token, roughly **2,000 to 2,600
tokens**, again a rough estimate, not a measured count. That fits comfortably
inside the roughly 12,000-token cached prefix the reasoning-layer document
already budgets for, alongside the system prompt itself and the pillar and
conviction material it separately lists, some of which overlaps with what is
proposed here and should be merged rather than duplicated when this is built.

---

## Reasoning-layer 2: should the daily brief see the previous day's brief?

**Verdict: NARROWED.** Feeding the model the previous day's rendered prose is
the wrong design regardless of the compounding risk, because it duplicates
information the deterministic layer already owns more precisely. A design
that gets continuity without compounding exists and should be used instead.

**Reasoning.** The continuity the question wants, "read better," is really
"reference what changed and whether yesterday's read of it held up." Both of
those already exist as data, computed deterministically, before any model call:
`ReportDiff` from `diff_reports` in `src/fbe/report.py`, and the hot list from
`hotlist.py` in `docs/reasoning-layer.md`. Handing the model yesterday's prose
on top of that is redundant at best. At worst it is exactly the compounding
risk named in the question: prose is not falsifiable the way a diff is, so if
yesterday's brief narrated a coverage-driven conviction drop as if it were a
fundamental weakening (the "hollow move" case the hot list is specifically
built to catch), a model reading that sentence today has no way to tell it was
wrong and will tend to carry the framing forward. A diff computed fresh from
the two reports every time has no such memory to be wrong about: it is
recomputed from source data on every run, so a bad reading on Monday cannot
propagate into Tuesday's inputs.

The document's own architecture points at the fix. `MacroBrief` and `Claim`
with `Evidence` (planned in `src/fbe/reasoning/types.py`) are structured, not
prose, and the verifier already grounds every claim against report values.
That means each of yesterday's claims can be re-checked against today's report
by ordinary code, with no model call: does the evidence field yesterday's
claim pointed at still hold the same relationship today (still positive, still
the top mover, still above the crowding threshold), or has it moved. That
produces a short, structured status list, for example "EUR growth pillar
softening: still true" or "JPY conviction drop: was coverage-driven, coverage
has since recovered," which is exactly the continuity the question wants,
generated the same way the hot list is, by code, not by asking the model to
remember its own sentence.

**Recommendation.** Do not pass the previous day's rendered `MacroBrief` text
into the daily context pack. Instead:
1. Add a small deterministic step, alongside `hotlist.py`, that re-evaluates
   each `Claim` from the previous day's stored brief against today's report
   and tags it `confirmed`, `reversed`, or `superseded-by-new-data`. This
   needs no API call and belongs in the same "no model, no network" tier as
   the hot list.
2. Put that tagged list, not the prior prose, into `context.py`'s volatile
   half of the context pack for the daily brief.
3. Reserve actual narrative continuity, referencing how a story has developed
   over several days in the model's own words, for the weekly brief, which
   already gets `effort: high` and sets the watchlist. A model rereading a
   week of structured, machine-checked claims once a week has far less
   surface for an early misreading to compound across than a model rereading
   yesterday's prose every single day.

---

## Reasoning-layer 3: what is the right tolerance for numeric grounding?

**Verdict: JUDGEMENT.** There is no measured answer available yet; this is a
design call, made below and stated as such.

**Reasoning.** The question's own framing is correct: a score of 1.62, a
coverage of 0.95, and a risk amount of R35.81 are different kinds of numbers
and a single tolerance for all of them will be wrong for at least two of the
three. A single fixed absolute tolerance, say plus or minus 0.05, is far too
loose for a risk amount (5 cents matters when the whole exercise is proving
sizing was not rounded away, per `fbe size`'s worked example in
`docs/interfaces.md`, where an 11% rounding effect is treated as worth calling
out) and might be too tight for a coverage percentage where the natural
reading unit is a whole percentage point. A single fixed relative tolerance,
say plus or minus 2%, is wrong the other way: 2% of a composite near zero is
a fraction of a point, so a model rounding "-0.02" to "roughly flat" would
fail an over-tight relative check even though "roughly flat" is the correct
reading.

The three example numbers also differ in how a trader actually reads them.
A composite or spread on the -3 to +3 band is a ranking position, and moving
it by even a tenth can cross a conviction boundary (`docs/scoring-spec.md`
section 5.4 sets conviction bands on `min_spread_*` thresholds), so the
grounding gate has to be tight enough that "1.6" is never accepted for a true
value of 1.9. A coverage or agreement figure is a percentage where the natural
spoken unit is a whole point, "86%" for 0.859, and a reader does not
distinguish 86% from 85.9%, so tolerance here can be looser without any real
information loss. A money amount is read at whichever precision the trader
would act on, cents for a R2,000 account where every rand is close to 1% of
the risk cap, so tolerance should track relative size rather than a flat
absolute figure: 5 cents off on R35.81 is nothing, 5 rand off on R35.81 is
a different trade.

**Recommendation.** Field-specific tolerance, not one number:

| Field kind | Examples | Rule |
| --- | --- | --- |
| Score band values (-3 to +3): composite, pillar score, spread | 1.62, -0.89 | Absolute tolerance of 0.05. Tight, because a tenth of a point can cross a conviction boundary, and this is the number the whole shortlist rests on. |
| Ratios published as percentages: coverage, dispersion-derived bands, agreement | 0.95 rendered as "95%" | Absolute tolerance of 1 percentage point (0.01 in the underlying fraction). A reader does not act differently on 85% versus 86%. |
| Money amounts: risk_amount, realised_risk_amount, notional, pip value | R35.81 | The larger of 1% relative or R0.50 absolute. Relative for large notional figures, an absolute floor so a small number like a pip value is not held to an unreasonably tight relative bar. |
| Counts and ranks: rank position, "3 majors hidden," pair counts | rank 4, 28 pairs | Exact match, zero tolerance. These are discrete and a rounding argument does not apply to them. |

Two further rules, both about how the tolerance interacts with the prose
itself rather than just the numbers:
1. **Hedge language widens the tolerance, precision language does not.**
   If the sentence uses an approximating word, "around," "roughly," "close
   to," "nearly," treat the stated number as a rounded value at whatever
   precision it was given and accept it if the true value rounds to the same
   figure at that precision, using standard rounding. If the sentence states
   the number with no hedge, treat it as a precise claim and hold it to the
   table above with no widening. This rewards a brief for hedging honestly
   and penalises a false precise-sounding claim, which is the direction the
   grounding gate should push generation in.
2. **Compare the two numbers at the coarser of the report's precision and
   the prose's precision**, not at the report's fixed two decimals. The
   report always publishes two decimals; the prose is allowed to say "1.6"
   for a true value of 1.62. The gate should round the true value to one
   decimal before comparing, not require the prose to reproduce both
   decimals. This is what stops the tolerance table above from stripping
   true statements just because the model rounded, which the question
   specifically warns against.

This tolerance table is a starting design, not a measured result. Once
`verify.py` exists and the rejection log at
`data/reports/briefs/<date>.rejected.jsonl` accumulates real strips, the log
itself is the evidence to revisit these numbers against, exactly as
`docs/reasoning-layer.md` already says it should be used for the verifier's
backlog generally.
